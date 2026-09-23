"""实时会议助理 · 原生启动器

打开即用：窗口先显示一个加载页，后台把 meeting.server 拉起来（本机麦克风 +
settings.json 里配置的 ASR 引擎 + 公共库/嵌入模型），服务一健康就自动切到会议界面。
没有首页、不需要点"启动"，也没有命令行窗口。

进程模型：
    MeetingLauncher.exe (pywebview, 无 torch, 无控制台)
        └─ 子进程（CREATE_NO_WINDOW，看不见窗口）:
               venv python -m meeting.server --port P --managed
               （UI + 音频 + ASR + 检索 单进程；ASR 模型与嵌入模型都在这里面）

几条刻意的约定：

* **关窗 = 停服务**：ASR 模型和嵌入模型都活在子进程里，窗口一关就把子进程终止，
  不会留下占着显存的后台进程。
* **不替用户改配置**：ASR 引擎、模型目录、知识库路径都只从 config/settings.json 读。
  要换引擎就去会议界面的「设置」页改，改完点那里的「重启服务」——界面写一个标记文件
  （data/launcher/restart.flag），本进程看到就把子进程重启一遍。
* **端口自己找**：8510 被占就顺延试下一个，因为界面上没有地方让你改端口了。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path

import webview

DEFAULT_PORT = 8510
PORT_TRIES = 8
# 界面「重启服务」写的标记文件。路径与 meeting/server.py 的 /api/restart 必须一致。
RESTART_FLAG = Path("data") / "launcher" / "restart.flag"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def _port_free(port: int) -> bool:
    """端口能不能立刻绑上（严格检查，不带 SO_REUSEADDR）。

    注意：Windows 上 TIME_WAIT 残留也会让严格检查失败，所以它**不等于**"有服务在跑"——
    区分办法见 `_port_answered`。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _port_answered(port: int) -> bool:
    """这个端口上真的有服务在应答吗？（TIME_WAIT 残留不会有应答）

    会议服务用的是 http.server 的 ThreadingHTTPServer，它 bind 时带 SO_REUSEADDR，
    所以"TIME_WAIT 残留"并不妨碍它重新绑定同一个端口——只有**别的服务真在监听**才需要
    顺延端口。
    """
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/stats", timeout=1) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def _exe_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def _resource_dir() -> Path:
    """launcher.html 等资源所在目录：冻结态用 _MEIPASS，开发态用本文件目录。"""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", None)
        if base:
            return Path(base)
    return Path(__file__).resolve().parent


def _resolve_root() -> Path:
    """定位 meeting-assistant 仓库根：含 venv/ 的目录。

    顺序：launcher.json 显式 repo_root → 开发模式 __file__/../ → exe 目录 → exe 上一级。
    """
    def _from_cfg(base: Path):
        cfg = base / "launcher.json"
        if cfg.exists():
            try:
                # utf-8-sig：Windows 记事本 / PowerShell 的 Set-Content 都会写出带 BOM 的
                # UTF-8，用 utf-8 硬读会直接 JSONDecodeError，于是"配置文件明明在"却
                # 报找不到仓库根（踩过一次）。
                r = (json.loads(cfg.read_text(encoding="utf-8-sig")) or {}).get("repo_root")
                if r:
                    p = Path(r).expanduser().resolve()
                    if (p / "venv").is_dir():
                        return p
            except Exception:
                pass
        return None

    for base in (_exe_dir(), Path.cwd()):
        r = _from_cfg(base)
        if r:
            return r

    frozen = getattr(sys, "frozen", False)
    if not frozen:
        # 开发模式：本文件在 launcher/ 下，root = 上一级
        dev = Path(__file__).resolve().parent.parent
        if (dev / "venv").is_dir():
            return dev
    # 冻结模式：exe 目录 / 上一级（把 MeetingLauncher 文件夹放到仓库根内即可命中）
    for cand in (_exe_dir(), _exe_dir().parent):
        if (cand / "venv").is_dir():
            return cand
    if not frozen:
        return Path(__file__).resolve().parent.parent
    raise SystemExit(
        "找不到仓库根（含 venv/ 的目录）。\n"
        "请把 MeetingLauncher 文件夹放到 meeting-assistant 仓库根内，\n"
        "或在 exe 旁放 launcher.json：{\"repo_root\": \"<仓库根路径>\"}"
    )


