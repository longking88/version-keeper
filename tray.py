# -*- coding: utf-8 -*-
"""版本助手 · 系统托盘（纯 Win32 API，无第三方依赖）

用 Shell_NotifyIcon 在通知区域挂一个图标，右键出菜单。
"""
import os
import sys
import ctypes
import ctypes.wintypes as wt

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_DESTROY = 0x0002
WM_NULL = 0x0000
WM_COMMAND = 0x0111
WM_CONTEXTMENU = 0x007B
WM_RBUTTONUP = 0x0205
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
MF_STRING, MF_SEPARATOR, MF_CHECKED, MF_GRAYED = 0x0000, 0x0800, 0x0008, 0x0001
TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_NONOTIFY = 0x0002, 0x0100, 0x0080
IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x0010, 0x0040

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASS(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


class NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("hWnd", wt.HWND),
        ("uID", wt.UINT),
        ("uFlags", wt.UINT),
        ("uCallbackMessage", wt.UINT),
        ("hIcon", wt.HICON),
        ("szTip", wt.WCHAR * 128),
        ("dwState", wt.DWORD),
        ("dwStateMask", wt.DWORD),
        ("szInfo", wt.WCHAR * 256),
        ("uVersion", wt.UINT),
        ("szInfoTitle", wt.WCHAR * 64),
        ("dwInfoFlags", wt.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wt.HICON),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", wt.LONG), ("y", wt.LONG)]


class MSG(ctypes.Structure):
    _fields_ = [("hWnd", wt.HWND), ("message", wt.UINT), ("wParam", wt.WPARAM),
                ("lParam", wt.LPARAM), ("time", wt.DWORD), ("pt", POINT)]


user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU,
                                   wt.HINSTANCE, wt.LPVOID]
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
user32.LoadImageW.restype = wt.HANDLE
user32.CreatePopupMenu.restype = wt.HMENU
user32.TrackPopupMenu.restype = ctypes.c_int
user32.TrackPopupMenu.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, wt.HWND, wt.LPVOID]
shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATA)]
kernel32.GetModuleHandleW.restype = wt.HMODULE


