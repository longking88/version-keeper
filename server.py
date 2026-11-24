# -*- coding: utf-8 -*-
"""版本助手 · 本地服务

启动: python server.py --repo <版本库目录> [--root <要监控的文件夹>] [--port 8848]
界面: http://127.0.0.1:8848
"""
import os
import sys
import json
import time
import argparse
import threading
import subprocess
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import Repo, diff_versions, human_size, save_author  # noqa: E402
import autostart  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
UI = os.path.join(BASE, "ui", "index.html")
scan_lock = threading.Lock()
RECENT = {"last_scan": None, "last_result": None}


def log(msg, level="INFO"):
    """统一日志出口：无论有没有控制台，都会同时落到 logs/app.log。"""
    try:
        print("[%s] %-5s %s" % (time.strftime("%H:%M:%S"), level, msg))
    except Exception:  # noqa: BLE001
        pass
TRAY = {"on": False, "obj": None}
HTTPD = {"obj": None}
STOP = {"ev": threading.Event()}
PORT = {"v": 8848}
QUITTING = {"v": False}
REPO = {"obj": None}          # 当前版本库，支持运行时切换（换到网络共享）
REPO_FILE = os.path.join(BASE, "repo.txt")


def get_repo():
    return REPO["obj"]


class Watcher(threading.Thread):
    def __init__(self, stop_event):
        super().__init__(daemon=True)
        self.stop_event = stop_event

    def run(self):
        while not self.stop_event.is_set():
            st = get_repo().get_settings()
            if st["auto_enabled"] and st["roots"]:
                try:
                    with scan_lock:
                        res = get_repo().scan("auto")
                    RECENT["last_scan"] = time.strftime("%H:%M:%S")
                    RECENT["last_result"] = res
                    n = (res.get("created") or 0) + (res.get("changed") or 0)
                    if n:
                        log("自动记录 %d 个文件" % n)
                except Exception as e:  # noqa: BLE001
                    RECENT["last_result"] = {"ok": False, "error": str(e)}
                    log("自动扫描出错: %s" % e, level="ERROR")
            self.stop_event.wait(max(5, st["interval"]))