class Backend:
    """管理 meeting.server 子进程：拉起、健康探测、重启、终止。"""

    def __init__(self, root: Path):
        self.root = root
        self.venv_py = root / "venv" / "Scripts" / "python.exe"
        self.scripts = root / "scripts"
        self.config = root / "config" / "settings.json"
        self.logpath = root / "data" / "launcher" / "backend.log"
        self.flag = root / RESTART_FLAG

        self.proc: subprocess.Popen | None = None
        self.port = DEFAULT_PORT
        self.state = "stopped"          # stopped | starting | running
        self.healthy = False
        self.error = ""
        self.log: deque = deque(maxlen=400)
        self._lock = threading.RLock()
        self._stop_evt = threading.Event()
        self._logfh = None

    # -- 配置（只读） ---------------------------------------------------------
    def _note(self, text: str) -> None:
        """启动器自己的一句话：既进内存（加载页显示），也写进 backend.log。

        冻结版是无控制台的，print 等于没写；启动器侧的事件（重启、起不来）只有落到
        日志文件里，出问题时才查得到。
        """
        self.log.append(text)
        fh = self._logfh
        if fh is not None:
            try:
                fh.write(text + "\n")
            except OSError:
                pass

    def engine(self) -> str:
        """当前配置的 ASR 引擎名，只用于在加载页上显示进度文案。"""
        try:
            data = json.loads(self.config.read_text(encoding="utf-8")) or {}
            return str((data.get("asr") or {}).get("engine") or "funasr")
        except Exception:  # noqa: BLE001
            return "funasr"

    def _free_port(self, prefer: int | None = None) -> int:
        """挑一个能用的端口：优先 prefer/8510，真有服务占着才顺延。"""
        start = prefer or DEFAULT_PORT
        if _port_free(start) or not _port_answered(start):
            return start
        for p in range(DEFAULT_PORT, DEFAULT_PORT + PORT_TRIES):
            if p == start:
                continue
            if _port_free(p) or not _port_answered(p):
                return p
        return start

    # -- 生命周期 ------------------------------------------------------------
    def start(self, port: int | None = None) -> dict:
        with self._lock:
            if self._alive():
                return self.status()
            self.port = self._free_port(port)
            self.state = "starting"
            self.healthy = False
            self.error = ""
            self.log.clear()
            self._stop_evt.clear()
            try:                       # 清掉上一轮遗留的重启标记
                self.flag.unlink()
            except OSError:
                pass

            args = [str(self.venv_py), "-u", "-m", "meeting.server",
                    "--port", str(self.port), "--managed"]
            try:
                self.logpath.parent.mkdir(parents=True, exist_ok=True)
                self._logfh = open(self.logpath, "a", encoding="utf-8", buffering=1)
                self._logfh.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} "
                                  f"start port={self.port} =====\n")
            except OSError:
                self._logfh = None
            self._note("$ python " + " ".join(args[1:]))
            try:
                self.proc = subprocess.Popen(
                    args, cwd=str(self.scripts),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1,
                    # 没有它，GUI 父进程拉起的 python.exe 会自己开一个控制台窗口
                    creationflags=CREATE_NO_WINDOW,
                )
            except Exception as e:  # noqa: BLE001
                self.state = "stopped"
                self.error = f"无法启动子进程：{e}"
                self._note(self.error)
                return self.status()

            proc = self.proc
            for fn in (self._reader, self._poll, self._watch_flag):
                threading.Thread(target=fn, args=(proc,), daemon=True).start()
            return self.status()

    def _alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> dict:
        """终止子进程（ASR 模型、嵌入模型都随之退出，不留后台占用）。"""
        self._stop_evt.set()
        proc = self.proc
        self.proc = None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()          # Windows 上就是 TerminateProcess，立即生效
                proc.wait(timeout=8)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        with self._lock:
            self.state = "stopped"
            self.healthy = False
        return self.status()

    def restart(self, reason: str = "") -> dict:
        """重启后端：界面上的「重启服务」走这里（引擎 / 知识库改完要重新加载）。"""
        port = self.port
        if reason:
            self._note(f"[launcher] 重启：{reason}")
        self.stop()
        # 等旧服务不再应答（TerminateProcess 之后通常瞬间），然后用**同一个端口**起来：
        # 页面还开着，端口一变窗口里的界面就失联了。端口上若只剩 TIME_WAIT 残留不影响
        # ——会议服务 bind 带 SO_REUSEADDR，能重新绑上。
        for _ in range(50):
            if not _port_answered(port):
                break
            time.sleep(0.1)
        self._stop_evt.clear()
        return self.start(port=port)

    # -- 线程 ----------------------------------------------------------------
    def _reader(self, proc: subprocess.Popen) -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip()
                if not line:
                    continue
                self.log.append(line)
                if self._logfh is not None:
                    try:
                        self._logfh.write(line + "\n")
                    except OSError:
                        pass
        except Exception:  # noqa: BLE001
            pass
        finally:
            if self._logfh is not None:
                try:
                    self._logfh.close()
                except OSError:
                    pass
                self._logfh = None
            try:
                proc.wait(timeout=2)
            except Exception:  # noqa: BLE001
                pass
            # 只有"现在这一轮"的进程才允许改状态：重启时的旧进程退出不算失败
            if self.proc is proc:
                with self._lock:
                    self.state = "stopped"
                    self.healthy = False
                code = proc.returncode
                if code not in (0, None):
                    last = self.log[-1] if self.log else ""
                    self.error = (f"服务异常退出（code {code}）"
                                  + (f"：{last}" if last else "，见日志"))

    def _poll(self, proc: subprocess.Popen) -> None:
        url = f"http://127.0.0.1:{self.port}/api/stats"
        while not self._stop_evt.is_set() and self.proc is proc:
            ok = False
            try:
                with urllib.request.urlopen(url, timeout=2) as r:
                    ok = r.status == 200
            except Exception:  # noqa: BLE001
                ok = False
            if self.proc is not proc:
                return
            self.healthy = ok
            if ok and self.state == "starting":
                self.state = "running"
            time.sleep(1.0)

    def _watch_flag(self, proc: subprocess.Popen) -> None:
        """界面点了「重启服务」会留下标记文件；看到就重启（不静默打断，是用户点的）。"""
        while not self._stop_evt.is_set() and self.proc is proc:
            if self.flag.exists():
                try:
                    self.flag.unlink()
                except OSError:
                    pass
                self._note("[launcher] 收到重启请求（界面「重启服务」）")
                self.restart("界面请求")
                return
            time.sleep(1.0)

    # -- 状态 ----------------------------------------------------------------
    def status(self) -> dict:
        return {
            "state": self.state,
            "healthy": self.healthy,
            "ready": self.state == "running" and self.healthy,
            "port": self.port,
            "url": f"http://127.0.0.1:{self.port}/",
            "pid": (self.proc.pid if self.proc else None),
            "alive": self._alive(),
            "error": self.error,
            "engine": self.engine(),
            "log": list(self.log)[-10:],
        }