# ---------------- 图标生成 ----------------
def _render_bgra(size, ssp=4):
    """画一个蓝色圆角方块 + 三条白色横杠（版本堆叠），4 倍超采样。"""
    S = size * ssp
    # 单位坐标转像素
    def bar(x0, y0, x1, y1):
        return (x0 * S, y0 * S, x1 * S, y1 * S)

    bars = [bar(0.22, 0.24, 0.78, 0.335), bar(0.22, 0.435, 0.78, 0.53),
            bar(0.22, 0.63, 0.62, 0.725)]
    r = S * 0.235
    px = bytearray(S * S * 4)
    for y in range(S):
        for x in range(S):
            # 圆角矩形判定
            cx = min(max(x + 0.5, r), S - r)
            cy = min(max(y + 0.5, r), S - r)
            inside = (x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2 <= r * r
            if not inside:
                continue
            col = (165, 95, 24, 255)  # BGRA, 即 #185FA5
            for (bx0, by0, bx1, by1) in bars:
                if bx0 <= x < bx1 and by0 <= y < by1:
                    col = (255, 255, 255, 255)
                    break
            i = (y * S + x) * 4
            px[i:i + 4] = bytes(col)

    # 下采样（预乘 alpha 平均，边缘更顺）
    out = bytearray(size * size * 4)
    n = ssp * ssp
    for y in range(size):
        for x in range(size):
            b = g = rr = a = 0
            for dy in range(ssp):
                for dx in range(ssp):
                    i = ((y * ssp + dy) * S + (x * ssp + dx)) * 4
                    pa = px[i + 3]
                    b += px[i] * pa
                    g += px[i + 1] * pa
                    rr += px[i + 2] * pa
                    a += pa
            o = (y * size + x) * 4
            if a:
                out[o] = min(255, b // a)
                out[o + 1] = min(255, g // a)
                out[o + 2] = min(255, rr // a)
                out[o + 3] = a // n
    return bytes(out)


def make_ico(path, sizes=(16, 32, 48)):
    import struct
    images = []
    for s in sizes:
        bgra = _render_bgra(s)
        rows = []
        for y in range(s - 1, -1, -1):  # BMP 自下而上
            rows.append(bgra[y * s * 4:(y + 1) * s * 4])
        xor = b"".join(rows)
        mask = b"\x00" * (((s + 31) // 32) * 4 * s)
        header = struct.pack("<IiiHHIIiiII", 40, s, s * 2, 1, 32, 0,
                             len(xor) + len(mask), 0, 0, 0, 0)
        images.append((s, header + xor + mask))
    data = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = b""
    for s, blob in images:
        entries += struct.pack("<BBBBHHII", s if s < 256 else 0, s if s < 256 else 0,
                               0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data + entries + b"".join(b for _, b in images))
    return path


# ---------------- 托盘 ----------------
class Tray:
    """callbacks: dict(open_ui=fn, snapshot=fn, auto_enabled=fn->bool, toggle_auto=fn,
                      autostart=fn->bool, toggle_autostart=fn, open_repo=fn, open_root=fn,
                      quit=fn)"""

    def __init__(self, title, tip, callbacks, icon_path=None):
        self.title = title
        self.tip = tip
        self.cb = callbacks
        self.icon_path = icon_path or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                   "assets", "tray.ico")
        if not os.path.exists(self.icon_path):
            make_ico(self.icon_path)
        self._proc = WNDPROC(self._wndproc)
        self.hwnd = None
        self.nid = None
        self.hicon = None
        self._class_name = "VersionKeeperTrayWnd"

    def _create_window(self):
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASS()
        wc.style = 0
        wc.lpfnWndProc = self._proc
        wc.cbClsExtra = 0
        wc.cbWndExtra = 0
        wc.hInstance = hinst
        wc.hIcon = None
        wc.hCursor = None
        wc.hbrBackground = None
        wc.lpszMenuName = None
        wc.lpszClassName = self._class_name
        user32.RegisterClassW(ctypes.byref(wc))
        self.hwnd = user32.CreateWindowExW(0, self._class_name, self.title, 0, 0, 0, 0, 0,
                                           None, None, hinst, None)
        if not self.hwnd:
            raise OSError("创建托盘窗口失败: %s" % ctypes.get_last_error())

    def _add_icon(self):
        self.hicon = user32.LoadImageW(None, self.icon_path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE)
        nid = NOTIFYICONDATA()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATA)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self.hicon
        nid.szTip = self.tip[:127]
        self.nid = nid
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            raise OSError("托盘图标注册失败")

    def notify(self, title, msg, info_flags=0x01):
        if not self.nid:
            return
        nid = NOTIFYICONDATA()
        ctypes.pointer(nid)[0] = ctypes.pointer(self.nid)[0]
        nid.uFlags = NIF_INFO
        nid.szInfo = (msg or "")[:255]
        nid.szInfoTitle = (title or "")[:63]
        nid.dwInfoFlags = info_flags
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def set_tip(self, tip):
        if not self.nid:
            return
        self.tip = tip[:127]
        nid = NOTIFYICONDATA()
        ctypes.pointer(nid)[0] = ctypes.pointer(self.nid)[0]
        nid.uFlags = NIF_TIP
        nid.szTip = self.tip
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def _menu(self):
        MF = MF_STRING
        h = user32.CreatePopupMenu()
        auto_on = bool(self.cb.get("auto_enabled", lambda: True)())
        start_on = bool(self.cb.get("autostart", lambda: False)())
        user32.AppendMenuW(h, MF, 1, "打开界面")
        user32.AppendMenuW(h, MF, 2, "立即存一版")
        user32.AppendMenuW(h, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(h, MF | (MF_CHECKED if auto_on else 0), 3, "自动记录")
        user32.AppendMenuW(h, MF | (MF_CHECKED if start_on else 0), 4, "开机自动启动")
        user32.AppendMenuW(h, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(h, MF, 5, "打开版本库目录")
        user32.AppendMenuW(h, MF, 6, "打开监控文件夹")
        user32.AppendMenuW(h, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(h, MF, 9, "退出")
        return h

    def _show_menu(self):
        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        h = self._menu()
        user32.SetForegroundWindow(self.hwnd)
        cmd = user32.TrackPopupMenu(h, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                                    pt.x, pt.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(h)
        if cmd:
            self._command(cmd)

    def _command(self, cmd):
        if cmd == 1:
            self.cb["open_ui"]()
        elif cmd == 2:
            self.cb["snapshot"]()
        elif cmd == 3:
            self.cb["toggle_auto"]()
        elif cmd == 4:
            self.cb["toggle_autostart"]()
        elif cmd == 5:
            self.cb["open_repo"]()
        elif cmd == 6:
            self.cb["open_root"]()
        elif cmd == 9:
            self.cb["quit"]()

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAY:
            ev = lparam & 0xFFFF
            if ev in (WM_RBUTTONUP, WM_CONTEXTMENU):
                self._show_menu()
                return 0
            if ev == WM_LBUTTONDBLCLK:
                self.cb["open_ui"]()
                return 0
        elif msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def run(self):
        self._create_window()
        self._add_icon()
        msg = MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            if self.nid:
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self.nid))
                self.nid = None
            if self.hicon:
                user32.DestroyIcon(self.hicon)
                self.hicon = None
            if self.hwnd:
                if user32.IsWindow(self.hwnd):
                    user32.DestroyWindow(self.hwnd)
                self.hwnd = None

    def quit(self):
        # WM_CLOSE → DefWindowProc 销毁窗口 → WM_DESTROY → 结束消息循环
        if self.hwnd:
            user32.PostMessageW(self.hwnd, 0x0010, 0, 0)


if __name__ == "__main__":
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "tray.ico")
    print(make_ico(p), os.path.getsize(p), "bytes")