def make_handler(_unused=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "VersionKeeper/1.0"

        def log_message(self, fmt, *args):
            pass

        # ---------- helpers ----------
        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False)
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json_body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode("utf-8"))
            except json.JSONDecodeError:
                return {}

        def _state(self):
            repo = get_repo()
            st = repo.get_settings()
            return {"settings": st, "stats": repo.stats(),
                    "watcher": RECENT, "repo": repo.root,
                    "network": repo.network,
                    "projects": repo.projects(),
                    "peers": repo.peers(),
                    "autostart": autostart.is_enabled(),
                    "tray": TRAY["on"],
                    "roots_exist": [{"path": r, "ok": os.path.isdir(r)} for r in st["roots"]]}

        # ---------- routes ----------
        def do_GET(self):
            repo = get_repo()
            u = urlparse(self.path)
            q = parse_qs(u.query)
            try:
                if u.path in ("/", "/index.html"):
                    with open(UI, "rb") as f:
                        return self._send(200, f.read(), "text/html; charset=utf-8")
                if u.path == "/api/state":
                    return self._send(200, self._state())
                if u.path == "/api/files":
                    return self._send(200, repo.files((q.get("q") or [""])[0] or None))
                if u.path == "/api/versions":
                    return self._send(200, repo.versions(int(q["item_id"][0])))
                if u.path == "/api/activity":
                    return self._send(200, repo.activity())
                if u.path == "/api/timeline":
                    days = q1(q, "days")
                    return self._send(200, repo.timeline(
                        limit=int(q1(q, "limit") or 400),
                        project=q1(q, "project") or None,
                        q=q1(q, "q") or None,
                        origin=q1(q, "origin") or None,
                        days=int(days) if days and days not in ("0", "") else None))
                if u.path == "/api/diff":
                    return self._send(200, diff_versions(repo, int(q["a"][0]), int(q["b"][0])))
                if u.path == "/api/pick-folder":
                    return self._send(200, {"path": pick_folder()})
                return self._send(404, {"error": "not found"})
            except Exception as e:  # noqa: BLE001
                return self._send(500, {"error": str(e)})

        def do_POST(self):
            repo = get_repo()
            u = urlparse(self.path)
            body = self._json_body()
            try:
                if u.path == "/api/snapshot":
                    with scan_lock:
                        res = repo.scan("manual", body.get("note") or None)
                    RECENT["last_scan"] = time.strftime("%H:%M:%S")
                    RECENT["last_result"] = res
                    return self._send(200, res)
                if u.path == "/api/restore":
                    return self._send(200, repo.restore(int(body["version_id"])))
                if u.path == "/api/pin":
                    return self._send(200, repo.set_pin(int(body["version_id"]), bool(body.get("pinned"))))
                if u.path == "/api/delete-version":
                    return self._send(200, repo.delete_version(int(body["version_id"])))
                if u.path == "/api/settings":
                    patch = {}
                    for k in ("auto_enabled", "interval", "max_mb", "auto_keep"):
                        if k in body:
                            patch[k] = body[k]
                    if patch:
                        repo.set_settings(patch)
                    if "author" in body:
                        # 身份只写本机 identity.json，不进共享库
                        repo.author = save_author(body["author"])
                    return self._send(200, self._state())
                if u.path == "/api/repo":
                    p = (body.get("path") or "").strip().strip('"')
                    if not p:
                        return self._send(400, {"error": "请填写版本库路径"})
                    if not os.path.isdir(p):
                        try:
                            os.makedirs(p, exist_ok=True)
                        except OSError as e:
                            return self._send(400, {"error": "无法创建或访问该目录：" + str(e)})
                    try:
                        new = Repo(p)
                    except Exception as e:  # noqa: BLE001
                        return self._send(400, {"error": "打开版本库失败：" + str(e)})
                    with scan_lock:
                        old = REPO["obj"]
                        REPO["obj"] = new
                    if old is not None:
                        try:
                            old.close()
                        except Exception:  # noqa: BLE001
                            pass
                    try:
                        with open(REPO_FILE, "w", encoding="utf-8") as f:
                            f.write(new.root)
                    except OSError:
                        pass
                    RECENT["last_result"] = None
                    return self._send(200, self._state())
                if u.path == "/api/roots":
                    act = body.get("action")
                    if act == "add":
                        p = body.get("path") or pick_folder()
                        if not p:
                            return self._send(400, {"error": "未选择文件夹"})
                        repo.add_root(p)
                    elif act == "remove":
                        repo.remove_root(body.get("path"))
                    return self._send(200, self._state())
                if u.path == "/api/autostart":
                    if body.get("enabled"):
                        autostart.enable(BASE, repo.root, PORT["v"])
                    else:
                        autostart.disable()
                    return self._send(200, self._state())
                if u.path == "/api/tray":
                    if body.get("enabled") and not TRAY["on"]:
                        start_tray(f"http://127.0.0.1:{PORT['v']}/", blocking=False)
                    elif not body.get("enabled") and TRAY["on"] and TRAY["obj"]:
                        TRAY["obj"].quit()
                    return self._send(200, self._state())
                if u.path == "/api/quit":
                    self._send(200, {"ok": True})
                    threading.Timer(0.4, shutdown_all).start()
                    return
                if u.path == "/api/open":
                    p = body.get("path")
                    if p and os.path.exists(p):
                        os.startfile(p)  # noqa: S606
                    return self._send(200, {"ok": True})
                if u.path == "/api/reveal":
                    p = body.get("path")
                    if p and os.path.exists(p):
                        subprocess.Popen(["explorer", "/select,", os.path.normpath(p)])
                    return self._send(200, {"ok": True})
                return self._send(404, {"error": "not found"})
            except Exception as e:  # noqa: BLE001
                return self._send(500, {"error": str(e)})

    return Handler


