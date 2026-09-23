#!/usr/bin/env python3
"""
统一 LLM 调用层 — OpenAI 兼容接口（优先）或 Claude CLI（回退）
══════════════════════════════════════════════════════════════
配置（环境变量 / .env）:
  LLM_PROVIDER=openai                    # 启用 OpenAI 兼容接口
  OPENAI_BASE_URL=http://127.0.0.1:8080/v1
  OPENAI_API_KEY=任意非空串               # llama.cpp 不校验
  LLM_MODEL=模型名或 gguf 路径
  LLM_MAX_TOKENS=8192                     # 单轮最大输出 token
  LLM_TIMEOUT=1800                        # 默认超时（秒）
  NO_THINK=1                              # Qwen3 系列禁用思考模式（防污染 JSON 输出）
"""
import os
import re
import subprocess
import time

import requests

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")

# Reasoning artefacts that must never reach generated documents. The local model
# intermittently emits stray </think> tags even with enable_thinking=False; one leaked
# into a generated wiki page as its entire body (wiki/people/*.md line 3).
_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>[\s\S]*?</think\s*>", re.IGNORECASE)
_THINK_TAG_RE = re.compile(r"</?think\b[^>]*>?", re.IGNORECASE)
# A lone truncated tag ("</think" with no ">" at all) is only stripped when it stands
# alone on its line, so real prose that merely mentions the word is left alone.
_THINK_LONE_RE = re.compile(r"^[ \t]*</?think\b[^\n]{0,4}$", re.IGNORECASE | re.MULTILINE)


def strip_reasoning(text: str) -> str:
    """Remove <think> blocks/tags (including truncated ones) and tidy whitespace."""
    if not text:
        return ""
    out = _THINK_BLOCK_RE.sub("", text)
    out = _THINK_LONE_RE.sub("", out)
    out = _THINK_TAG_RE.sub("", out)
    # Collapse the blank lines a removed block leaves behind.
    out = re.sub(r"\n{3,}", "\n\n", out)
    # Drop leading/trailing empty lines left by a removed tag on its own line.
    return out.strip()

def _post_chat(payload: dict, timeout: int, attempts: int = 3) -> dict:
    """POST /chat/completions with backoff on transient 5xx.

    llama.cpp intermittently returns 500 on a request whose prompt is much larger than
    whatever it just served (observed right after a smaller call, with the process
    staying healthy throughout). A single shot made a whole pipeline step produce no
    output, so retry server-side failures rather than losing the step.
    """
    last_err = None
    for i in range(attempts):
        try:
            resp = requests.post(
                _base_url() + "/chat/completions",
                headers={"Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY', 'sk-no-key')}"},
                json=payload,
                timeout=timeout,
            )
            if resp.status_code >= 500:
                last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
                if i < attempts - 1:
                    time.sleep(1.5 * (i + 1))
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            last_err = f"{type(e).__name__}: {e}"
            if i < attempts - 1:
                time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"LLM 请求失败（重试 {attempts} 次）: {last_err}")


def _base_url() -> str:
    return os.environ.get("OPENAI_BASE_URL", "").rstrip("/")


def _openai_chat(prompt: str, timeout: int) -> str:
    base = _base_url()
    key = os.environ.get("OPENAI_API_KEY", "sk-no-key")
    model = os.environ.get("LLM_MODEL", "")
    if not base or not model:
        raise RuntimeError("OPENAI_BASE_URL / LLM_MODEL 未配置")

    content = prompt
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0.3,
        "max_tokens": int(os.environ.get("LLM_MAX_TOKENS", "8192")),
    }
    if os.environ.get("NO_THINK", "1") == "1":
        # Disable reasoning mode. The textual "/no_think" marker is Qwen3-era and is
        # only *sometimes* honoured by newer templates (measured: it still dropped the
        # enclosing [] from a JSON array, and the model burned its whole token budget
        # on `reasoning_content` and returned empty `content`). The reliable switch is
        # the template kwarg the server's chat template actually reads.
        content = prompt + "\n/no_think"
        payload["messages"] = [{"role": "user", "content": content}]
        payload["chat_template_kwargs"] = {"enable_thinking": False}

    data = _post_chat(payload, timeout)["choices"][0]

    msg = data.get("message", {})
    out = strip_reasoning(msg.get("content") or "")
    if not out:
        # Reasoning mode swallowed the budget (finish_reason == "length" with only
        # reasoning_content set). Surface it instead of silently returning "".
        reason = data.get("finish_reason")
        rlen = len(msg.get("reasoning_content") or "")
        print(f"  [llm] 空 content (finish={reason}, reasoning={rlen} 字符)；"
              f"若频繁出现请确认 NO_THINK=1 生效")
    return out


def llm_chat(prompt: str, timeout: int = 0) -> str:
    """发送 prompt 返回模型文本；失败返回空字符串（不抛异常）。

    LLM_PROVIDER=openai 时走 OpenAI 兼容接口；否则回退 Claude CLI。
    timeout<=0 时使用 LLM_TIMEOUT 环境变量（默认 1800s）。
    """
    if timeout <= 0:
        timeout = int(os.environ.get("LLM_TIMEOUT", "1800"))

    if os.environ.get("LLM_PROVIDER", "").lower() == "openai":
        try:
            out = _openai_chat(prompt, timeout)
            if not out and os.environ.get("NO_THINK", "1") == "1":
                # Not every server/template accepts chat_template_kwargs; if the model
                # spent the whole budget thinking, retry once with the textual marker
                # only so a single quirk cannot silently cost a whole pipeline step.
                print("  [llm] 重试一次（不带 enable_thinking kwarg）")
                saved = os.environ.get("NO_THINK")
                os.environ["NO_THINK"] = "0"
                try:
                    out = _openai_chat(prompt + "\n/no_think", timeout)
                finally:
                    if saved is None:
                        os.environ.pop("NO_THINK", None)
                    else:
                        os.environ["NO_THINK"] = saved
            return out
        except Exception as e:
            print(f"  [llm] OpenAI 兼容接口调用失败: {e}")
            return ""

    try:
        result = subprocess.run(
            [CLAUDE_BIN, "--print", "--input-format", "text",
             "--max-turns", "5", "--model", "claude-sonnet-4-6"],
            input=prompt, capture_output=True, text=True, timeout=timeout,
        )
        return result.stdout.strip()
    except Exception as e:
        print(f"  [llm] Claude CLI 调用失败: {e}")
        return ""
