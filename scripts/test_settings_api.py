"""Verify the settings API: a change applies, and a blank secret does not wipe a key.

Two behaviours, both of which fail silently if wrong:

* **A blank secret must mean "leave it alone".** The password field is rendered empty on
  purpose (a key is never sent to the browser), so saving the form without touching that
  field submits "". Treating that as "clear the key" would silently destroy a working
  credential every time the user edited an unrelated setting -- and the failure would not
  appear until the LLM was next needed.
* **A change must take effect without a restart.** The LLM settings are pushed into the
  environment, which is how ``llm_client`` reads them; if the API saved the file but did not
  apply, the UI would report success while the running process kept using the old endpoint.

Usage:
    python scripts/test_settings_api.py [--port 8510]
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

ROOT = HERE.parent


def call(port: int, path: str, payload: dict | None = None):
    if payload is None:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=15) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8510)
    args = ap.parse_args()
    fails = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal fails
        if not ok:
            fails += 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))

    print("=" * 74)
    print("设置 API")
    print("=" * 74)

    code, d = call(args.port, "/api/settings")
    check("GET 返回 200", code == 200, str(code))
    groups = (d.get("settings") or {}).get("groups") or {}
    rows = {r["key"]: r for g in groups.values() for r in g}
    check("覆盖三个后端", {"asr.model_dir", "embedding.backend", "llm.model"} <= set(rows),
          str(sorted(rows)[:4]))

    # ── secrets never travel ────────────────────────────────────────────
    print("\n密钥保护：")
    key_row = rows.get("llm.api_key") or {}
    check("api_key 的 value 为空", key_row.get("value", None) == "", repr(key_row.get("value")))
    check("api_key 报告 is_set 状态", "is_set" in key_row, str(key_row.get("is_set")))
    raw = json.dumps(d, ensure_ascii=False)
    check("响应体不含密钥明文", "sk-" not in raw)

    # ── a blank secret must not clear the key ───────────────────────────
    print("\n空密钥不应覆盖已有密钥：")
    before_set = key_row.get("is_set")
    if not before_set:
        print("    （当前没有密钥，跳过这条；先设置一个再测）")
    else:
        code, r = call(args.port, "/api/settings", {"set": {"llm.api_key": ""}})
        check("提交空密钥返回 200", code == 200, str(code))
        changed_keys = [c["key"] for c in (r.get("changed") or [])]
        check("空密钥未被记为变更", "llm.api_key" not in changed_keys, str(changed_keys))
        code, d2 = call(args.port, "/api/settings")
        row2 = {r["key"]: r for g in (d2["settings"]["groups"]).values() for r in g}["llm.api_key"]
        check("密钥仍然存在", row2.get("is_set") is True, str(row2.get("is_set")))

    # ── a real change applies immediately ───────────────────────────────
    print("\n改动即时生效：")
    code, d = call(args.port, "/api/settings")
    old_timeout = {r["key"]: r for g in d["settings"]["groups"].values() for r in g}["llm.timeout"]["value"]
    new_timeout = 111 if str(old_timeout) != "111" else 222
    code, r = call(args.port, "/api/settings", {"set": {"llm.timeout": new_timeout}})
    check("保存返回 200", code == 200, str(code))
    check("报告了变更项",
          any(c["key"] == "llm.timeout" for c in (r.get("changed") or [])),
          str([c["key"] for c in (r.get("changed") or [])]))
    code, d3 = call(args.port, "/api/settings")
    now = {r["key"]: r for g in d3["settings"]["groups"].values() for r in g}["llm.timeout"]["value"]
    check("新值已生效", str(now) == str(new_timeout), f"{now} (期望 {new_timeout})")

    # ── unknown keys are rejected, not ignored ──────────────────────────
    code, r = call(args.port, "/api/settings", {"set": {"llm.nonexistent": "x"}})
    check("未知键被拒绝", bool(r.get("errors")), str(r.get("errors"))[:60])

    # ── restore ─────────────────────────────────────────────────────────
    call(args.port, "/api/settings", {"set": {"llm.timeout": old_timeout}})
    print(f"\n（已把 llm.timeout 恢复为 {old_timeout}）")

    print(f"\n{'全部通过' if fails == 0 else f'{fails} 处失败'}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