class API:
    """暴露给加载页 JS 的方法（window.pywebview.api.*）。"""

    def __init__(self, backend: Backend):
        self.backend = backend

    def get_status(self) -> dict:
        st = self.backend.status()
        st["venv_ok"] = self.backend.venv_py.exists()
        st["venv"] = str(self.backend.venv_py)
        st["log_path"] = str(self.backend.logpath)
        return st

    def restart(self) -> dict:
        return self.backend.restart("加载页点了重试")

    def open_log(self) -> bool:
        """用系统默认程序打开后端日志（排查用，只有用户点了才会开）。"""
        try:
            os.startfile(str(self.backend.logpath))  # noqa: S606 - 用户主动点开日志
            return True
        except Exception:  # noqa: BLE001
            return False


def _fatal(message: str) -> None:
    """启动期致命错误的可见提示。

    冻结版是 --windowed 的：没有控制台，异常打到 stderr 就等于什么都没说，用户只看到
    "双击没反应"。所以这里用系统消息框把原因摆出来（找不到仓库根、venv 解释器缺失这类
    问题都只会在这时候出现）。
    """
    try:
        print(message, file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "实时会议助理 · 启动失败", 0x10)
    except Exception:  # noqa: BLE001
        pass


def _navigator(window, backend: "Backend") -> None:
    """等后端就绪 → 把窗口切到会议界面 → 再显示窗口（"打开直接进主界面"由这里负责）。

    窗口是 **hidden 创建**的，所以正常情况（实测 2.6~4s 就绪）用户第一眼看到的就是会议
    界面，没有加载页；只有"起得慢"或"起不来"时才会把窗口亮出来——那时候加载页上的进度、
    日志和重试按钮才有意义。

    **跨线程调 pywebview 是安全的**：winforms 后端的 load_url / show 内部走 self.Invoke，
    会把真正的 GUI 调用排到 UI 线程上（见 pywebview/platforms/winforms.py）。
    之所以不让加载页自己 location.replace：那样"能不能进主界面"就取决于 JS 桥是否注入
    成功，而这条链路我们控制不了；Python 这边只要状态是 ready 就一定切过去。

    重启（设置页的「重启服务」，端口可能变）后再 ready 时也会再切一次。
    """
    show_after = 8.0        # 超过这么久还没就绪，先把加载页亮出来（否则双击之后一片安静）
    hard_timeout = 900.0
    t0 = time.time()
    shown = False
    last = ""

    def _show() -> None:
        nonlocal shown
        if shown:
            return
        shown = True
        try:
            window.show()
        except Exception as e:  # noqa: BLE001
            print(f"[launcher] 显示窗口失败：{type(e).__name__}: {e}", flush=True)

    while time.time() - t0 < hard_timeout:
        st = backend.status()
        if st["ready"] and st["url"] != last:
            url = st["url"]
            try:
                window.load_url(url)
                # 等这一页真的画出来再显示，免得先闪一下加载页。
                window.events.loaded.wait(timeout=15)
                _show()
                last = url
                print(f"[launcher] 服务就绪，窗口已切到 {url}", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"[launcher] 切换窗口失败：{type(e).__name__}: {e}", flush=True)
                _show()
        elif st["error"]:
            _show()             # 起不来：把加载页亮出来，上面写着错误和最后几行日志
            return
        elif time.time() - t0 > show_after:
            _show()
        time.sleep(0.4)
    _show()


