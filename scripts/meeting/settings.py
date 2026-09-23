"""One place for the settings that were previously environment variables only.

The three back ends differ in a way that decides what belongs here:

* **ASR** — always local, always shipped. It is the product's core input; if it depended on
  a network call, the assistant would stop working in the meeting rooms it is built for.
  So its settings are paths and model names, never credentials.
* **embedding** — also always local, for the same reason plus one more: the index is built
  offline and stored on disk, so the embedding model must be reproducible years later
  without a subscription.
* **LLM** — deliberately *not* bundled. Keyword extraction and clue classification are
  enhancements around a transcript that already exists, so a missing or distant model
  degrades the assistant instead of breaking it. That makes it the one back end worth
  pointing at whatever the user already has: a local server, or any OpenAI-compatible API.

Because the LLM is remote and configurable, its settings include a key. This file is
therefore written with restrictive permissions where the platform supports it, and the key
is **never** returned to the browser -- the API reports only whether a key is set.

Precedence, highest first: explicit CLI flag, then ``config/settings.json``, then the
environment (``config/.env`` and the shell), then the built-in default. That order lets a
one-off ``--set llm.model=x`` override a saved setting without editing files, while leaving
existing ``.env`` setups working unchanged.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent      # scripts/meeting/
_ROOT = _HERE.parent.parent                  # the repository root

DEFAULT_PATH = _ROOT / "config" / "settings.json"

# schema: section -> key -> (type, default, label, hint)
SCHEMA: dict[str, dict[str, tuple[str, Any, str, str]]] = {
    "llm": {
        "provider": ("str", "openai_compatible", "接口类型",
                     "openai_compatible 兼容 llama.cpp / vLLM / DeepSeek / OpenAI 等；"
                     "claude_cli 走本机 claude 命令行；none 关闭（只用规则分类）"),
        "base_url": ("str", "http://127.0.0.1:8080/v1", "接口地址",
                     "以 /v1 结尾，例如 https://api.deepseek.com/v1"),
        "api_key": ("secret", "", "API Key", "只保存在本机；本地 llama.cpp 可任意填"),
        "model": ("str", "", "模型名", "例如 Qwen3-4B-Q4_K_M 或 deepseek-chat"),
        "max_tokens": ("int", 1024, "单次最大输出", "关键词与线索分类都很短，1024 足够"),
        "no_think": ("bool", True, "关闭思考过程",
                     "部分模型会输出思考段落，开启后要求它直接给答案"),
        # 1800 是 llm_client 里的历史默认值（LLM_TIMEOUT）。以前这个设置项和
        # LLM_TIMEOUT 是两套：界面上改它、存进 settings.json，但没有任何代码读它，
        # 真正生效的一直是 llm_client 的 1800。现在它真的映射到 LLM_TIMEOUT 了，
        # 所以默认值必须写成实际生效的那个数，否则界面显示的又是一个假值。
        "timeout": ("int", 1800, "单次调用超时（秒）",
                    "本地模型首次调用要加载权重，可能很慢；超时后该段会重试，共 3 次"),
    },
    "embedding": {
        "backend": ("str", "bge-small-zh", "嵌入模型",
                    "bge-small-zh 体积小(23.9MB)、中文够用；bge-m3 支持多语言(568MB)。"
                    "两种向量空间不兼容，换模型必须重建索引"),
    },
    "asr": {
        "engine": ("str", "funasr", "识别后端",
                  "funasr = Paraformer 流式 + 离线二遍（默认）；"
                  "firered = FireRedASR2S（AED 识别 + 流式 VAD + BERT 标点，"
                  "准确率更高，模型约 5GB，需 GPU）。改动后需重启服务生效"),
        "model_dir": ("str", r"E:\models\gguf-asr", "ASR 模型目录",
                      "内置 SenseVoice / Paraformer 模型所在目录，随软件提供"),
        "device": ("str", "cuda:0", "推理设备", "cuda:0 或 cpu"),
        "firered_dir": ("str", r"E:\WhisperX\FireRedASR2S", "FireRedASR2S 目录",
                        "engine=firered 时生效：含 pretrained_models"
                        "（FireRedASR2-AED / FireRedVAD / FireRedPunc）的目录"),
        "hotwords": ("bool", True, "热词纠错",
                     "按参会名单+领域术语对识别文本做文本级纠错（防专名/术语错字）。"
                     "关闭后识别结果原样输出"),
        "hotwords_dir": ("str", "", "显式错词表目录",
                         "可选：放 错词=>正词 文本文件的目录（每行一条，# 开头为注释）。"
                         "留空则只用参会名单/术语的拼音模糊纠错"),
    },
    "corpora": {
        "public_db": ("str", str(_ROOT / "data" / "ar.db"), "公共库索引",
                      "合同、资质、规范等公司级资料"),
        "public_kb": ("str", str(_ROOT / "data" / "public"), "公共库语料目录",
                      "导入到公共库的文件复制到这里"),
        "project_db": ("str", "", "项目库索引",
                       "留空则用 <项目文件夹>/.plaud/rag.db，随项目文件夹走"),
    },
    "ui": {
        "theme": ("str", "dark", "主题", "dark 或 light"),
        "poll_ms": ("int", 1000, "界面刷新间隔（毫秒）", "越小越实时，越大越省电"),
    },
}

SECRET_KEYS = {f"{s}.{k}" for s, keys in SCHEMA.items()
               for k, spec in keys.items() if spec[0] == "secret"}

# 这些键是**进程启动时**才读的（ASR 模型、嵌入后端、知识库路径都在服务起来的那一刻
# 加载好），改完必须重启服务才生效。其余键（大模型走环境变量、主题存浏览器）改完立即
# 生效。界面据此提示"需重启"，并只在需要时给出「重启服务」按钮；这份集合是唯一出处，
# `/api/settings` 会把它回传给页面。
RESTART_KEYS = {
    "asr.engine", "asr.model_dir", "asr.device", "asr.firered_dir",
    "asr.hotwords", "asr.hotwords_dir", "embedding.backend",
    "corpora.public_db", "corpora.public_kb", "corpora.project_db",
}


class Settings:
    """Load, validate, and save settings with a documented precedence chain."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_PATH
        self.values: dict[str, Any] = self._defaults()
        self.sources: dict[str, str] = {k: "默认" for k in self.values}
        self.load()

    # ── defaults / load / save ──────────────────────────────────────────

    @staticmethod
    def _defaults() -> dict[str, Any]:
        return {f"{s}.{k}": spec[1] for s, keys in SCHEMA.items() for k, spec in keys.items()}

    def load(self) -> None:
        # 0. config/.env first, lowest priority. It is the older configuration mechanism
        #    (kept for compatibility); settings.json and the environment take precedence.
        #    Without reading it here, a working .env setup would look unconfigured in the
        #    settings page ("llm.model 未设置") while every other part kept using it.
        self._load_dotenv()

        # 1. the process environment, which now includes anything .env provided.
        for flat in list(self.values):
            env = self._env_name(flat)
            if env and os.environ.get(env) not in (None, ""):
                self.values[flat] = self._coerce(flat, os.environ[env])
                self.sources[flat] = f"环境变量 {env}"

        # 2. the settings file overrides the environment.
        if self.path.exists():
            try:
                # utf-8-sig：记事本、或 PowerShell 的 `Set-Content -Encoding UTF8` 写出的是
                # **带 BOM** 的 UTF-8。用 utf-8 硬读会 JSONDecodeError，于是整份设置被当成
                # 损坏、静默退回内置默认值——用户看到的是"我改的引擎没生效"，而现场只有一行
                # warning。BOM 只是个无害前缀，读进来时吃掉它就行（踩过一次）。
                data = json.loads(self.path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError) as e:
                # A corrupt settings file must not stop the software from starting -- it
                # must fall back to defaults and say so.
                print(f"[settings] {self.path.name} 读取失败，使用默认值: {e}", file=sys.stderr)
                data = {}
            for section, keys in (data or {}).items():
                if not isinstance(keys, dict):
                    continue
                for k, v in keys.items():
                    flat = f"{section}.{k}"
                    if flat in self.values:
                        self.values[flat] = self._coerce(flat, v)
                        self.sources[flat] = f"settings.json"

    def _load_dotenv(self) -> None:
        """Load config/.env and ~/.hermes/.env without overwriting a real environment var.

        ``python-dotenv`` is present (the batch pipeline uses it), but it is imported
        defensively: settings must not depend on an optional package to report what is
        configured. A missing .env is normal, not an error.
        """
        candidates = [_ROOT / "config" / ".env", Path.home() / ".hermes" / ".env"]
        try:
            from dotenv import load_dotenv

            for p in candidates:
                if p.is_file():
                    # override=False: an explicitly exported variable wins over the file.
                    load_dotenv(p, override=False)
            return
        except Exception:  # noqa: BLE001 - fall through to the hand-rolled reader
            pass
        for p in candidates:
            if not p.is_file():
                continue
            try:
                for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    k, v = k.strip(), v.split("#")[0].strip()
                    if k and k not in os.environ:
                        os.environ[k] = v
            except OSError:
                continue

    def _env_name(self, flat: str) -> str | None:
        return {
            "llm.base_url": "OPENAI_BASE_URL",
            "llm.api_key": "OPENAI_API_KEY",
            "llm.model": "LLM_MODEL",
            "llm.max_tokens": "LLM_MAX_TOKENS",
            "llm.no_think": "NO_THINK",
            "llm.timeout": "LLM_TIMEOUT",
            "llm.provider": "LLM_PROVIDER",
            "embedding.backend": "PLAUD_EMBED_BACKEND",
            "asr.engine": "PLAUD_ASR_ENGINE",
            "asr.model_dir": "PLAUD_ASR_DIR",
        }.get(flat)

    def _coerce(self, flat: str, value: Any) -> Any:
        typ = self._type_of(flat)
        try:
            if typ == "int":
                return int(value)
            if typ == "bool":
                if isinstance(value, bool):
                    return value
                return str(value).strip().lower() not in ("0", "false", "no", "off", "")
            return str(value)
        except (TypeError, ValueError):
            return self._defaults()[flat]

    def _type_of(self, flat: str) -> str:
        section, _, key = flat.partition(".")
        spec = SCHEMA.get(section, {}).get(key)
        return spec[0] if spec else "str"

    def save(self) -> None:
        """Write the settings, keeping the file readable only by the current user.

        The file may hold an API key, so on POSIX the mode is set to 0600. On Windows the
        permission model is different and this is a no-op -- stated here rather than
        silently skipped, because "we protect the key" would otherwise be an untested claim.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        out: dict[str, dict[str, Any]] = {}
        for flat, v in self.values.items():
            section, _, key = flat.partition(".")
            out.setdefault(section, {})[key] = v
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        try:
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass   # Windows: ACLs, not POSIX modes

    # ── access ──────────────────────────────────────────────────────────

    def get(self, flat: str, default: Any = None) -> Any:
        return self.values.get(flat, default)

    def set(self, flat: str, value: Any) -> None:
        if flat not in self.values:
            raise KeyError(f"未知设置项 {flat}")
        self.values[flat] = self._coerce(flat, value)
        self.sources[flat] = "已修改"

    def apply_to_env(self) -> None:
        """Push the LLM settings into the environment, which is how llm_client reads them.

        `llm_client` was written against environment variables and is used by the batch
        pipeline too, so the settings module feeds it rather than replacing it. Changing
        the LLM in the UI therefore takes effect immediately, with no restart.
        """
        mapping = {
            "llm.provider": "LLM_PROVIDER",
            "llm.base_url": "OPENAI_BASE_URL",
            "llm.api_key": "OPENAI_API_KEY",
            "llm.model": "LLM_MODEL",
            "llm.max_tokens": "LLM_MAX_TOKENS",
            "llm.timeout": "LLM_TIMEOUT",
        }
        for flat, env in mapping.items():
            v = self.values.get(flat)
            if v not in (None, ""):
                os.environ[env] = str(v)
        os.environ["NO_THINK"] = "1" if self.values.get("llm.no_think") else "0"

    # ── description for the UI ──────────────────────────────────────────

    def describe(self) -> dict:
        """Everything the settings page needs, **without any secret**.

        A key is reported as set or unset, never echoed. Returning it would put a live
        credential into every browser that opens the page and into any proxy log in
        between -- the UI only needs to know whether one exists.
        """
        groups: dict[str, list[dict]] = {}
        for section, keys in SCHEMA.items():
            rows = []
            for k, (typ, default, label, hint) in keys.items():
                flat = f"{section}.{k}"
                v = self.values.get(flat)
                if typ == "secret":
                    rows.append({"key": flat, "type": typ, "label": label, "hint": hint,
                                 "value": "", "is_set": bool(v),
                                 "default": ""})
                else:
                    rows.append({"key": flat, "type": typ, "label": label, "hint": hint,
                                 "value": v, "default": default,
                                 "changed": v != default,
                                 "source": self.sources.get(flat, "")})
            groups[section] = rows
        return {"groups": groups, "path": str(self.path),
                "exists": self.path.exists(),
                "secret_keys": sorted(SECRET_KEYS)}

    # ── health ──────────────────────────────────────────────────────────

    def check(self) -> dict:
        """Whether each back end is actually usable, by looking rather than assuming."""
        out: dict[str, Any] = {}

        asr_dir = Path(str(self.values.get("asr.model_dir") or ""))
        out["asr"] = {
            "ok": asr_dir.is_dir(),
            "detail": ("模型目录存在" if asr_dir.is_dir()
                       else f"找不到 {asr_dir}；ASR 是内置能力，缺失会导致无法转写"),
            "path": str(asr_dir),
        }

        out["embedding"] = {
            "ok": True,
            "detail": f"内置 {self.values.get('embedding.backend')}，随软件提供，无需配置",
        }

        prov = str(self.values.get("llm.provider") or "")
        if prov == "none":
            out["llm"] = {"ok": True, "detail": "已关闭，只用规则分类线索"}
        elif prov == "claude_cli":
            from shutil import which

            binp = os.environ.get("CLAUDE_BIN", "claude")
            found = which(binp)
            out["llm"] = {"ok": bool(found),
                          "detail": f"claude 命令行：{found or '未找到 ' + binp}"}
        else:
            missing = [k for k in ("base_url", "model")
                       if not str(self.values.get(f"llm.{k}") or "").strip()]
            out["llm"] = {
                "ok": not missing,
                "detail": ("配置完整" if not missing
                           else "缺少 " + "、".join(f"llm.{m}" for m in missing)),
            }
        return out


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="settings")
    ap.add_argument("--path", default=None)
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)

    s = Settings(args.path)
    if args.set:
        for item in args.set:
            k, _, v = item.partition("=")
            s.set(k.strip(), v.strip())
            print(f"  {k.strip()} = {'*' * 8 if k.strip() in SECRET_KEYS else v.strip()}")
        s.save()
        print(f"已保存到 {s.path}")
    if args.check:
        for section, r in s.check().items():
            print(f"  {'✓' if r['ok'] else '✗'} {section:<10} {r['detail']}")
    if args.show or not (args.set or args.check):
        d = s.describe()
        for section, rows in d["groups"].items():
            print(f"\n[{section}]")
            for r in rows:
                val = ("（已设置）" if r.get("is_set") else "（未设置）") \
                    if r["type"] == "secret" else r["value"]
                print(f"  {r['key']:<22} {val}")
        print(f"\n配置文件: {d['path']}  {'存在' if d['exists'] else '尚未创建'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