class LocalServer(ThreadingHTTPServer):
    # Windows 上 SO_REUSEADDR 会允许第二个进程抢占同一端口，必须关掉
    allow_reuse_address = False
    daemon_threads = True


def already_running(url, timeout=0.8):
    """用一次真实请求判断端口上是不是我们自己。"""
    try:
        import urllib.request
        with urllib.request.urlopen(url.rstrip("/") + "/api/state", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        return "settings" in data and "stats" in data
    except Exception:  # noqa: BLE001
        return False


def q1(q, k, default=None):
    v = q.get(k)
    return v[0] if v else default


def pick_folder():
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        p = filedialog.askdirectory(title="选择要监控的文件夹")
        root.destroy()
        return p or ""
    except Exception:  # noqa: BLE001
        return ""


def shutdown_all():
    """退出整个程序（关托盘、停服务、结束进程）。"""
    QUITTING["v"] = True
    STOP["ev"].set()
    if TRAY["obj"]:
        TRAY["obj"].quit()
    if HTTPD["obj"]:
        threading.Thread(target=HTTPD["obj"].shutdown, daemon=True).start()


def start_tray(url, blocking=False):
    """托盘可以随时开，不依赖启动参数。"""
    if TRAY["on"]:
        return
    from tray import Tray
    import webbrowser as _wb

    def do_snapshot():
        def work():
            try:
                with scan_lock:
                    res = get_repo().scan("manual", "托盘手动备份")
                n = (res.get("created") or 0) + (res.get("changed") or 0)
                tip = f"已存 {n} 个新版本" if res.get("ok") else (res.get("error") or "未执行")
                TRAY["obj"].notify("版本助手", tip)
                RECENT["last_scan"] = time.strftime("%H:%M:%S")
                RECENT["last_result"] = res
            except Exception as e:  # noqa: BLE001
                TRAY["obj"].notify("版本助手", "存版失败：" + str(e))
        threading.Thread(target=work, daemon=True).start()

    def toggle_auto():
        r = get_repo()
        st = r.get_settings()
        r.set_settings({"auto_enabled": not st["auto_enabled"]})
        on = r.get_settings()["auto_enabled"]
        TRAY["obj"].set_tip("版本助手 · " + ("自动记录中" if on else "已暂停"))
        TRAY["obj"].notify("版本助手", "自动记录已" + ("恢复" if on else "暂停"))

    def toggle_autostart():
        if autostart.is_enabled():
            autostart.disable()
            TRAY["obj"].notify("版本助手", "已关闭开机自启")
        else:
            autostart.enable(BASE, get_repo().root)
            TRAY["obj"].notify("版本助手", "已设置为开机自启")

    def open_root():
        r = get_repo()
        st = r.get_settings()
        p = st["roots"][0] if st["roots"] else r.root
        if os.path.exists(p):
            os.startfile(p)  # noqa: S606

    def quit_app():
        shutdown_all()

    cb = {
        "open_ui": lambda: _wb.open(url),
        "snapshot": do_snapshot,
        "auto_enabled": lambda: get_repo().get_settings()["auto_enabled"],
        "toggle_auto": toggle_auto,
        "autostart": autostart.is_enabled,
        "toggle_autostart": toggle_autostart,
        "open_repo": lambda: os.startfile(get_repo().root),  # noqa: S606
        "open_root": open_root,
        "quit": quit_app,
    }
    on = get_repo().get_settings()["auto_enabled"]
    t = Tray("版本助手", "版本助手 · " + ("自动记录中" if on else "已暂停"), cb)
    TRAY["obj"] = t
    TRAY["on"] = True

    def run():
        try:
            t.run()
        except Exception as e:  # noqa: BLE001
            print("托盘启动失败:", e)
        finally:
            TRAY["on"] = False

    if blocking:
        run()
    else:
        threading.Thread(target=run, daemon=True).start()


def read_repo_setting():
    """repo.txt 里写一行路径，可以指向网络共享，开机自启也跟着走。"""
    try:
        with open(REPO_FILE, encoding="utf-8") as f:
            p = f.read().strip().strip('"')
        return p or None
    except OSError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=None,
                    help="版本库目录，可以是网络共享路径 \\\\NAS\\共享\\版本库")
    ap.add_argument("--root", action="append", default=[])
    ap.add_argument("--port", type=int, default=8848)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--once", metavar="NOTE", nargs="?", const="手动存一版",
                    help="只跑一次备份然后退出（可配计划任务使用）")
    ap.add_argument("--tray", action="store_true", help="常驻托盘，不显示控制台")
    args = ap.parse_args()

    # 日志始终落盘。pythonw 下 stdout 是 None；被后台任务或计划任务拉起时，
    # stdout 可能只是一根随时会消失的管道——两种情况下出问题都无从排查。
    class _Tee:
        def __init__(self, *streams):
            self.streams = [s for s in streams if s is not None]

        def write(self, s):
            for st in self.streams:
                try:
                    st.write(s)
                except Exception:  # noqa: BLE001
                    pass
            return len(s)

        def flush(self):
            for st in self.streams:
                try:
                    st.flush()
                except Exception:  # noqa: BLE001
                    pass

        def isatty(self):
            return False

    try:
        logdir = os.path.join(BASE, "logs")
        os.makedirs(logdir, exist_ok=True)
        logf = open(os.path.join(logdir, "app.log"), "a", encoding="utf-8", buffering=1)
        sys.stdout = _Tee(sys.stdout, logf)
        sys.stderr = _Tee(sys.stderr, logf)
    except OSError:
        pass
    log("===== 启动 pid=%d 参数=%s =====" % (os.getpid(), " ".join(sys.argv[1:]) or "(无)"))

    repo_path = args.repo or read_repo_setting() or os.path.join(BASE, "repo")
    repo = Repo(repo_path)
    REPO["obj"] = repo
    # 把最终生效的库位置记下来，启动脚本和开机自启就不用各自写死一份
    try:
        with open(REPO_FILE, "w", encoding="utf-8") as f:
            f.write(repo.root)
    except OSError:
        pass
    for r in args.root:
        try:
            repo.add_root(r)
        except ValueError as e:
            print("跳过:", e)

    if args.once:
        res = repo.scan("manual", args.once)
        print(json.dumps(res, ensure_ascii=False))
        return

    url = f"http://127.0.0.1:{args.port}/"
    PORT["v"] = args.port

    if already_running(url):
        print("版本助手已在运行，直接打开界面:", url)
        webbrowser.open(url)
        return

    try:
        srv = LocalServer(("127.0.0.1", args.port), make_handler())
    except OSError as e:
        print("端口 %d 被其他程序占用：%s" % (args.port, e))
        print("可以用 --port 换一个端口重试")
        return

    HTTPD["obj"] = srv
    stop = STOP["ev"]
    Watcher(stop).start()
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    log("版本助手已启动: %s" % url)
    log("版本库: %s %s" % (repo.root, "(网络共享)" if repo.network else "(本机)"))
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    # 记下退出原因：日志里有启动没退出，说明是被强杀的，不是自己走的
    exit_reason = "services 停止"
    try:
        if args.tray:
            start_tray(url, blocking=True)
            if not QUITTING["v"]:
                # 只是把托盘图标关了，程序继续在后台服务
                log("托盘已关闭，服务继续运行在 %s" % url)
                exit_reason = "托盘关闭后主循环结束"
                while not stop.wait(0.5):
                    pass
        else:
            while not stop.wait(0.5):
                pass
    except KeyboardInterrupt:
        exit_reason = "收到中断信号 (Ctrl+C / CTRL_BREAK)"
    finally:
        stop.set()
        try:
            srv.shutdown()
        except Exception:  # noqa: BLE001
            pass
        log("已停止 pid=%d 原因=%s" % (os.getpid(), exit_reason))


if __name__ == "__main__":
    main()
