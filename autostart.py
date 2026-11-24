# -*- coding: utf-8 -*-
"""版本助手 · 开机自启（写 HKCU 的 Run 键，不需要管理员权限）"""
import os
import sys
import winreg

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "VersionKeeper"


def _pythonw():
    """优先用 pythonw.exe，开机时不弹黑框。

    尽量避开临时/随工具升级会变动的解释器目录，否则自启项可能哪天就失效了。
    """
    if getattr(sys, "frozen", False):
        return sys.executable
    exe = sys.executable
    local = os.environ.get("LOCALAPPDATA") or ""
    # 1) 官方安装包默认位置，版本号从高到低
    root = os.path.join(local, "Programs", "Python")
    if os.path.isdir(root):
        cands = []
        for name in os.listdir(root):
            pw = os.path.join(root, name, "pythonw.exe")
            if os.path.exists(pw):
                try:
                    ver = tuple(int(x) for x in name.replace("Python", "").split("."))
                except ValueError:
                    ver = (0,)
                cands.append((ver, pw))
        if cands:
            return sorted(cands, reverse=True)[0][1]
    # 2) Program Files 下的安装
    for p in (r"C:\Program Files\python\pythonw.exe",):
        if os.path.exists(p):
            return p
    # 3) 当前解释器旁边
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return cand if os.path.exists(cand) else exe


def build_cmd(base_dir, repo_path=None, port=8848):
    """不写死 --repo：库位置统一由 repo.txt 决定。

    否则在界面里切到共享库后，开机自启还会把程序拉回旧的本地库。
    """
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --port {port} --tray --no-browser'
    return (f'"{_pythonw()}" "{os.path.join(base_dir, "server.py")}"'
            f' --port {port} --tray --no-browser')


def current():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_QUERY_VALUE) as k:
            val, _ = winreg.QueryValueEx(k, VALUE_NAME)
            return val
    except FileNotFoundError:
        return None
    except OSError:
        return None


def is_enabled():
    val = current()
    return bool(val and "server.py" in val.lower()) or bool(val and getattr(sys, "frozen", False))


def enable(base_dir, repo_path, port=8848):
    cmd = build_cmd(base_dir, repo_path, port)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, VALUE_NAME, 0, winreg.REG_SZ, cmd)
    return cmd


def disable():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, VALUE_NAME)
    except FileNotFoundError:
        pass


if __name__ == "__main__":
    base = os.path.dirname(os.path.abspath(__file__))
    print("当前:", current())
    print("将要写入:", build_cmd(base, os.path.join(base, "repo")))