def main() -> None:
    try:
        root = _resolve_root()
    except SystemExit as e:
        _fatal(str(e) or "找不到仓库根（含 venv/ 的目录）")
        return
    backend = Backend(root)
    if not backend.venv_py.exists():
        _fatal(f"找不到 venv 解释器：{backend.venv_py}\n"
               "启动器需要 meeting-assistant 仓库里的 venv 来跑 meeting.server。")
        return
    api = API(backend)
    html_path = _resource_dir() / "launcher.html"
    html_content = html_path.read_text(encoding="utf-8")
    window = webview.create_window(
        "实时会议助理",
        html=html_content,
        width=1360, height=880, min_size=(960, 640),
        background_color="#0f1216",
        js_api=api,
        # 先不显示：就绪后由 _navigator 直接切到会议界面再显示（见那里的说明）。
        # launcher.html 只在"起得慢 / 起不来"时才会被看到。
        hidden=True,
    )

    def _on_closing() -> None:
        # 关窗 = 停服务：ASR 模型与嵌入模型都在子进程里，必须一起带走
        backend.stop()

    window.events.closing += _on_closing
    backend.start()
    threading.Thread(target=_navigator, args=(window, backend), daemon=True).start()
    try:
        webview.start()
    finally:
        backend.stop()


if __name__ == "__main__":
    main()
