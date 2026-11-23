"""
SmoothRecord - a tiny, fast screen recorder for Windows 11.

Capture:  Desktop Duplication API (ddagrab) -> GPU scaling -> hardware encoder
          (Intel QSV / NVIDIA NVENC / AMD AMF), with automatic CPU fallback.
Needs:    Python 3.10+, FFmpeg (with ddagrab), optional `pip install soundcard`
          for system-audio capture.

Run:      double-click SmoothRecord.pyw   (or: pythonw SmoothRecord.pyw)
"""
import collections
import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import warnings
from datetime import datetime

# ---- DPI awareness must be set before Tk starts ---------------------------------
try:
    if not ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
        raise OSError
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox

warnings.filterwarnings("ignore")  # soundcard emits harmless "data discontinuity" warnings

APP = "SmoothRecord"
APP_VERSION = "1.0.0"
APP_AUTHOR = "Anant Rastogi"
CFG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP)
CFG_PATH = os.path.join(CFG_DIR, "config.json")
LOG_PATH = os.path.join(CFG_DIR, "ffmpeg.log")
NOWIN = 0x08000000  # CREATE_NO_WINDOW

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
dwmapi = ctypes.windll.dwmapi

user32.GetAncestor.restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.WindowFromPoint.restype = wt.HWND
user32.WindowFromPoint.argtypes = [wt.POINT]
user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
user32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_long]
user32.SetWindowDisplayAffinity.argtypes = [wt.HWND, wt.DWORD]
user32.EnumDisplaySettingsW.argtypes = [wt.LPCWSTR, wt.DWORD, ctypes.c_void_p]
user32.GetMonitorInfoW.argtypes = [wt.HMONITOR, ctypes.c_void_p]
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
user32.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]
user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
dwmapi.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]
dwmapi.DwmSetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]

# ---- colours ------------------------------------------------------------------------
BG, BG2, BORDER = "#18181b", "#26262b", "#34343a"
FG, FG_DIM = "#ececef", "#8b8b93"
RED, RED_DIM = "#ef4444", "#7f1d1d"
ACCENT = "#ef4444"

# ---- bar renderer (anti-aliased, via Pillow; falls back to plain Tk drawing) -------
try:
    from PIL import Image, ImageDraw, ImageFont, ImageTk
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False

BAR_W, BAR_H = 352, 54
# key -> logical centre x
BAR_BTNS = {"rec": 44, "pause": 82, "shot": 262, "settings": 291, "min": 313, "close": 336}
BAR_TEXT_X0, BAR_TEXT_X1 = 114, 240
BAR_DIVIDERS = (104, 246)
_FONT_FILES = {"ui": ["segoeui.ttf"], "semi": ["seguisb.ttf", "segoeui.ttf"],
               "icons": ["SegoeIcons.ttf", "segmdl2.ttf"]}
_font_cache = {}


def pil_font(kind, px):
    key = (kind, px)
    if key not in _font_cache:
        fdir = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
        for name in _FONT_FILES[kind]:
            p = os.path.join(fdir, name)
            if os.path.exists(p):
                _font_cache[key] = ImageFont.truetype(p, px)
                break
        else:
            _font_cache[key] = ImageFont.load_default()
    return _font_cache[key]


def render_bar(sp):
    """sp: dict(S, state, hover, pressed, timer, status, blink). Returns a PIL image."""
    SS = 3                      # supersampling factor -> smooth edges
    k = sp["S"] * SS
    u = lambda v: round(v * k)
    img = Image.new("RGB", (u(BAR_W), u(BAR_H)), BG)
    d = ImageDraw.Draw(img)
    cy = u(BAR_H / 2)
    st = sp["state"]
    d.rounded_rectangle([0, 0, img.width - 1, img.height - 1], radius=u(9), fill=BG,
                        outline=BORDER, width=max(1, u(1)))
    # grip
    for i in (-1, 0, 1):
        for j in (0, 1):
            x, y = u(15 + j * 5), cy + u(i * 5)
            d.ellipse([x - u(1.3), y - u(1.3), x + u(1.3), y + u(1.3)], fill="#4a4a52")
    for x in BAR_DIVIDERS:
        d.line([u(x), cy - u(13), u(x), cy + u(13)], fill="#2c2c32", width=max(1, u(1)))

    def hover_bg(key, cx, r=15, color=None):
        if sp["hover"] == key:
            fill = color or ("#34343b" if sp["pressed"] == key else "#29292f")
            d.rounded_rectangle([cx - u(r), cy - u(r), cx + u(r), cy + u(r)], radius=u(8), fill=fill)

    def glyph(cx, ch, px, color):
        d.text((cx, cy), ch, font=pil_font("icons", u(px)), fill=color, anchor="mm")

    recording = st in ("recording", "paused", "pausing", "starting", "stopping")
    busy = st in ("starting", "stopping", "pausing")
    # record / stop
    cx = u(BAR_BTNS["rec"])
    if recording:
        d.ellipse([cx - u(16), cy - u(16), cx + u(16), cy + u(16)],
                  fill="#3d1619" if sp["hover"] != "rec" else "#4a1a1e")
        d.rounded_rectangle([cx - u(6), cy - u(6), cx + u(6), cy + u(6)], radius=u(2.5),
                            fill=RED if not busy else "#8a2a2a")
    else:
        hover_bg("rec", cx, 16)
        ring = RED if st == "countdown" else "#5c2226"
        d.ellipse([cx - u(12), cy - u(12), cx + u(12), cy + u(12)], outline=ring, width=u(2))
        if st != "countdown":
            d.ellipse([cx - u(8), cy - u(8), cx + u(8), cy + u(8)],
                      fill="#f87171" if sp["hover"] == "rec" else RED)
    # pause / resume
    cx = u(BAR_BTNS["pause"])
    can_pause = st in ("recording", "paused")
    if can_pause:
        if st == "paused":
            d.rounded_rectangle([cx - u(15), cy - u(15), cx + u(15), cy + u(15)], radius=u(8), fill="#2f2a1c")
        hover_bg("pause", cx)
    glyph(cx, "\uE768" if st == "paused" else "\uE769", 15,
          ("#fbbf24" if st == "paused" else FG) if can_pause else "#44444b")
    # right-hand buttons
    for key, ch, px in (("shot", "\uE722", 16), ("settings", "\uE713", 16),
                        ("min", "\uE921", 11), ("close", "\uE8BB", 11)):
        cx = u(BAR_BTNS[key])
        hover_bg(key, cx, 11 if key in ("min", "close") else 14, "#c42b1c" if key == "close" else None)
        glyph(cx, ch, px, "white" if key == "close" and sp["hover"] == "close" else
              (FG if sp["hover"] == key else "#c9c9cf"))
    # timer + status
    x0, x1 = u(BAR_TEXT_X0), u(BAR_TEXT_X1)
    tcol = {"recording": RED, "paused": "#fbbf24"}.get(st, FG)
    d.text((x0, cy - u(8)), sp["timer"], font=pil_font("semi", u(17)), fill=tcol, anchor="lm")
    sx = x0
    if st == "recording" and not sp["hover"]:
        if sp["blink"]:
            d.ellipse([x0, cy + u(9) - u(3), x0 + u(6), cy + u(9) + u(3)], fill=RED)
        sx = x0 + u(11)
    f = pil_font("ui", u(10.5))
    text = sp["status"]
    if d.textlength(text, font=f) > x1 - sx:
        while text and d.textlength(text + "…", font=f) > x1 - sx:
            text = text[:-1]
        text = text.rstrip() + "…"
    d.text((sx, cy + u(10)), text, font=f, fill=FG_DIM, anchor="lm")
    return img.resize((round(BAR_W * sp["S"]), round(BAR_H * sp["S"])), Image.LANCZOS)


RESOLUTIONS = {
    "Native": None,
    "3840x2160 (4K)": (3840, 2160),
    "2560x1440 (1440p)": (2560, 1440),
    "1920x1080 (1080p)": (1920, 1080),
    "1600x900": (1600, 900),
    "1280x720 (720p)": (1280, 720),
    "854x480 (480p)": (854, 480),
    "Custom": None,
}
FPS_CHOICES = ["24", "30", "48", "50", "60", "90", "100", "120", "144", "165", "240"]
QUALITY = {"High": 20, "Balanced": 25, "Small file": 31}
VENDORS = {"qsv": "Intel Quick Sync", "nvenc": "NVIDIA NVENC", "amf": "AMD AMF", "x264": "CPU (x264)"}
CODECS = {"h264": "H.264", "hevc": "HEVC / H.265", "av1": "AV1"}


def videos_dir():
    buf = ctypes.create_unicode_buffer(260)
    try:
        ctypes.windll.shell32.SHGetFolderPathW(None, 0x000E, None, 0, buf)  # CSIDL_MYVIDEO
        if buf.value:
            return os.path.join(buf.value, APP)
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "Videos", APP)


DEFAULTS = {
    "monitor": 0, "capture": "full", "region": None, "window_title": "",
    "out_res": "Native", "custom_w": 1920, "custom_h": 1080, "scale_mode": "Fit",
    "fps": 60, "cursor": True,
    "encoder": "auto", "codec": "h264", "quality": "Balanced", "bitrate": 0, "container": "mp4",
    "chroma444": False,
    "sys_audio": True, "speaker": "Default", "sys_vol": 100,
    "mic": False, "mic_device": "", "mic_vol": 100, "mic_denoise": True, "separate_tracks": False,
    "out_dir": videos_dir(), "countdown": 3, "auto_stop": 0,
    "exclude_ui": True, "border": True, "open_after": False,
    "hk_record": "ctrl+alt+r", "hk_pause": "ctrl+alt+p", "hk_shot": "ctrl+alt+s", "hk_toggle": "ctrl+alt+h",
    "pos": None, "last_plan": None, "ffmpeg": "",
}


# the app's former names: only used to migrate old settings/recordings once (newest first)
_OLD_NAMES = ("Smooth" + "Rec", "Quick" + "Rec")


def _move_dir(old, new):
    """Rename a folder; if Windows refuses, move its contents item by item instead."""
    if not os.path.isdir(old) or (os.path.exists(new) and os.listdir(new)):
        return
    try:
        if os.path.isdir(new):
            os.rmdir(new)
        os.replace(old, new)
        return
    except OSError:
        pass
    try:
        os.makedirs(new, exist_ok=True)
        for name in os.listdir(old):
            shutil.move(os.path.join(old, name), os.path.join(new, name))
        os.rmdir(old)
    except OSError:
        pass


def _migrate_old_name():
    """Move %APPDATA%\\<old name> and Videos\\<old name> to the SmoothRecord locations (one time)."""
    for old in _OLD_NAMES:
        _move_dir(os.path.join(os.path.dirname(CFG_DIR), old), CFG_DIR)
        _move_dir(os.path.join(os.path.dirname(videos_dir()), old), videos_dir())


def load_cfg():
    _migrate_old_name()
    cfg = dict(DEFAULTS)
    try:
        with open(CFG_PATH, encoding="utf-8-sig") as f:  # tolerate a BOM (e.g. edited in Notepad)
            cfg.update(json.load(f))
    except Exception:
        pass
    old_defaults = {os.path.normcase(os.path.join(os.path.dirname(videos_dir()), n)) for n in _OLD_NAMES}
    if os.path.normcase(os.path.normpath(cfg.get("out_dir", ""))) in old_defaults:
        cfg["out_dir"] = videos_dir()  # an old default recordings folder now has the new name
    return cfg


def save_cfg(cfg):
    os.makedirs(CFG_DIR, exist_ok=True)
    tmp = CFG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CFG_PATH)


def find_ffmpeg(cfg):
    cands = [cfg.get("ffmpeg"),
             os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "ffmpeg.exe")
             if getattr(sys, "frozen", False) else None,
             shutil.which("ffmpeg"),
             os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg.exe"),
             os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Links", "ffmpeg.exe")]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def run_quiet(args, timeout=20):
    return subprocess.run(args, capture_output=True, timeout=timeout, creationflags=NOWIN)


# ---- Win32 helpers ------------------------------------------------------------------
class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT),
                ("dwFlags", wt.DWORD), ("szDevice", wt.WCHAR * 32)]


class DEVMODEW(ctypes.Structure):
    _fields_ = [("dmDeviceName", wt.WCHAR * 32), ("dmSpecVersion", wt.WORD), ("dmDriverVersion", wt.WORD),
                ("dmSize", wt.WORD), ("dmDriverExtra", wt.WORD), ("dmFields", wt.DWORD),
                ("dmPositionX", wt.LONG), ("dmPositionY", wt.LONG), ("dmDisplayOrientation", wt.DWORD),
                ("dmDisplayFixedOutput", wt.DWORD), ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short),
                ("dmYResolution", ctypes.c_short), ("dmTTOption", ctypes.c_short), ("dmCollate", ctypes.c_short),
                ("dmFormName", wt.WCHAR * 32), ("dmLogPixels", wt.WORD), ("dmBitsPerPel", wt.DWORD),
                ("dmPelsWidth", wt.DWORD), ("dmPelsHeight", wt.DWORD), ("dmDisplayFlags", wt.DWORD),
                ("dmDisplayFrequency", wt.DWORD), ("dmICMMethod", wt.DWORD), ("dmICMIntent", wt.DWORD),
                ("dmMediaType", wt.DWORD), ("dmDitherType", wt.DWORD), ("dmReserved1", wt.DWORD),
                ("dmReserved2", wt.DWORD), ("dmPanningWidth", wt.DWORD), ("dmPanningHeight", wt.DWORD)]


MONITORENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)
WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def get_monitors():
    """Monitors ordered primary-first (matches DXGI output order on typical setups)."""
    mons = []

    def cb(hmon, hdc, lprc, lp):
        mi = MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(mi)
        user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        dm = DEVMODEW()
        dm.dmSize = ctypes.sizeof(dm)
        hz = 60
        if user32.EnumDisplaySettingsW(mi.szDevice, 0xFFFFFFFF, ctypes.byref(dm)):
            hz = dm.dmDisplayFrequency or 60
        r = mi.rcMonitor
        mons.append({"x": r.left, "y": r.top, "w": r.right - r.left, "h": r.bottom - r.top,
                     "primary": bool(mi.dwFlags & 1), "device": mi.szDevice, "hz": hz,
                     "hmon": int(hmon or 0),
                     "work": (mi.rcWork.left, mi.rcWork.top, mi.rcWork.right - mi.rcWork.left,
                              mi.rcWork.bottom - mi.rcWork.top)})
        return True

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(cb), 0)
    mons.sort(key=lambda m: (not m["primary"], m["device"]))
    return mons or [{"x": 0, "y": 0, "w": 1920, "h": 1080, "primary": True, "device": "", "hz": 60, "hmon": 0}]


def window_rect(hwnd):
    r = wt.RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(r), ctypes.sizeof(r)) != 0:  # EXTENDED_FRAME_BOUNDS
        user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def list_windows():
    out, me = [], os.getpid()

    def cb(h, lp):
        if not user32.IsWindowVisible(h) or user32.IsIconic(h):
            return True
        n = user32.GetWindowTextLengthW(h)
        if not n:
            return True
        cloaked = ctypes.c_int(0)
        dwmapi.DwmGetWindowAttribute(h, 14, ctypes.byref(cloaked), 4)
        if cloaked.value:
            return True
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if pid.value == me or user32.GetWindowLongW(h, -20) & 0x80:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(h, buf, n + 1)
        x, y, w, hh = window_rect(h)
        if w >= 64 and hh >= 64 and buf.value not in ("Program Manager",):
            out.append((h, buf.value))
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return out


def virtual_screen():
    return (user32.GetSystemMetrics(76), user32.GetSystemMetrics(77),
            user32.GetSystemMetrics(78), user32.GetSystemMetrics(79))


def top_hwnd(widget):
    return user32.GetAncestor(widget.winfo_id(), 2)  # GA_ROOT


def exclude_from_capture(widget, on=True):
    try:
        user32.SetWindowDisplayAffinity(top_hwnd(widget), 0x11 if on else 0)
    except Exception:
        pass


def dark_titlebar(widget):
    try:
        v = ctypes.c_int(1)
        dwmapi.DwmSetWindowAttribute(top_hwnd(widget), 20, ctypes.byref(v), 4)
    except Exception:
        pass


class _PPTS(ctypes.Structure):  # PROCESS_POWER_THROTTLING_STATE
    _fields_ = [("Version", wt.ULONG), ("ControlMask", wt.ULONG), ("StateMask", wt.ULONG)]


kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.GetCurrentProcess.restype = wt.HANDLE
kernel32.SetProcessInformation.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]


def no_power_throttling(pid=None):
    """Opt a process out of Windows EcoQoS / timer throttling (matters on battery)."""
    try:
        h = kernel32.OpenProcess(0x0200 | 0x1000, False, pid) if pid else kernel32.GetCurrentProcess()
        st = _PPTS(1, 0x1 | 0x4, 0)  # EXECUTION_SPEED | IGNORE_TIMER_RESOLUTION -> off
        kernel32.SetProcessInformation(h, 4, ctypes.byref(st), ctypes.sizeof(st))  # ProcessPowerThrottling
        if pid:
            kernel32.CloseHandle(h)
    except Exception:
        pass


class _SPS(ctypes.Structure):  # SYSTEM_POWER_STATUS
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", wt.DWORD), ("BatteryFullLifeTime", wt.DWORD)]


def on_battery():
    s = _SPS()
    try:
        return bool(kernel32.GetSystemPowerStatus(ctypes.byref(s))) and s.ACLineStatus == 0
    except Exception:
        return False


# ---- hotkeys ------------------------------------------------------------------------
VK_NAMES = {"space": 0x20, "pause": 0x13, "printscreen": 0x2C, "prtsc": 0x2C, "insert": 0x2D, "delete": 0x2E,
            "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "scrolllock": 0x91,
            "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28}


def parse_hotkey(s):
    if not s:
        return None
    mods, vk = 0, None
    for p in s.lower().replace(" ", "").split("+"):
        if p in ("ctrl", "control"):
            mods |= 2
        elif p == "alt":
            mods |= 1
        elif p == "shift":
            mods |= 4
        elif p in ("win", "windows"):
            mods |= 8
        elif len(p) == 1 and p.isalnum():
            vk = ord(p.upper())
        elif re.fullmatch(r"f([1-9]|1[0-9]|2[0-4])", p):
            vk = 0x6F + int(p[1:])
        elif p in VK_NAMES:
            vk = VK_NAMES[p]
        else:
            return None
    return (mods, vk) if vk else None


class HotkeyThread(threading.Thread):
    def __init__(self, bindings, q):
        super().__init__(daemon=True)
        self.bindings, self.q, self.tid, self.failed = bindings, q, None, []
        self.ready = threading.Event()

    def run(self):
        self.tid = kernel32.GetCurrentThreadId()
        ids = []
        for i, (name, combo) in enumerate(self.bindings.items(), 1):
            hk = parse_hotkey(combo)
            if hk and user32.RegisterHotKey(None, i, hk[0] | 0x4000, hk[1]):
                ids.append(i)
            elif combo:
                self.failed.append(combo)
        names = list(self.bindings)
        self.ready.set()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312:  # WM_HOTKEY
                self.q.put(("hotkey", names[msg.wParam - 1]))
        for i in ids:
            user32.UnregisterHotKey(None, i)

    def stop(self):
        if self.tid:
            user32.PostThreadMessageW(self.tid, 0x0012, 0, 0)  # WM_QUIT


# ---- capability detection ---------------------------------------------------------
def detect_encoders(ffmpeg):
    avail = {}
    for v in ("qsv", "nvenc", "amf"):
        for c in ("h264", "hevc", "av1"):
            try:
                r = run_quiet([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                               "color=c=black:s=640x360:r=30", "-frames:v", "5", "-pix_fmt", "nv12",
                               "-c:v", f"{c}_{v}", "-f", "null", "-"], timeout=20)
                ok = r.returncode == 0
            except Exception:
                ok = False
            if ok:
                avail.setdefault(v, []).append(c)
            elif c == "h264":
                break
    try:
        avail["_wgc"] = b"gfxcapture" in run_quiet([ffmpeg, "-hide_banner", "-filters"]).stdout
    except Exception:
        avail["_wgc"] = False
    return avail


def list_mics(ffmpeg):
    try:
        import soundcard as sc
        return [m.name for m in sc.all_microphones(include_loopback=False)]
    except Exception:
        pass
    try:
        r = run_quiet([ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"])
        return re.findall(r'"([^"]+)" \(audio\)', r.stderr.decode("utf-8", "replace"))
    except Exception:
        return []


def list_speakers():
    try:
        import soundcard as sc
        return [s.name for s in sc.all_speakers()]
    except Exception:
        return []


def soundcard_ok():
    try:
        import soundcard  # noqa: F401
        import numpy  # noqa: F401
        return True
    except Exception:
        return False


# ---- audio capture (WASAPI via soundcard -> audio-only ffmpeg over localhost TCP) --------
class AudioSender(threading.Thread):
    """Streams one audio source to ffmpeg. Before the live audio it sends exactly enough silence that
    sample 0 corresponds to the shared reference instant `t_ref` (time.perf_counter()), so every audio
    source - and later the video - can be aligned sample-accurately."""

    def __init__(self, port, kind, device, t_ref):
        super().__init__(daemon=True)
        self.port, self.kind, self.device, self.t_ref = port, kind, device, t_ref
        self.channels = 2 if kind == "loopback" else 1
        self.stop_evt = threading.Event()
        self.error = None

    def _open(self, sc):
        if self.kind == "loopback":
            spk = None
            if self.device and self.device != "Default":
                spk = next((s for s in sc.all_speakers() if s.name == self.device), None)
            spk = spk or sc.default_speaker()
            return sc.get_microphone(spk.id, include_loopback=True)
        mic = None
        if self.device:
            mic = next((m for m in sc.all_microphones() if m.name == self.device), None)
        return mic or sc.default_microphone()

    def run(self):
        try:
            ctypes.windll.ole32.CoInitializeEx(None, 0)
            import numpy as np
            import soundcard as sc
            warnings.simplefilter("ignore")
            src = self._open(sc)
            with src.recorder(samplerate=48000, channels=self.channels, blocksize=480) as rec:
                sock, t0 = None, time.time()
                while not self.stop_evt.is_set() and time.time() - t0 < 15:
                    try:
                        sock = socket.create_connection(("127.0.0.1", self.port), timeout=1)
                        break
                    except OSError:
                        time.sleep(0.02)
                if sock is None:
                    return
                sock.settimeout(5)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)  # smooth 10 ms packets, no batching
                try:
                    rec.flush()  # drop audio buffered before ffmpeg was ready
                except Exception:
                    pass
                with sock:
                    # pad with silence so that sample 0 == t_ref
                    pad = max(0, int(round((time.perf_counter() - self.t_ref) * 48000)))
                    zero = bytes(4 * self.channels * 4800)
                    while pad > 0 and not self.stop_evt.is_set():
                        n = min(pad, 4800)
                        sock.sendall(zero[:4 * self.channels * n])
                        pad -= n
                    while not self.stop_evt.is_set():
                        data = rec.record(numframes=480)
                        if data.size:
                            if data.ndim == 2 and data.shape[1] != self.channels:
                                data = data[:, :self.channels] if data.shape[1] > self.channels else \
                                    np.repeat(data, self.channels, axis=1)
                            sock.sendall(np.ascontiguousarray(data, dtype="<f4").tobytes())
        except Exception as e:  # socket closed by ffmpeg on stop is expected
            if not self.stop_evt.is_set():
                self.error = repr(e)

    def stop(self):
        self.stop_evt.set()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ---- recording engine ------------------------------------------------------------
def even(v):
    return max(2, int(v) // 2 * 2)


def output_size(cw, ch, cfg):
    lab = cfg["out_res"]
    if lab == "Native" or lab not in RESOLUTIONS:
        W, H = cw, ch
    else:
        W, H = (int(cfg["custom_w"]), int(cfg["custom_h"])) if lab == "Custom" else RESOLUTIONS[lab]
        if cfg["scale_mode"] == "Fit":
            s = min(W / cw, H / ch)
            W, H = cw * s, ch * s
    return even(round(W)), even(round(H))


def effective_codec(cfg):
    """4:4:4 full-colour mode on Intel needs HEVC Range Extensions."""
    if cfg.get("chroma444") and cfg["encoder"] in ("auto", "qsv"):
        return "hevc"
    return cfg["codec"]


def capture_source(source, geo, cursor, fps):
    """geo = (dda_index, ox, oy, w, h, full, monitor_dict)"""
    idx, ox, oy, cw, ch, full, mon = geo
    if source == "wgc":
        # max_framerate well above 120: WGC's minimum-interval check otherwise rejects frames that arrive a
        # little early (measured 105 -> 118 fps captured); the output is resampled to exact CFR afterwards.
        s = f"gfxcapture=max_framerate=1000:capture_cursor={1 if cursor else 0}"
        s += f":hmonitor={mon['hmon']}" if mon.get("hmon") else f":monitor_idx={idx}"
        if not full:
            s += (f":crop_left={ox}:crop_top={oy}:crop_right={mon['w'] - ox - cw}"
                  f":crop_bottom={mon['h'] - oy - ch}")
        return s
    s = f"ddagrab=output_idx={idx}:framerate={fps}:draw_mouse={1 if cursor else 0}"
    if not full:
        s += f":offset_x={ox}:offset_y={oy}:video_size={cw}x{ch}"
    return s


def make_plans(cfg, avail):
    """Ordered list of (encoder vendor, gpu|cpu path, capture source) to try.
    Source 'wgc' = Windows Graphics Capture (colour-exact); 'dda' = Desktop Duplication (fallback)."""
    codec, enc = effective_codec(cfg), cfg["encoder"]
    sources = ["wgc", "dda"] if (avail is None or avail.get("_wgc", True)) else ["dda"]
    plans = []
    for src in sources:
        if enc == "x264":
            plans.append(("x264", "cpu", src))
            continue
        vendors = ["qsv", "nvenc", "amf"] if enc == "auto" else [enc]
        if avail is not None:
            vendors = [v for v in vendors if codec in avail.get(v, [])]
        plans += [(v, "gpu", src) for v in vendors] + [(v, "cpu", src) for v in vendors] + [("x264", "cpu", src)]
    last = tuple(cfg["last_plan"]) if cfg.get("last_plan") and len(cfg["last_plan"]) == 3 else None
    if last in plans:
        plans.remove(last)
        plans.insert(0, last)
    return plans


# Seconds added to the measured video start before aligning audio (video pipeline latency); calibrated
# with a flash+beep test.
AV_CALIBRATION = 0.0


class Segment:
    def __init__(self, path, plan):
        self.path, self.plan = path, plan
        self.apath = path[:-4] + "_audio.mka"
        self.proc = None          # video ffmpeg
        self.aproc = None         # audio ffmpeg
        self.senders = []
        self.tracks = []
        self.t_ref = None         # perf_counter() instant that audio sample 0 corresponds to
        self.v_start = None       # estimated perf_counter() instant of video frame 0
        self.first_frame = threading.Event()
        self.stats = {}
        self.hist = collections.deque(maxlen=5)  # (time, frame) for a ~2 s fps window
        self.t0 = None
        self.log = None


class Recorder:
    """Owns the ffmpeg processes. Video and audio run in SEPARATE processes: in one process FFmpeg's stream
    scheduler kept pausing the screen capture to wait for audio, and Windows dropped those frames
    (measured ~15 vs ~110 unique fps). Audio is aligned to the video sample-accurately when saving.
    All public methods are called from worker threads."""

    def __init__(self, ffmpeg):
        self.ffmpeg = ffmpeg
        self.segments = []
        self.current = None

    # geometry = (dda_index, ox, oy, cw, ch, full, monitor)
    def begin(self, cfg, geometry, plans):
        self.cfg, self.geo, self.plans = dict(cfg), geometry, list(plans)
        self.segments, self.current = [], None
        self.stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.tmpdir = os.path.join(cfg["out_dir"], ".smoothrecord_tmp")
        os.makedirs(self.tmpdir, exist_ok=True)
        return self.new_segment()

    def new_segment(self):
        path = os.path.join(self.tmpdir, f"{self.stamp}_part{len(self.segments) + 1:02d}.mkv")
        errors = []
        for plan in list(self.plans):
            seg, err = self._launch(plan, path)
            if seg:
                self.plans.remove(plan)
                self.plans.insert(0, plan)
                self.segments.append(seg)
                self.current = seg
                return None
            errors.append(f"{VENDORS[plan[0]]} ({plan[1]}, {plan[2]}): {err}")
        return "\n".join(errors[-3:])

    # ---- commands
    def build_cmd(self, plan, out, port=None, cfg=None, geo=None):
        """Video-only ffmpeg command."""
        cfg, geo = cfg or self.cfg, geo or self.geo
        vendor, mode, source = plan
        W, H = output_size(geo[3], geo[4], cfg)
        fps = max(1, min(240, int(cfg["fps"])))
        codec = "h264" if vendor == "x264" else effective_codec(cfg)
        c444 = bool(cfg.get("chroma444")) and (vendor in ("qsv", "x264"))
        q = QUALITY.get(cfg["quality"], 25)
        mbps = float(cfg.get("bitrate") or 0)

        a = [self.ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostats",
             "-progress", "pipe:1", "-stats_period", "0.1", "-y"]
        if vendor == "qsv" and mode == "gpu":
            a += ["-init_hw_device", "d3d11va=dx", "-init_hw_device", "qsv=qs@dx", "-filter_hw_device", "dx"]
        a += ["-f", "lavfi", "-i", capture_source(source, geo, cfg["cursor"], fps)]

        # screen pixels are full-range sRGB -> BT.709 limited-range YUV, tagged as such.
        # (No setparams input tags: combined with the output tags FFmpeg silently negotiates 4:2:0 even in
        #  4:4:4 mode. Colour is exact without them - verified.)
        if mode == "gpu" and vendor == "qsv":
            v = ("hwmap=derive_device=qsv,format=qsv,"
                 f"vpp_qsv=w={W}:h={H}:format={'vuyx' if c444 else 'nv12'}:scale_mode=3:out_range=tv:"
                 "out_color_matrix=bt709:out_color_primaries=bt709:out_color_transfer=iec61966-2-1")
        elif mode == "gpu":
            v = f"scale_d3d11=width={W}:height={H}:format=nv12"
        else:
            pf = ("yuv444p" if c444 else "yuv420p") if vendor == "x264" else "nv12"
            v = (f"hwdownload,format=bgra,scale={W}:{H}:flags=bicubic+accurate_rnd+full_chroma_int:"
                 f"out_color_matrix=bt709:out_range=tv,format={pf}")
        a += ["-vf", v]

        if vendor == "qsv":
            # no B-frames + deeper async queue: measured +10-15% captured frames at 120 fps
            a += ["-c:v", f"{codec}_qsv", "-preset", "veryfast", "-low_power", "1", "-bf", "0", "-async_depth", "4"]
            if c444:
                a += ["-profile:v", "rext"]
            a += ["-b:v", f"{mbps}M", "-maxrate", f"{mbps * 1.5}M", "-bufsize", f"{mbps * 2}M"] if mbps \
                else ["-global_quality", str(q)]
        elif vendor == "nvenc":
            a += ["-c:v", f"{codec}_nvenc", "-preset", "p4", "-tune", "hq"]
            a += ["-rc", "vbr", "-b:v", f"{mbps}M", "-maxrate", f"{mbps * 1.5}M"] if mbps \
                else ["-rc", "vbr", "-cq", str(q - 1), "-b:v", "0"]
        elif vendor == "amf":
            a += ["-c:v", f"{codec}_amf", "-quality", "speed"]
            a += ["-rc", "vbr_peak", "-b:v", f"{mbps}M", "-maxrate", f"{mbps * 1.5}M"] if mbps \
                else ["-rc", "cqp", "-qp_i", str(q - 2), "-qp_p", str(q - 2)]
        else:
            a += ["-c:v", "libx264", "-preset", "veryfast" if fps <= 60 else "superfast"]
            a += ["-b:v", f"{mbps}M", "-maxrate", f"{mbps * 1.5}M", "-bufsize", f"{mbps * 2}M"] if mbps \
                else ["-crf", str(q - 2)]
        a += ["-g", str(fps * 2), "-fps_mode", "cfr", "-r", str(fps),
              "-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709",
              "-color_trc", "iec61966-2-1", "-f", "matroska", out]
        return a

    def build_audio_cmd(self, srcs, out, cfg=None):
        """Audio-only ffmpeg: srcs = [(kind, port, channels)] -> lossless FLAC tracks in MKA."""
        cfg = cfg or self.cfg
        a = [self.ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostats", "-y"]
        idx = {}
        for i, (kind, port, ch) in enumerate(srcs):
            a += ["-f", "f32le", "-ar", "48000", "-ac", str(ch),
                  "-i", f"tcp://127.0.0.1:{port}?listen=1&listen_timeout=15000"]
            idx[kind] = i
        fc, tracks = [], []
        if "loopback" in idx:
            fc.append(f"[{idx['loopback']}:a]volume={cfg['sys_vol'] / 100:.2f}[sa]")
        if "mic" in idx:
            den = ",highpass=f=80,afftdn=nf=-25" if cfg["mic_denoise"] else ""
            fc.append(f"[{idx['mic']}:a]volume={cfg['mic_vol'] / 100:.2f}{den},"
                      f"aformat=channel_layouts=stereo[ma]")
        if "loopback" in idx and "mic" in idx:
            mix = "amix=inputs=2:duration=longest:dropout_transition=0:normalize=0"
            if cfg["separate_tracks"]:
                fc += ["[sa]asplit=2[sa1][sa2]", "[ma]asplit=2[ma1][ma2]", f"[sa1][ma1]{mix}[mix]"]
                tracks = [("[mix]", "Mixed"), ("[sa2]", "System"), ("[ma2]", "Microphone")]
            else:
                fc.append(f"[sa][ma]{mix}[mix]")
                tracks = [("[mix]", "Mixed")]
        elif "loopback" in idx:
            tracks = [("[sa]", "System")]
        else:
            tracks = [("[ma]", "Microphone")]
        a += ["-filter_complex", ";".join(fc)]
        for lab, _ in tracks:
            a += ["-map", lab]
        a += ["-c:a", "flac", "-f", "matroska", out]
        return a, [t for _, t in tracks]

    # ---- process control
    def _launch(self, plan, path):
        seg = Segment(path, plan)
        os.makedirs(CFG_DIR, exist_ok=True)
        seg.log = open(LOG_PATH, "a", encoding="utf-8", errors="replace")
        srcs = []
        if soundcard_ok():
            if self.cfg["sys_audio"]:
                srcs.append(("loopback", free_port(), 2, self.cfg["speaker"]))
            if self.cfg["mic"]:
                srcs.append(("mic", free_port(), 1, self.cfg["mic_device"]))
        try:
            if srcs:
                seg.t_ref = time.perf_counter()
                acmd, seg.tracks = self.build_audio_cmd([(k, p, c) for k, p, c, _ in srcs], seg.apath)
                seg.log.write(f"\n==== {datetime.now()}  audio\n{subprocess.list2cmdline(acmd)}\n")
                seg.aproc = subprocess.Popen(acmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                             stderr=seg.log, creationflags=NOWIN | 0x80)
                for k, p, c, dev in srcs:
                    s = AudioSender(p, k, dev, seg.t_ref)
                    s.start()
                    seg.senders.append(s)
            cmd = self.build_cmd(plan, path)
            seg.log.write(f"\n==== {datetime.now()}  {plan}\n{subprocess.list2cmdline(cmd)}\n")
            seg.log.flush()
            seg.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=seg.log, creationflags=NOWIN | 0x80)  # HIGH_PRIORITY_CLASS
        except Exception as e:
            self._kill(seg)
            return None, str(e)
        no_power_throttling(seg.proc.pid)
        seg.reader = threading.Thread(target=self._progress_reader, args=(seg,), daemon=True)
        seg.reader.start()
        deadline = time.time() + 8
        while time.time() < deadline:
            if seg.first_frame.wait(0.1):
                seg.t0 = time.monotonic()
                return seg, None
            if seg.proc.poll() is not None:
                break
        self._kill(seg)
        err = self._log_tail() or "ffmpeg did not start"
        for f in (path, seg.apath):
            try:
                os.remove(f)
            except OSError:
                pass
        return None, err

    def _progress_reader(self, seg):
        cur = {}
        try:
            for raw in seg.proc.stdout:
                now = time.perf_counter()
                line = raw.decode("utf-8", "ignore").strip()
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cur[k] = v
                if k == "out_time_us":
                    # frame-0 instant ~= read time - media time; the minimum over many reports removes delay
                    try:
                        us = int(v)
                        if us > 0:
                            est = now - us / 1e6
                            seg.v_start = est if seg.v_start is None else min(seg.v_start, est)
                    except ValueError:
                        pass
                if k == "progress":
                    seg.stats = dict(cur, _t=time.monotonic())
                    try:
                        fr = int(cur.get("frame", "0"))
                        seg.hist.append((seg.stats["_t"], fr))
                        if fr > 0:
                            seg.first_frame.set()
                    except ValueError:
                        pass
        except Exception:
            pass

    @staticmethod
    def _log_tail():
        try:
            with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
                lines = [l.strip() for l in f.readlines()[-8:] if l.strip() and not l.startswith("====")]
            return lines[-1][:300] if lines else ""
        except Exception:
            return ""

    @staticmethod
    def _quit(p, timeout=20):
        if not p:
            return
        try:
            p.stdin.write(b"q")
            p.stdin.flush()
        except Exception:
            pass
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()

    def _kill(self, seg):
        for p in (seg.proc, seg.aproc):
            try:
                if p:
                    p.kill()
                    p.wait(5)
            except Exception:
                pass
        for s in seg.senders:
            s.stop()
        try:
            seg.log.close()
        except Exception:
            pass

    def stop_segment(self):
        seg, self.current = self.current, None
        if not seg:
            return
        seg.t_quit = time.perf_counter()  # the last video frame was captured at (about) this instant
        self._quit(seg.proc)
        if getattr(seg, "reader", None):
            seg.reader.join(3)  # make sure the final frame count has been read
        time.sleep(0.15)          # let audio run just past the last video frame
        self._quit(seg.aproc, 10)
        for s in seg.senders:
            s.stop()
            s.join(3)
        try:
            seg.log.close()
        except Exception:
            pass

    def segment_died(self):
        seg = self.current
        return seg is not None and seg.proc.poll() is not None

    def abandon_dead_segment(self):
        seg, self.current = self.current, None
        if seg:
            self._quit(seg.aproc, 5)
            for s in seg.senders:
                s.stop()
            try:
                seg.log.close()
            except Exception:
                pass

    # ---- saving
    def _mux_segment(self, s):
        """Video part + aligned audio -> one MKV. Returns its path (or the video-only part)."""
        if not (s.aproc and os.path.exists(s.apath) and os.path.getsize(s.apath) > 1024
                and s.t_ref is not None and s.tracks):
            return s.path
        out = s.path[:-4] + "_av.mkv"
        # Video start = moment we stopped it minus its real length (frames / fps of the CFR output).
        # (Estimating it from FFmpeg's progress reports proved unreliable - they arrive in delayed batches.)
        d = None
        try:
            frames = int(s.stats.get("frame", "0"))
            if getattr(s, "t_quit", None) and frames > 0:
                d = (s.t_quit - frames / max(1, int(self.cfg["fps"]))) - s.t_ref + AV_CALIBRATION
        except (ValueError, TypeError):
            pass
        if d is None or not (-2.0 < d < 30.0):
            d = 0.0  # implausible: keep audio untrimmed rather than risk cutting the video
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as lf:
                lf.write(f"[sync] video starts {d * 1000:.1f} ms into the audio; tracks={s.tracks}\n")
        except OSError:
            pass
        fc = []
        for i in range(len(s.tracks)):
            sync = (f"atrim=start={d:.6f},asetpts=PTS-STARTPTS" if d >= 0
                    else f"adelay={-d * 1000:.3f}:all=1")
            fc.append(f"[1:a:{i}]{sync}[a{i}]")
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", s.path, "-i", s.apath,
               "-filter_complex", ";".join(fc), "-map", "0:v"]
        for i, title in enumerate(s.tracks):
            cmd += ["-map", f"[a{i}]", f"-metadata:s:a:{i}", f"title={title}"]
        cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", out]  # never cut the video to the audio
        r = run_quiet(cmd, timeout=3600)
        return out if r.returncode == 0 and os.path.exists(out) else s.path

    def finalize(self):
        """Mux audio, join segments into the final file. Returns (path, error)."""
        parts = [self._mux_segment(s) for s in self.segments
                 if os.path.exists(s.path) and os.path.getsize(s.path) > 2048]
        if not parts:
            return None, "Nothing was recorded."
        cont = self.cfg["container"]
        out = os.path.join(self.cfg["out_dir"], f"SmoothRecord_{self.stamp}.{cont}")
        if cont == "mkv" and len(parts) == 1:
            os.replace(parts[0], out)
            self._cleanup_tmp()
            return out, None
        if len(parts) == 1:
            inp = ["-i", parts[0]]
        else:
            lst = os.path.join(self.tmpdir, f"{self.stamp}_list.txt")
            with open(lst, "w", encoding="utf-8") as f:
                for s in parts:
                    f.write("file '" + s.replace("\\", "/").replace("'", "'\\''") + "'\n")
            inp = ["-f", "concat", "-safe", "0", "-i", lst]
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y"] + inp + ["-map", "0", "-c", "copy"]
        if cont == "mp4":
            cmd += ["-movflags", "+faststart"]
            if self.segments[-1].plan[0] != "x264" and effective_codec(self.cfg) == "hevc":
                cmd += ["-tag:v", "hvc1"]
        cmd.append(out)
        r = run_quiet(cmd, timeout=3600)
        if r.returncode == 0 and os.path.exists(out):
            self._cleanup_tmp()
            return out, None
        # keep the raw parts so nothing is lost
        kept = []
        for s in parts:
            dst = os.path.join(self.cfg["out_dir"], os.path.basename(s))
            try:
                os.replace(s, dst)
                kept.append(dst)
            except OSError:
                pass
        self._cleanup_tmp()
        return (kept[0] if kept else None), "Join failed - raw MKV parts kept: " + r.stderr.decode(
            "utf-8", "replace")[-200:]

    def _cleanup_tmp(self):
        try:
            for f in os.listdir(self.tmpdir):
                if f.startswith(self.stamp):
                    os.remove(os.path.join(self.tmpdir, f))
            os.rmdir(self.tmpdir)
        except OSError:
            pass

    def stats(self):
        """(instant_fps, total_bytes) for the live segment."""
        seg = self.current
        size = sum(os.path.getsize(s.path) for s in self.segments[:-1] if os.path.exists(s.path)) \
            if self.segments else 0
        if not seg or not seg.stats:
            return None, size
        st = seg.stats
        try:
            size += int(st.get("total_size", "0") or 0)
        except ValueError:
            pass
        h = list(seg.hist)
        fps = None
        if len(h) >= 2 and h[-1][0] - h[0][0] >= 1.5 and h[0][1] > 0:
            fps = (h[-1][1] - h[0][1]) / (h[-1][0] - h[0][0])
        return fps, size


def take_screenshot(ffmpeg, geo, cursor, out_path, wgc=True):
    """Lossless PNG. WGC gives exact sRGB pixels; a Heartbeat must be running so a frame arrives."""
    for source in (["wgc", "dda"] if wgc else ["dda"]):
        vf = "hwdownload,format=bgra" + ("" if source == "wgc" else ",select=gte(n\\,3)")
        try:
            r = run_quiet([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                           capture_source(source, geo, cursor, 30), "-vf", vf, "-frames:v", "1", "-update", "1",
                           out_path], timeout=10)
        except subprocess.TimeoutExpired:
            continue
        if r.returncode == 0 and os.path.exists(out_path):
            return True
    return False


# ---- overlays ---------------------------------------------------------------------
class Heartbeat:
    """A single on-screen pixel that alternates #000000 / #010101 (invisible to the eye).
    Windows Graphics Capture only delivers frames when something on the monitor changes; on a still
    screen it delivers none, which would freeze the recording timeline. This pixel guarantees a steady
    frame flow, so the output stays an exact, gap-free constant frame rate. It is placed in a monitor
    corner outside the recorded area whenever possible, and it is click-through."""

    def __init__(self, root, mon, rect=None):
        # use the work area: the taskbar is itself always-on-top and would cover a pixel placed on it,
        # so its changes would never reach the screen (and the capture would stall)
        x0, y0, w, h = mon.get("work") or (mon["x"], mon["y"], mon["w"], mon["h"])
        corners = [(x0 + w - 1, y0 + h - 1), (x0, y0 + h - 1), (x0 + w - 1, y0), (x0, y0)]
        pick = corners[0]
        if rect:
            rx, ry, rw, rh = rect
            outside = [c for c in corners if not (rx <= c[0] < rx + rw and ry <= c[1] < ry + rh)]
            pick = outside[0] if outside else corners[0]
        t = self.top = tk.Toplevel(root)
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        t.geometry(f"1x1+{pick[0]}+{pick[1]}")
        self.px = tk.Frame(t, bg="#000000", width=1, height=1)
        self.px.pack(fill="both", expand=True)
        t.update_idletasks()
        hwnd = top_hwnd(t)
        ex = user32.GetWindowLongW(hwnd, -20)
        user32.SetWindowLongW(hwnd, -20, ex | 0x80 | 0x20 | 0x80000 | 0x08000000)  # tool|transparent|layered|noactivate
        user32.SetLayeredWindowAttributes(hwnd, 0, 255, 2)  # fully opaque, so changes are real pixels
        self.alive, self.n = True, 0
        self._tick()

    def _tick(self):
        if not self.alive:
            return
        self.n += 1
        self.px.configure(bg="#010101" if self.n & 1 else "#000000")
        if self.n % 100 == 0:
            self.top.lift()
        self.top.after(4, self._tick)

    def destroy(self):
        self.alive = False
        try:
            self.top.destroy()
        except tk.TclError:
            pass


class RegionSelector:
    """Full-desktop dimmed overlay. Drag = region, click = window under cursor, Esc = cancel."""

    def __init__(self, app, done):
        self.app, self.done = app, done
        vx, vy, vw, vh = virtual_screen()
        self.vx, self.vy = vx, vy
        t = self.top = tk.Toplevel(app.root)
        t.overrideredirect(True)
        t.geometry(f"{vw}x{vh}+{vx}+{vy}")
        t.attributes("-topmost", True)
        t.attributes("-alpha", 0.40)
        c = self.cv = tk.Canvas(t, bg="black", highlightthickness=0, cursor="crosshair")
        c.pack(fill="both", expand=True)
        m = get_monitors()[0]
        S = app.S
        c.create_text(m["x"] - vx + m["w"] // 2, m["y"] - vy + int(60 * S), fill="white",
                      font=("Segoe UI Semibold", 15),
                      text="Drag to select an area   •   Click a window to capture it   •   Esc to cancel")
        self.rect = c.create_rectangle(0, 0, 0, 0, outline=RED, width=max(2, int(2 * S)), fill="#333333")
        self.lab = c.create_text(0, 0, fill="white", font=("Segoe UI Semibold", 12), anchor="sw")
        self.start = None
        c.bind("<ButtonPress-1>", self.press)
        c.bind("<B1-Motion>", self.motion)
        c.bind("<ButtonRelease-1>", self.release)
        t.bind("<Escape>", lambda e: self.finish(None))
        t.bind("<ButtonPress-3>", lambda e: self.finish(None))
        t.focus_force()
        t.grab_set()

    def press(self, e):
        self.start = (e.x_root, e.y_root)

    def _rect(self, e):
        x0, y0 = self.start
        x1, y1 = e.x_root, e.y_root
        return min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0)

    def motion(self, e):
        if not self.start:
            return
        x, y, w, h = self._rect(e)
        c = self.cv
        c.coords(self.rect, x - self.vx, y - self.vy, x + w - self.vx, y + h - self.vy)
        c.coords(self.lab, x - self.vx, y - self.vy - 6)
        c.itemconfigure(self.lab, text=f"{even(w)} × {even(h)}")

    def release(self, e):
        if not self.start:
            return
        x, y, w, h = self._rect(e)
        if w < 16 or h < 16:  # treat as click: pick window under cursor
            pt = wt.POINT(e.x_root, e.y_root)
            self.top.withdraw()
            self.top.update()
            hwnd = user32.WindowFromPoint(pt)
            hwnd = user32.GetAncestor(hwnd, 2) if hwnd else None
            if hwnd:
                n = user32.GetWindowTextLengthW(hwnd)
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                self.finish(("window", hwnd, buf.value, window_rect(hwnd)))
            else:
                self.finish(None)
            return
        self.finish(("region", [x, y, w, h]))

    def finish(self, result):
        try:
            self.top.grab_release()
            self.top.destroy()
        except tk.TclError:
            pass
        self.done(result)


class BorderOverlay:
    """Click-through red frame around the recorded area (excluded from capture)."""

    def __init__(self, root, x, y, w, h, S):
        b = max(2, int(2 * S))
        t = self.top = tk.Toplevel(root)
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        key = "#ff00fe"
        t.configure(bg=key)
        t.attributes("-transparentcolor", key)
        t.geometry(f"{w + 2 * b}x{h + 2 * b}+{x - b}+{y - b}")
        c = tk.Canvas(t, bg=key, highlightthickness=0)
        c.pack(fill="both", expand=True)
        c.create_rectangle(b // 2, b // 2, w + 2 * b - b // 2 - 1, h + 2 * b - b // 2 - 1,
                           outline=RED, width=b, dash=(8, 4))
        t.update_idletasks()
        hwnd = top_hwnd(t)
        ex = user32.GetWindowLongW(hwnd, -20)
        user32.SetWindowLongW(hwnd, -20, ex | 0x80000 | 0x20 | 0x80)  # layered|transparent|toolwindow
        exclude_from_capture(t)

    def destroy(self):
        try:
            self.top.destroy()
        except tk.TclError:
            pass


class Countdown:
    def __init__(self, root, cx, cy, secs, S, done):
        self.done, self.left, self.cancelled = done, secs, False
        sz = int(150 * S)
        t = self.top = tk.Toplevel(root)
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        key = "#ff00fe"
        t.configure(bg=key)
        t.attributes("-transparentcolor", key)
        t.geometry(f"{sz}x{sz}+{cx - sz // 2}+{cy - sz // 2}")
        c = self.cv = tk.Canvas(t, bg=key, highlightthickness=0)
        c.pack(fill="both", expand=True)
        c.create_oval(2, 2, sz - 2, sz - 2, fill=BG, outline=RED, width=max(2, int(3 * S)))
        self.txt = c.create_text(sz // 2, sz // 2, fill=FG, font=("Segoe UI Semibold", 54))
        t.update_idletasks()
        exclude_from_capture(t)
        self.tick()

    def tick(self):
        if self.cancelled:
            return
        if self.left <= 0:
            self.top.destroy()
            self.done()
            return
        self.cv.itemconfigure(self.txt, text=str(self.left))
        self.left -= 1
        self.top.after(1000, self.tick)

    def cancel(self):
        self.cancelled = True
        try:
            self.top.destroy()
        except tk.TclError:
            pass


# ---- main app ---------------------------------------------------------------------
class App:
    def __init__(self):
        self.cfg = load_cfg()
        self.root = tk.Tk()
        self.root.withdraw()
        ico = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), "SmoothRecord.ico")
        if os.path.exists(ico):
            try:
                self.root.iconbitmap(default=ico)
            except tk.TclError:
                pass
        self.S = self.root.winfo_fpixels("1i") / 96.0
        self.q = queue.Queue()
        self.state = "idle"  # idle|countdown|starting|recording|pausing|paused|stopping
        self.acc = 0.0
        self.seg_t0 = None
        self.flash = None
        self.hover = None
        self.avail = None
        self.mics, self.speakers = [], []
        self.window_hwnd = None
        self.border = None
        self.countdown = None
        self.settings_win = None
        self.recover_count = 0
        self.hk = None

        self.ffmpeg = find_ffmpeg(self.cfg)
        if not self.ffmpeg:
            messagebox.showerror(APP, "FFmpeg was not found.\n\nInstall it with:\n    winget install Gyan.FFmpeg\n"
                                      "or put ffmpeg.exe next to SmoothRecord.pyw, then start SmoothRecord again.")
            sys.exit(1)
        self.rec = Recorder(self.ffmpeg)
        self.heartbeat = None
        no_power_throttling()  # keep the heartbeat/UI timers precise on battery

        fams = set(tkfont.families(self.root))
        self.icon_font_family = "Segoe Fluent Icons" if "Segoe Fluent Icons" in fams else "Segoe MDL2 Assets"
        self._style()
        self._build_bar()
        self._start_hotkeys()
        threading.Thread(target=self._detect, daemon=True).start()
        self.root.after(80, self.tick)

    # -- background detection
    def _detect(self):
        avail = detect_encoders(self.ffmpeg)
        mics = list_mics(self.ffmpeg)
        spk = list_speakers()
        self.q.put(("detected", (avail, mics, spk)))

    # -- styling
    def _style(self):
        st = ttk.Style(self.root)
        st.theme_use("clam")
        F = ("Segoe UI", 10)
        st.configure(".", background=BG, foreground=FG, fieldbackground=BG2, bordercolor=BORDER,
                     lightcolor=BG2, darkcolor=BG2, troughcolor=BG2, focuscolor=BORDER, font=F,
                     arrowcolor=FG, selectbackground="#3f3f46", selectforeground=FG, insertcolor=FG)
        st.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 0))
        st.configure("TNotebook.Tab", background=BG, foreground=FG_DIM, padding=(14, 6), borderwidth=0)
        st.map("TNotebook.Tab", background=[("selected", BG2)], foreground=[("selected", FG)])
        st.configure("TFrame", background=BG2)
        st.configure("Card.TFrame", background=BG2)
        st.configure("TLabel", background=BG2, foreground=FG)
        st.configure("Dim.TLabel", background=BG2, foreground=FG_DIM, font=("Segoe UI", 9))
        st.configure("Head.TLabel", background=BG2, foreground=FG, font=("Segoe UI Semibold", 10))
        st.configure("TCheckbutton", background=BG2, foreground=FG, indicatorbackground=BG,
                     indicatorforeground=FG)
        st.map("TCheckbutton", background=[("active", BG2)], indicatorbackground=[("selected", ACCENT)])
        st.configure("TRadiobutton", background=BG2, foreground=FG, indicatorbackground=BG)
        st.map("TRadiobutton", background=[("active", BG2)], indicatorbackground=[("selected", ACCENT)])
        st.configure("TButton", background="#3a3a41", foreground=FG, borderwidth=0, padding=(12, 5))
        st.map("TButton", background=[("active", "#4a4a52")])
        st.configure("Accent.TButton", background=ACCENT, foreground="white")
        st.map("Accent.TButton", background=[("active", "#f87171")])
        st.configure("TCombobox", fieldbackground=BG, background="#3a3a41", foreground=FG, padding=4)
        st.map("TCombobox", fieldbackground=[("readonly", BG)], foreground=[("readonly", FG)],
               selectbackground=[("readonly", BG)], selectforeground=[("readonly", FG)])
        st.configure("TEntry", fieldbackground=BG, foreground=FG, padding=4)
        st.configure("TSpinbox", fieldbackground=BG, foreground=FG, background="#3a3a41", padding=3)
        st.configure("Horizontal.TScale", background=BG2, troughcolor=BG)
        self.root.option_add("*TCombobox*Listbox.background", BG)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)
        self.root.option_add("*TCombobox*Listbox.selectBackground", "#3f3f46")
        self.root.option_add("*TCombobox*Listbox.font", F)

    # -- the floating bar
    def _build_bar(self):
        S, r = self.S, self.root
        self.BW, self.BH = round(BAR_W * S), round(BAR_H * S)
        self._bar_key, self._bar_img = None, None
        self._press_btn = None
        r.overrideredirect(True)
        r.attributes("-topmost", True)
        r.attributes("-alpha", 0.97)
        r.title(APP)
        pos = self.cfg.get("pos")
        vx, vy, vw, vh = virtual_screen()
        if pos and vx <= pos[0] <= vx + vw - 40 and vy <= pos[1] <= vy + vh - 20:
            x, y = pos
        else:
            m = get_monitors()[0]
            x, y = m["x"] + (m["w"] - self.BW) // 2, m["y"] + int(24 * S)
        r.geometry(f"{self.BW}x{self.BH}+{x}+{y}")
        self.cv = tk.Canvas(r, width=self.BW, height=self.BH, bg=BG, highlightthickness=0)
        self.cv.pack()
        self._make_btns()
        self.cv.bind("<Motion>", self._on_motion)
        self.cv.bind("<Leave>", lambda e: self._set_hover(None))
        self.cv.bind("<ButtonPress-1>", self._on_press)
        self.cv.bind("<B1-Motion>", self._on_drag)
        self.cv.bind("<ButtonRelease-1>", self._on_release)
        self.cv.bind("<Button-3>", self._menu)
        self._draw()
        r.update_idletasks()
        self._fix_taskbar()

    def _make_btns(self):
        hk = lambda k: "+".join(p.capitalize() if len(p) > 1 else p.upper()
                                for p in self.cfg[k].split("+")) if self.cfg[k] else ""
        tips = {"rec": f"Record · {hk('hk_record')}", "pause": f"Pause · {hk('hk_pause')}",
                "shot": f"Screenshot · {hk('hk_shot')}", "settings": "Settings · right-click: menu",
                "min": "Minimise", "close": "Quit"}
        self.btns = [(k, round(x * self.S), tips[k]) for k, x in BAR_BTNS.items()]

    def _fix_taskbar(self):
        r = self.root
        hwnd = top_hwnd(r)
        ex = user32.GetWindowLongW(hwnd, -20)
        user32.SetWindowLongW(hwnd, -20, (ex & ~0x80) | 0x40000)  # -TOOLWINDOW +APPWINDOW
        # WS_MINIMIZEBOX | WS_SYSMENU: lets Windows itself minimise/restore the borderless bar (and the taskbar
        # button toggle it) without ever changing the window type - that switch caused the old flicker.
        st = user32.GetWindowLongW(hwnd, -16)
        user32.SetWindowLongW(hwnd, -16, st | 0x00020000 | 0x00080000)
        try:
            pref = ctypes.c_int(2)  # rounded corners on Win11
            dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), 4)
        except Exception:
            pass
        r.withdraw()
        r.after(10, self._show_bar)

    def _show_bar(self):
        self.root.deiconify()
        self.root.after(30, lambda: exclude_from_capture(self.root, self.cfg["exclude_ui"]))

    def minimise(self):
        self.hover = None
        self._draw()
        user32.ShowWindow(top_hwnd(self.root), 6)  # SW_MINIMIZE - restored from the taskbar button

    def toggle_bar(self):
        hwnd = top_hwnd(self.root)
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        elif self.root.state() == "withdrawn":
            self._show_bar()
        else:
            self.root.withdraw()

    def _btn_at(self, x, y):
        for key, cx, tip in self.btns:
            hw = (11 if key in ("min", "close") else 14) * self.S
            if abs(x - cx) <= hw and 6 * self.S <= y <= self.BH - 6 * self.S:
                return key
        return None

    def _set_hover(self, key):
        if key != self.hover:
            self.hover = key
            self._draw()

    def _on_motion(self, e):
        self._set_hover(self._btn_at(e.x, e.y))

    def _on_press(self, e):
        self._press_btn = self._btn_at(e.x, e.y)
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y(), e.x_root, e.y_root)
        self._draw()

    def _on_drag(self, e):
        if self._press_btn and abs(e.x_root - self._drag[2]) + abs(e.y_root - self._drag[3]) < 6:
            return
        self._press_btn = None
        self.root.geometry(f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}")

    def _on_release(self, e):
        key = self._press_btn
        self._press_btn = None
        if key and key == self._btn_at(e.x, e.y):
            {"rec": self.toggle_record, "pause": self.toggle_pause, "shot": self.screenshot,
             "settings": self.open_settings, "min": self.minimise, "close": self.quit}[key]()
        elif not key:
            self.cfg["pos"] = [self.root.winfo_x(), self.root.winfo_y()]
        if self.flash and self.flash[2] and not key and abs(e.x_root - self._drag[2]) < 4 \
                and BAR_DIVIDERS[0] * self.S < e.x < BAR_DIVIDERS[1] * self.S:
            os.startfile(self.flash[2]) if os.path.exists(self.flash[2]) else None

    def timer_text(self):
        h, rem = divmod(int(self.elapsed()), 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    def _draw(self):
        if not HAVE_PIL:
            return self._draw_tk()
        sp = {"S": self.S, "state": self.state, "hover": self.hover, "pressed": self._press_btn,
              "timer": self.timer_text(), "status": self.status_text(),
              "blink": int(time.time() * 2) % 2 == 0}
        key = tuple(sp.values())
        if key == self._bar_key:
            return
        self._bar_key = key
        self._bar_img = ImageTk.PhotoImage(render_bar(sp))
        if not self.cv.find_withtag("bar"):
            self.cv.create_image(0, 0, anchor="nw", tags="bar")
        self.cv.itemconfigure("bar", image=self._bar_img)

    def _draw_tk(self):
        c, S = self.cv, self.S
        u = lambda v: int(v * S)
        c.delete("all")
        W, H = self.BW, self.BH
        c.create_rectangle(0, 0, W - 1, H - 1, fill=BG, outline=BORDER)
        # grip
        for i in range(3):
            for j in range(2):
                cx, cy = u(10 + j * 5), H // 2 - u(5) + i * u(5)
                c.create_oval(cx - u(1), cy - u(1), cx + u(1), cy + u(1), fill="#55555c", outline="")
        icon = (self.icon_font_family, 11)
        busy = self.state in ("starting", "stopping", "pausing")
        for key, cx, tip in self.btns:
            cy = H // 2
            if key == self.hover:
                c.create_rectangle(cx - u(13), cy - u(14), cx + u(13), cy + u(14), fill="#2e2e34", outline="")
            if key == "rec":
                if self.state in ("recording", "paused", "pausing", "starting", "stopping"):
                    c.create_rectangle(cx - u(6), cy - u(6), cx + u(6), cy + u(6),
                                       fill=RED if not busy else RED_DIM, outline="")
                elif self.state == "countdown":
                    c.create_oval(cx - u(8), cy - u(8), cx + u(8), cy + u(8), outline=RED, width=u(2))
                else:
                    c.create_oval(cx - u(10), cy - u(10), cx + u(10), cy + u(10), outline="#5a2020", width=u(2))
                    c.create_oval(cx - u(7), cy - u(7), cx + u(7), cy + u(7), fill=RED, outline="")
            elif key == "pause":
                active = self.state in ("recording", "paused")
                glyph = "" if self.state == "paused" else ""
                c.create_text(cx, cy, text=glyph, font=icon,
                              fill=(RED if self.state == "paused" else FG) if active else "#4a4a50")
            else:
                glyph = {"shot": "", "settings": "", "min": "", "close": ""}[key]
                c.create_text(cx, cy, text=glyph, font=(self.icon_font_family, 10 if key in ("min", "close") else 11),
                              fill=FG if key != "close" or self.hover != "close" else RED)
        # timer + status
        tx = u(88)
        el = self.elapsed()
        h, rem = divmod(int(el), 3600)
        m, s = divmod(rem, 60)
        timer = f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
        rec_on = self.state == "recording"
        c.create_text(tx, H // 2 - u(7), text=timer, anchor="w", font=("Segoe UI Semibold", 13),
                      fill=RED if rec_on else FG)
        if rec_on and int(time.time() * 2) % 2 == 0:
            c.create_oval(tx + u(60), H // 2 - u(10), tx + u(66), H // 2 - u(4), fill=RED, outline="")
        c.create_text(tx, H // 2 + u(10), text=self.status_text(), anchor="w", font=("Segoe UI", 8), fill=FG_DIM)

    def status_text(self):
        if self.hover:
            return next(t for k, _, t in self.btns if k == self.hover)
        if self.flash and time.time() < self.flash[1]:
            return self.flash[0]
        cfg = self.cfg
        if self.state == "recording":
            fps, size = self.rec.stats()
            target = int(cfg["fps"])
            # output is constant-frame-rate; progress arrives in bursts, so only show a number when the
            # encoder is genuinely falling behind real time
            f = f"{target} fps" if (fps is None or fps >= target * 0.95) else f"{fps:.0f}/{target} fps"
            return f"{f} · {size / 1048576:.0f} MB"
        if self.state == "paused":
            return "Paused"
        if self.state in ("starting",):
            return "Starting…"
        if self.state == "stopping":
            return "Saving…"
        if self.state == "countdown":
            return "Get ready · click to cancel"
        lab = cfg["out_res"]
        if lab == "Custom":
            res = f"{cfg['custom_w']}x{cfg['custom_h']}"
        else:
            mm = re.search(r"\((.+)\)", lab)
            res = mm.group(1) if mm else lab.split(" ")[0]
        area = {"full": "Screen", "region": "Region", "window": "Window"}[cfg["capture"]]
        return f"{area} · {res} · {cfg['fps']} fps"

    def elapsed(self):
        if self.state == "recording" and self.seg_t0:
            return self.acc + time.monotonic() - self.seg_t0
        return self.acc

    def notify(self, text, path=None, secs=4):
        self.flash = (text, time.time() + secs, path)

    # -- quick menu
    def _menu(self, e):
        m = tk.Menu(self.root, tearoff=0, bg=BG2, fg=FG, activebackground="#3f3f46", activeforeground=FG,
                    bd=0, font=("Segoe UI", 10))
        cap = tk.Menu(m, tearoff=0, bg=BG2, fg=FG, activebackground="#3f3f46", activeforeground=FG)
        mons = get_monitors()
        for i, mo in enumerate(mons):
            cap.add_command(label=f"Full screen {i + 1}  ({mo['w']}x{mo['h']})",
                            command=lambda i=i: self._set_cfg(capture="full", monitor=i))
        cap.add_command(label="Select region / window…", command=self.select_region)
        m.add_cascade(label="Capture", menu=cap)
        fm = tk.Menu(m, tearoff=0, bg=BG2, fg=FG, activebackground="#3f3f46", activeforeground=FG)
        for f in ["24", "30", "60", "90", "120", "144"]:
            fm.add_radiobutton(label=f"{f} fps", command=lambda f=f: self._set_cfg(fps=int(f)))
        m.add_cascade(label="Frame rate", menu=fm)
        rm = tk.Menu(m, tearoff=0, bg=BG2, fg=FG, activebackground="#3f3f46", activeforeground=FG)
        for lab in RESOLUTIONS:
            if lab != "Custom":
                rm.add_command(label=lab, command=lambda l=lab: self._set_cfg(out_res=l))
        m.add_cascade(label="Resolution", menu=rm)
        m.add_separator()
        m.add_checkbutton(label="System audio", onvalue=True, offvalue=False,
                          variable=tk.BooleanVar(value=self.cfg["sys_audio"]),
                          command=lambda: self._set_cfg(sys_audio=not self.cfg["sys_audio"]))
        m.add_checkbutton(label="Microphone", variable=tk.BooleanVar(value=self.cfg["mic"]),
                          command=lambda: self._set_cfg(mic=not self.cfg["mic"]))
        m.add_separator()
        m.add_command(label="Open recordings folder", command=self.open_folder)
        m.add_command(label="Settings…", command=self.open_settings)
        m.add_command(label="Quit", command=self.quit)
        m.tk_popup(e.x_root, e.y_root)

    def _set_cfg(self, **kw):
        if self.state != "idle" and any(k in kw for k in ("capture", "monitor", "fps", "out_res")):
            self.notify("Stop recording first")
            return
        self.cfg.update(kw)
        if kw.get("mic") and not self.cfg["mic_device"] and self.mics:
            self.cfg["mic_device"] = self.mics[0]
        save_cfg(self.cfg)
        self._draw()

    def open_folder(self):
        os.makedirs(self.cfg["out_dir"], exist_ok=True)
        os.startfile(self.cfg["out_dir"])

    # -- hotkeys
    def _start_hotkeys(self):
        if self.hk:
            self.hk.stop()
        self.hk = HotkeyThread({"record": self.cfg["hk_record"], "pause": self.cfg["hk_pause"],
                                "shot": self.cfg["hk_shot"], "toggle": self.cfg["hk_toggle"]}, self.q)
        self.hk.start()
        self.hk.ready.wait(2)
        if self.hk.failed:
            self.notify("Hotkey in use: " + ", ".join(self.hk.failed), secs=6)

    # -- geometry
    def resolve_geometry(self):
        """-> (dda_idx, ox, oy, w, h, full, virtual_rect, monitor) or None"""
        cfg, mons = self.cfg, get_monitors()
        if cfg["capture"] == "full":
            i = min(max(0, cfg["monitor"]), len(mons) - 1)
            m = mons[i]
            return (i, 0, 0, m["w"], m["h"], True, (m["x"], m["y"], m["w"], m["h"]), m)
        if cfg["capture"] == "window":
            h = self.window_hwnd
            if not h or not user32.IsWindow(h):
                h = next((hw for hw, t in list_windows() if t == cfg["window_title"]), None)
            if not h:
                return None
            if user32.IsIconic(h):
                user32.ShowWindow(h, 9)
            self.window_hwnd = h
            rect = window_rect(h)
        else:
            if not cfg["region"]:
                return None
            rect = cfg["region"]
        x, y, w, hh = rect
        best, area = 0, -1
        for i, m in enumerate(mons):
            ix = max(0, min(x + w, m["x"] + m["w"]) - max(x, m["x"]))
            iy = max(0, min(y + hh, m["y"] + m["h"]) - max(y, m["y"]))
            if ix * iy > area:
                best, area = i, ix * iy
        m = mons[best]
        x0, y0 = max(x, m["x"]), max(y, m["y"])
        x1, y1 = min(x + w, m["x"] + m["w"]), min(y + hh, m["y"] + m["h"])
        cw, ch = even(x1 - x0), even(y1 - y0)
        if cw < 32 or ch < 32:
            return None
        ox, oy = x0 - m["x"], y0 - m["y"]
        full = (ox, oy, cw, ch) == (0, 0, m["w"], m["h"])
        return (best, ox, oy, cw, ch, full, (x0, y0, cw, ch), m)

    @staticmethod
    def cap_geo(g):
        """App geometry -> Recorder geometry (dda_idx, ox, oy, w, h, full, monitor)."""
        return tuple(g[:6]) + (g[7],)

    def _start_heartbeat(self, g):
        if not self.heartbeat:
            self.heartbeat = Heartbeat(self.root, g[7], None if g[5] else g[6])

    def _stop_heartbeat(self):
        if self.heartbeat:
            self.heartbeat.destroy()
            self.heartbeat = None

    def select_region(self, then_record=False):
        if self.state != "idle":
            return
        self.root.withdraw()

        def done(res):
            self._show_bar()
            if not res:
                return
            if res[0] == "region":
                self.cfg.update(capture="region", region=res[1])
            else:
                _, hwnd, title, rect = res
                self.window_hwnd = hwnd
                self.cfg.update(capture="window", window_title=title, region=list(rect))
            save_cfg(self.cfg)
            self._draw()
            g = self.resolve_geometry()
            if g:
                b = BorderOverlay(self.root, *g[6], self.S)
                self.root.after(1200, b.destroy)
            if self.settings_win:
                self.settings_win.refresh_capture()
            if then_record:
                self.root.after(300, self.toggle_record)

        self.root.after(150, lambda: RegionSelector(self, done))

    # -- actions
    def toggle_record(self):
        if self.state == "idle":
            geo = self.resolve_geometry()
            if geo is None:
                if self.cfg["capture"] == "window":
                    self.notify("Window not found – pick again")
                self.select_region(then_record=True)
                return
            self.geo = geo
            os.makedirs(self.cfg["out_dir"], exist_ok=True)
            if self.cfg["border"] and not geo[5]:
                self.border = BorderOverlay(self.root, *geo[6], self.S)
            secs = int(self.cfg["countdown"] or 0)
            if secs > 0:
                self.state = "countdown"
                x, y, w, h = geo[6]
                self.countdown = Countdown(self.root, x + w // 2, y + h // 2, secs, self.S, self._go)
            else:
                self._go()
            self._draw()
        elif self.state == "countdown":
            self.countdown.cancel()
            self._end_border()
            self.state = "idle"
            self._draw()
        elif self.state in ("recording", "paused"):
            self.stop()

    def _go(self):
        self.countdown = None
        self.state = "starting"
        self.acc, self.seg_t0, self.recover_count = 0.0, None, 0
        if self.cfg["mic"] and not self.cfg["mic_device"] and self.mics:
            self.cfg["mic_device"] = self.mics[0]
        plans = make_plans(self.cfg, self.avail)
        geo = self.cap_geo(self.geo)
        self._start_heartbeat(self.geo)
        if on_battery():
            self.notify("On battery · plug in for the steadiest fps", secs=6)

        def work():
            err = self.rec.begin(self.cfg, geo, plans)
            self.q.put(("started", err))

        threading.Thread(target=work, daemon=True).start()
        self._draw()

    def toggle_pause(self):
        if self.state == "recording":
            self.acc = self.elapsed()
            self.seg_t0 = None
            self.state = "pausing"

            def work():
                self.rec.stop_segment()
                self.q.put(("paused", None))

            threading.Thread(target=work, daemon=True).start()
        elif self.state == "paused":
            self.state = "starting"

            def work():
                self.q.put(("started", self.rec.new_segment()))

            threading.Thread(target=work, daemon=True).start()
        self._draw()

    def stop(self):
        if self.state not in ("recording", "paused"):
            return
        self.acc = self.elapsed()
        self.seg_t0 = None
        self.state = "stopping"

        def work():
            self.rec.stop_segment()
            self.q.put(("saved", self.rec.finalize()))

        threading.Thread(target=work, daemon=True).start()
        self._draw()

    def _end_border(self):
        if self.border:
            self.border.destroy()
            self.border = None

    def screenshot(self):
        geo = self.resolve_geometry() if self.state == "idle" else self.geo
        if geo is None:
            self.notify("Select a region first")
            return
        os.makedirs(self.cfg["out_dir"], exist_ok=True)
        path = os.path.join(self.cfg["out_dir"], f"SmoothRecord_{datetime.now():%Y-%m-%d_%H-%M-%S}.png")
        temp_hb = self.heartbeat is None
        self._start_heartbeat(geo)
        wgc = self.avail is None or self.avail.get("_wgc", True)

        def work():
            ok = take_screenshot(self.ffmpeg, self.cap_geo(geo), self.cfg["cursor"], path, wgc)
            self.q.put(("shot", (path if ok else None, temp_hb)))

        threading.Thread(target=work, daemon=True).start()

    # -- main loop
    def tick(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                self._handle(kind, data)
        except queue.Empty:
            pass
        if self.state == "recording":
            if self.rec.segment_died():
                self._recover()
            elif self.cfg["auto_stop"] and self.elapsed() >= float(self.cfg["auto_stop"]) * 60:
                self.notify("Auto-stopped")
                self.stop()
        self._draw()
        self.root.after(250 if self.state != "recording" else 200, self.tick)

    def _recover(self):
        self.acc = self.elapsed()
        self.seg_t0 = None
        self.rec.abandon_dead_segment()
        self.recover_count += 1
        if self.recover_count > 5:
            self.notify("Capture lost – saving")
            self.state = "paused"
            self.stop()
            return
        self.state = "starting"
        self.notify("Capture interrupted – resuming")

        def work():
            time.sleep(0.5)
            self.q.put(("started", self.rec.new_segment()))

        threading.Thread(target=work, daemon=True).start()

    def _handle(self, kind, data):
        if kind == "hotkey":
            {"record": self.toggle_record, "pause": self.toggle_pause, "shot": self.screenshot,
             "toggle": self.toggle_bar}[data]()
        elif kind == "detected":
            self.avail, self.mics, self.speakers = data
            if self.settings_win:
                self.settings_win.refresh_devices()
        elif kind == "started":
            if data is None:
                self.state = "recording"
                self.seg_t0 = self.rec.current.t0
                plan = self.rec.current.plan
                if list(plan) != self.cfg.get("last_plan"):
                    self.cfg["last_plan"] = list(plan)
                    save_cfg(self.cfg)
            else:
                self._end_border()
                had = bool(self.rec.segments)
                self.state = "paused" if had else "idle"
                if not had:
                    self._stop_heartbeat()
                messagebox.showerror(APP, "Could not start recording.\n\n" + data +
                                     f"\n\nFull log: {LOG_PATH}")
                if had:
                    self.stop()
        elif kind == "paused":
            self.state = "paused"
        elif kind == "saved":
            path, err = data
            self._end_border()
            self._stop_heartbeat()
            self.state = "idle"
            self.acc = 0.0
            if err:
                messagebox.showwarning(APP, err)
            if path:
                self.notify("Saved · click to open", path, secs=8)
                if self.cfg["open_after"]:
                    subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        elif kind == "shot":
            path, temp_hb = data
            if temp_hb and self.state == "idle":
                self._stop_heartbeat()
            self.notify("Screenshot saved · click to open" if path else "Screenshot failed", path)
        self._draw()

    def open_settings(self):
        if self.settings_win:
            self.settings_win.top.lift()
            return
        self.settings_win = SettingsWindow(self)

    def apply_settings(self):
        save_cfg(self.cfg)
        exclude_from_capture(self.root, self.cfg["exclude_ui"])
        self._make_btns()
        self._bar_key = None
        self._start_hotkeys()
        self._draw()

    def quit(self):
        if self.state in ("recording", "paused"):
            if not messagebox.askyesno(APP, "Stop and save the recording, then quit?"):
                return
            self.rec.stop_segment()
            path, err = self.rec.finalize()
        try:
            self.cfg["pos"] = [self.root.winfo_x(), self.root.winfo_y()]
            save_cfg(self.cfg)
        except Exception:
            pass
        if self.hk:
            self.hk.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# ---- settings window (modern, Windows 11 style) ----------------------------------------
# Every control is drawn with Pillow at 3x and downsampled, so edges stay smooth at any display scaling.
UI_WIN, UI_SIDE, UI_CARD, UI_CARD_BD = "#141416", "#141416", "#1e1e22", "#2a2a30"
UI_FIELD, UI_FIELD_HV, UI_FIELD_BD = "#2a2a30", "#323239", "#3a3a43"
UI_TXT, UI_DIM, UI_ACC, UI_ACC_HV = "#ececef", "#9a9aa3", "#ef4444", "#f26464"


def _pt(pt):
    """Font points -> logical pixels (matches Tk's own point sizes)."""
    return pt * 4 / 3


def text_w(text, kind="ui", pt=10):
    return pil_font(kind, 100).getlength(text) * _pt(pt) / 100


def render_img(w, h, S, bg, paint):
    SS = 3
    k = S * SS
    img = Image.new("RGB", (max(1, round(w * k)), max(1, round(h * k))), bg)
    d = ImageDraw.Draw(img)
    paint(d, lambda v: round(v * k))
    return ImageTk.PhotoImage(img.resize((max(1, round(w * S)), max(1, round(h * S))), Image.LANCZOS))


def ellipsize(text, kind, pt, maxw):
    if text_w(text, kind, pt) <= maxw:
        return text
    while text and text_w(text + "…", kind, pt) > maxw:
        text = text[:-1]
    return text + "…"


class PilWidget(tk.Canvas):
    def __init__(self, parent, w, h, S, bg=UI_CARD, cursor="hand2"):
        super().__init__(parent, width=round(w * S), height=round(h * S), bg=bg, highlightthickness=0, bd=0,
                         cursor=cursor)
        self.w, self.h, self.S, self.bgc = w, h, S, bg
        self.hover = False
        self._iid = self.create_image(0, 0, anchor="nw")
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))

    def _set_hover(self, v):
        if v != self.hover:
            self.hover = v
            self.redraw()

    def redraw(self):
        self._img = render_img(self.w, self.h, self.S, self.bgc, self.paint)
        self.itemconfigure(self._iid, image=self._img)

    def paint(self, d, u):
        pass


class Toggle(PilWidget):
    def __init__(self, parent, S, value, on_change):
        super().__init__(parent, 84, 26, S)
        self.value, self.cb = bool(value), on_change
        self.bind("<Button-1>", self._click)
        self.redraw()

    def _click(self, e):
        self.value = not self.value
        self.redraw()
        self.cb(self.value)

    def paint(self, d, u):
        d.text((u(30), u(13)), "On" if self.value else "Off", font=pil_font("ui", u(_pt(10))), fill=UI_TXT,
               anchor="rm")
        x0, x1, y0, y1 = 38, 82, 3, 23
        if self.value:
            d.rounded_rectangle([u(x0), u(y0), u(x1), u(y1)], radius=u(10),
                                fill=UI_ACC_HV if self.hover else UI_ACC)
            cx, r, col = x1 - 10, 6, "white"
        else:
            d.rounded_rectangle([u(x0), u(y0), u(x1), u(y1)], radius=u(10),
                                fill=UI_FIELD_HV if self.hover else UI_CARD, outline="#9a9aa3", width=u(1.4))
            cx, r, col = x0 + 10, 5, "#c9c9cf"
        d.ellipse([u(cx - r), u(13 - r), u(cx + r), u(13 + r)], fill=col)


class Segmented(PilWidget):
    PAD = 14

    def __init__(self, parent, S, options, value, on_change):
        self.opts = options
        self.widths = [text_w(lbl, "ui", 10) + 2 * self.PAD for _, lbl in options]
        super().__init__(parent, sum(self.widths) + 6, 32, S)
        self.value, self.cb, self.hidx = value, on_change, None
        self.bind("<Motion>", self._motion)
        self.bind("<Button-1>", self._click)
        self.redraw()

    def _idx(self, x):
        x = x / self.S - 3
        for i, w in enumerate(self.widths):
            if x < w:
                return i
            x -= w
        return len(self.widths) - 1

    def _motion(self, e):
        i = self._idx(e.x)
        if i != self.hidx:
            self.hidx = i
            self.redraw()

    def _set_hover(self, v):
        if not v:
            self.hidx = None
        super()._set_hover(v)

    def _click(self, e):
        v = self.opts[self._idx(e.x)][0]
        if v != self.value:
            self.value = v
            self.redraw()
            self.cb(v)

    def paint(self, d, u):
        d.rounded_rectangle([0, 0, u(self.w) - 1, u(self.h) - 1], radius=u(8), fill=UI_FIELD, outline=UI_FIELD_BD,
                            width=u(1))
        x = 3
        f = pil_font("ui", u(_pt(10)))
        for i, ((val, lbl), w) in enumerate(zip(self.opts, self.widths)):
            sel = val == self.value
            if sel:
                d.rounded_rectangle([u(x), u(3), u(x + w), u(self.h - 3)], radius=u(6), fill="#43434d")
                d.rounded_rectangle([u(x + w / 2 - 8), u(self.h - 6), u(x + w / 2 + 8), u(self.h - 4)],
                                    radius=u(1), fill=UI_ACC)
            elif i == self.hidx:
                d.rounded_rectangle([u(x), u(3), u(x + w), u(self.h - 3)], radius=u(6), fill=UI_FIELD_HV)
            d.text((u(x + w / 2), u(self.h / 2 - 1)), lbl, font=f, fill=UI_TXT if sel or i == self.hidx else UI_DIM,
                   anchor="mm")
            x += w


class Btn(PilWidget):
    def __init__(self, parent, S, text, on_click, accent=False, glyph=None, bg=UI_CARD):
        self.text, self.glyph, self.accent = text, glyph, accent
        w = (text_w(text, "ui", 10) if text else 0) + (22 if glyph else 0) + (28 if text else 12)
        super().__init__(parent, w, 32, S, bg=bg)
        self.bind("<Button-1>", lambda e: on_click())
        self.redraw()

    def paint(self, d, u):
        fill = (UI_ACC_HV if self.hover else UI_ACC) if self.accent else (UI_FIELD_HV if self.hover else UI_FIELD)
        d.rounded_rectangle([0, 0, u(self.w) - 1, u(self.h) - 1], radius=u(8), fill=fill,
                            outline=None if self.accent else UI_FIELD_BD, width=u(1))
        x = 14 if self.text else self.w / 2
        if self.glyph:
            d.text((u(x + (8 if self.text else 0)), u(16)), self.glyph, font=pil_font("icons", u(_pt(10))),
                   fill="white" if self.accent else UI_TXT, anchor="mm")
            x += 22
        if self.text:
            d.text((u(x), u(15.5)), self.text, font=pil_font("ui", u(_pt(10))),
                   fill="white" if self.accent else UI_TXT, anchor="lm")


class Field(PilWidget):
    """Rounded text field with an accent underline while focused."""

    def __init__(self, parent, S, value, on_commit, width=200, justify="left"):
        super().__init__(parent, width, 32, S, cursor="xterm")
        self.focused, self.cb, self.last = False, on_commit, str(value)
        self.var = tk.StringVar(value=str(value))
        self.entry = tk.Entry(self, textvariable=self.var, bd=0, relief="flat", bg=UI_FIELD, fg=UI_TXT,
                              insertbackground=UI_TXT, font=("Segoe UI", 10), highlightthickness=0,
                              justify=justify, selectbackground="#4a4a55", selectforeground=UI_TXT)
        self.create_window(round(11 * S), round(16 * S), anchor="w", window=self.entry, width=round((width - 22) * S))
        self.entry.bind("<FocusIn>", lambda e: self._focus(True))
        self.entry.bind("<FocusOut>", lambda e: (self._focus(False), self.commit()))
        self.entry.bind("<Return>", lambda e: (self.commit(), self.winfo_toplevel().focus_set()))
        self.entry.bind("<Escape>", lambda e: (self.var.set(self.last), self.winfo_toplevel().focus_set()))
        self.bind("<Button-1>", lambda e: self.entry.focus_set())
        self.redraw()

    def _focus(self, v):
        self.focused = v
        self.redraw()

    def commit(self):
        v = self.var.get().strip()
        if v == self.last:
            return
        if self.cb(v) is False:
            self.var.set(self.last)  # rejected
        else:
            self.last = v

    def paint(self, d, u):
        bg = UI_FIELD_HV if (self.hover and not self.focused) else UI_FIELD
        self.entry.configure(bg=bg)
        d.rounded_rectangle([0, 0, u(self.w) - 1, u(self.h) - 1], radius=u(7), fill=bg, outline=UI_FIELD_BD,
                            width=u(1))
        if self.focused:
            d.rounded_rectangle([u(1), u(self.h - 3), u(self.w - 1), u(self.h) - 1], radius=u(2), fill=UI_ACC)


class HotkeyField(Field):
    """Click, then press the shortcut you want."""
    NAMES = {"prior": "pageup", "next": "pagedown", "print": "printscreen", "scroll_lock": "scrolllock",
             "pause": "pause", "space": "space", "insert": "insert", "delete": "delete", "home": "home",
             "end": "end", "left": "left", "right": "right", "up": "up", "down": "down"}

    @staticmethod
    def pretty(combo):
        return " + ".join(p.capitalize() if len(p) > 1 else p.upper() for p in combo.split("+")) if combo else ""

    def __init__(self, parent, S, value, on_commit, app, width=190):
        super().__init__(parent, S, self.pretty(value), on_commit, width, justify="center")
        self.app = app
        self.entry.bind("<FocusIn>", self._fin)
        self.entry.bind("<FocusOut>", self._fout)
        self.entry.bind("<KeyPress>", self._key)

    def _fin(self, e):
        self._focus(True)
        if self.app.hk:
            self.app.hk.stop()  # don't trigger the current shortcut while recording a new one
        self.var.set("Press a shortcut…")

    def _fout(self, e):
        self._focus(False)
        if self.var.get() == "Press a shortcut…":
            self.var.set(self.last)
        self.app._start_hotkeys()

    def _key(self, e):
        ks = e.keysym.lower()
        if ks in ("control_l", "control_r", "shift_l", "shift_r", "alt_l", "alt_r", "win_l", "win_r",
                  "super_l", "super_r", "caps_lock"):
            return "break"
        if ks == "escape":
            self.var.set(self.last)
            self.winfo_toplevel().focus_set()
            return "break"
        mods = []
        if e.state & 0x4:
            mods.append("ctrl")
        if e.state & 0x20000 or e.state & 0x8:
            mods.append("alt")
        if e.state & 0x1:
            mods.append("shift")
        if ks in ("backspace",) and not mods:
            self.var.set("")
            self.commit()
            self.winfo_toplevel().focus_set()
            return "break"
        key = ks if (len(ks) == 1 and ks.isalnum()) else self.NAMES.get(ks, ks if re.fullmatch(r"f\d{1,2}", ks) else None)
        if not key:
            return "break"
        if not mods and not re.fullmatch(r"f\d{1,2}|pause|printscreen|scrolllock", key):
            return "break"  # plain letters would hijack typing everywhere
        self.var.set(self.pretty("+".join(mods + [key])))
        self.commit()
        self.winfo_toplevel().focus_set()
        return "break"


class Slider(PilWidget):
    def __init__(self, parent, S, value, on_change, lo=0, hi=200, width=230, unit="%"):
        super().__init__(parent, width, 30, S)
        self.value, self.cb, self.lo, self.hi, self.unit = int(value), on_change, lo, hi, unit
        self.bind("<Button-1>", self._drag)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", lambda e: self.cb(self.value))
        self.redraw()

    def _x(self):
        return 10, self.w - 62

    def _drag(self, e):
        x0, x1 = self._x()
        f = min(1, max(0, (e.x / self.S - x0) / (x1 - x0)))
        v = int(round(self.lo + f * (self.hi - self.lo)))
        if v != self.value:
            self.value = v
            self.redraw()

    def paint(self, d, u):
        x0, x1 = self._x()
        cy = 15
        fx = x0 + (self.value - self.lo) / (self.hi - self.lo) * (x1 - x0)
        d.rounded_rectangle([u(x0), u(cy - 2), u(x1), u(cy + 2)], radius=u(2), fill="#4a4a54")
        d.rounded_rectangle([u(x0), u(cy - 2), u(fx), u(cy + 2)], radius=u(2), fill=UI_ACC)
        d.ellipse([u(fx - 9), u(cy - 9), u(fx + 9), u(cy + 9)], fill="#3a3a43", outline="#55555f", width=u(1))
        r = 6 if self.hover else 5
        d.ellipse([u(fx - r), u(cy - r), u(fx + r), u(cy + r)], fill=UI_ACC)
        d.text((u(self.w - 4), u(cy)), f"{self.value}{self.unit}", font=pil_font("ui", u(_pt(10))), fill=UI_TXT,
               anchor="rm")


class Popup:
    """Modern dropdown list (replaces native menus)."""
    ROW = 34

    def __init__(self, anchor, S, options, value, on_pick, width):
        self.S, self.opts, self.value, self.cb, self.width = S, options, value, on_pick, width
        self.vis = min(len(options), 9)
        self.off = max(0, min(len(options) - self.vis, next((i for i, (v, _) in enumerate(options) if v == value), 0) - 3))
        self.hidx = None
        h = self.vis * self.ROW + 8
        t = self.top = tk.Toplevel(anchor)
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        x, y = anchor.winfo_rootx(), anchor.winfo_rooty() + anchor.winfo_height() + round(4 * S)
        if y + h * S > anchor.winfo_screenheight():
            y = anchor.winfo_rooty() - round((h + 4) * S)
        t.geometry(f"{round(width * S)}x{round(h * S)}+{x}+{y}")
        self.cv = tk.Canvas(t, width=round(width * S), height=round(h * S), highlightthickness=0, bd=0, bg=UI_CARD)
        self.cv.pack()
        self._iid = self.cv.create_image(0, 0, anchor="nw")
        self.cv.bind("<Motion>", self._motion)
        self.cv.bind("<Button-1>", self._click)
        t.bind("<MouseWheel>", self._wheel)
        t.bind("<Escape>", lambda e: self.close())
        t.bind("<FocusOut>", lambda e: self.close())
        t.update_idletasks()
        try:
            pref = ctypes.c_int(2)
            dwmapi.DwmSetWindowAttribute(top_hwnd(t), 33, ctypes.byref(pref), 4)
        except Exception:
            pass
        self.redraw()
        t.grab_set()
        t.focus_force()

    def _row(self, y):
        i = int((y / self.S - 4) // self.ROW)
        return self.off + i if 0 <= i < self.vis else None

    def _motion(self, e):
        i = self._row(e.y)
        if i != self.hidx:
            self.hidx = i
            self.redraw()

    def _wheel(self, e):
        self.off = max(0, min(len(self.opts) - self.vis, self.off + (-1 if e.delta > 0 else 1)))
        self.redraw()

    def _click(self, e):
        if not (0 <= e.x < self.cv.winfo_width() and 0 <= e.y < self.cv.winfo_height()):
            return self.close()
        i = self._row(e.y)
        if i is not None:
            self.close()
            self.cb(self.opts[i][0])

    def close(self):
        try:
            self.top.grab_release()
            self.top.destroy()
        except tk.TclError:
            pass

    def redraw(self):
        w, h = self.width, self.vis * self.ROW + 8

        def paint(d, u):
            d.rounded_rectangle([0, 0, u(w) - 1, u(h) - 1], radius=u(8), fill="#232328", outline=UI_FIELD_BD,
                                width=u(1))
            f = pil_font("ui", u(_pt(10)))
            for j in range(self.vis):
                i = self.off + j
                val, lbl = self.opts[i]
                y = 4 + j * self.ROW
                if i == self.hidx or val == self.value:
                    d.rounded_rectangle([u(4), u(y + 2), u(w - 4), u(y + self.ROW - 2)], radius=u(6),
                                        fill="#34343c" if i == self.hidx else "#2c2c33")
                if val == self.value:
                    d.rounded_rectangle([u(5), u(y + 10), u(8), u(y + self.ROW - 10)], radius=u(2), fill=UI_ACC)
                d.text((u(16), u(y + self.ROW / 2)), ellipsize(lbl, "ui", 10, w - 30), font=f, fill=UI_TXT,
                       anchor="lm")
            if len(self.opts) > self.vis:  # scroll hint
                th = h * self.vis / len(self.opts)
                ty = (h - th) * self.off / (len(self.opts) - self.vis)
                d.rounded_rectangle([u(w - 5), u(ty + 4), u(w - 3), u(ty + th - 4)], radius=u(1), fill="#55555f")

        self._img = render_img(w, h, self.S, UI_CARD, paint)
        self.cv.itemconfigure(self._iid, image=self._img)


class Dropdown(PilWidget):
    def __init__(self, parent, S, options, value, on_change, width=240, editable=False, validate=None):
        super().__init__(parent, width, 32, S)
        self.opts, self.value, self.cb, self.editable, self.validate = options, value, on_change, editable, validate
        self.entry = None
        if editable:
            self.var = tk.StringVar(value=str(value))
            self.entry = tk.Entry(self, textvariable=self.var, bd=0, relief="flat", bg=UI_FIELD, fg=UI_TXT,
                                  insertbackground=UI_TXT, font=("Segoe UI", 10), highlightthickness=0)
            self.create_window(round(11 * S), round(16 * S), anchor="w", window=self.entry,
                               width=round((width - 50) * S))
            self.entry.bind("<Return>", lambda e: (self._commit(), self.winfo_toplevel().focus_set()))
            self.entry.bind("<FocusOut>", lambda e: self._commit())
        self.bind("<Button-1>", self._open)
        self.redraw()

    def _commit(self):
        v = self.var.get().strip()
        ok = self.validate(v) if self.validate else v
        if ok is None:
            self.var.set(str(self.value))
            return
        if ok != self.value:
            self.value = ok
            self.var.set(str(ok))
            self.cb(ok)

    def _open(self, e):
        if self.editable and e.x < (self.w - 40) * self.S:
            self.entry.focus_set()
            return
        Popup(self, self.S, self.opts, self.value, self._pick, self.w)

    def _pick(self, v):
        self.value = v
        if self.entry:
            self.var.set(str(v))
        self.redraw()
        self.cb(v)

    def paint(self, d, u):
        bg = UI_FIELD_HV if self.hover else UI_FIELD
        if self.entry:
            self.entry.configure(bg=bg)
        d.rounded_rectangle([0, 0, u(self.w) - 1, u(self.h) - 1], radius=u(7), fill=bg, outline=UI_FIELD_BD,
                            width=u(1))
        if not self.editable:
            lbl = next((l for v, l in self.opts if v == self.value), str(self.value))
            d.text((u(12), u(16)), ellipsize(lbl, "ui", 10, self.w - 44), font=pil_font("ui", u(_pt(10))),
                   fill=UI_TXT, anchor="lm")
        d.text((u(self.w - 18), u(16.5)), "", font=pil_font("icons", u(_pt(8))), fill=UI_DIM, anchor="mm")


class SettingsWindow:
    NAV = [("video", "", "Video"), ("encoding", "", "Encoding"), ("audio", "", "Audio"),
           ("general", "", "General"), ("hotkeys", "", "Shortcuts")]
    SIDE_W, CONTENT_W, WIN_H = 210, 600, 640
    REBUILD = {"capture", "out_res", "monitor", "fps", "scale_mode", "custom_w", "custom_h", "region",
               "window_title", "sys_audio", "mic", "chroma444", "encoder", "codec"}

    def __init__(self, app):
        self.app, self.S = app, app.S
        S = self.S
        t = self.top = tk.Toplevel(app.root)
        t.title("SmoothRecord settings")
        t.configure(bg=UI_WIN)
        t.resizable(False, False)
        t.protocol("WM_DELETE_WINDOW", self.close)
        W = self.SIDE_W + self.CONTENT_W
        self.page = "video"
        self.windows = []
        self.side = tk.Canvas(t, width=round(self.SIDE_W * S), height=round(self.WIN_H * S), bg=UI_SIDE,
                              highlightthickness=0, bd=0)
        self.side.pack(side="left", fill="y")
        self._side_iid = self.side.create_image(0, 0, anchor="nw")
        self.side_hover = None
        self.side.bind("<Motion>", self._side_motion)
        self.side.bind("<Leave>", lambda e: self._side_set_hover(None))
        self.side.bind("<Button-1>", self._side_click)
        self.cv = tk.Canvas(t, width=round(self.CONTENT_W * S), height=round(self.WIN_H * S), bg=UI_WIN,
                            highlightthickness=0, bd=0, yscrollincrement=1)
        self.cv.pack(side="left", fill="both", expand=True)
        t.bind("<MouseWheel>", self._wheel)
        self.widgets, self.imgs = [], []
        self.draw_side()
        self.render_page()
        t.update_idletasks()
        dark_titlebar(t)
        exclude_from_capture(t, app.cfg["exclude_ui"])
        sw, sh = t.winfo_screenwidth(), t.winfo_screenheight()
        x = min(max(0, app.root.winfo_x()), sw - round(W * S) - 10)
        y = min(app.root.winfo_y() + app.BH + round(10 * S), sh - round(self.WIN_H * S) - round(60 * S))
        t.geometry(f"+{x}+{max(0, y)}")
        t.focus_force()

    # ---- sidebar
    def draw_side(self):
        S, W, H = self.S, self.SIDE_W, self.WIN_H

        def paint(d, u):
            d.ellipse([u(22), u(26), u(34), u(38)], fill=UI_ACC)
            d.text((u(44), u(32)), "SmoothRecord", font=pil_font("semi", u(_pt(13))), fill=UI_TXT, anchor="lm")
            for i, (key, glyph, label) in enumerate(self.NAV):
                y = 74 + i * 44
                if key == self.page or key == self.side_hover:
                    d.rounded_rectangle([u(10), u(y), u(W - 10), u(y + 38)], radius=u(7),
                                        fill="#27272d" if key == self.page else "#1e1e23")
                if key == self.page:
                    d.rounded_rectangle([u(10), u(y + 11), u(13), u(y + 27)], radius=u(2), fill=UI_ACC)
                d.text((u(34), u(y + 19)), glyph, font=pil_font("icons", u(_pt(11))),
                       fill=UI_TXT if key == self.page else UI_DIM, anchor="mm")
                d.text((u(54), u(y + 19)), label, font=pil_font("ui", u(_pt(10.5))),
                       fill=UI_TXT if key == self.page else "#c4c4cb", anchor="lm")
            d.line([u(22), u(H - 86), u(W - 22), u(H - 86)], fill="#26262c", width=max(1, u(1)))
            d.text((u(22), u(H - 66)), "Made by", font=pil_font("ui", u(_pt(8.5))), fill="#6e6e78", anchor="lm")
            d.text((u(22), u(H - 46)), APP_AUTHOR, font=pil_font("semi", u(_pt(10))), fill=UI_TXT, anchor="lm")
            d.text((u(22), u(H - 22)), f"v{APP_VERSION}  ·  Changes save automatically",
                   font=pil_font("ui", u(_pt(8))), fill="#6e6e78", anchor="lm")

        self._side_img = render_img(W, H, S, UI_SIDE, paint)
        self.side.itemconfigure(self._side_iid, image=self._side_img)

    def _side_key(self, y):
        i = int((y / self.S - 74) // 44)
        return self.NAV[i][0] if 0 <= i < len(self.NAV) and (y / self.S - 74) % 44 <= 38 else None

    def _side_set_hover(self, k):
        if k != self.side_hover:
            self.side_hover = k
            self.side.configure(cursor="hand2" if k else "")
            self.draw_side()

    def _side_motion(self, e):
        self._side_set_hover(self._side_key(e.y))

    def _side_click(self, e):
        k = self._side_key(e.y)
        if k and k != self.page:
            self.page = k
            self.draw_side()
            self.render_page(scroll_top=True)

    def _wheel(self, e):
        if isinstance(e.widget, tk.Canvas) and e.widget.winfo_toplevel() is not self.top:
            return
        self.cv.yview_scroll(int(-e.delta / 120 * 40 * self.S), "units")

    # ---- settings changes (applied immediately)
    def set(self, key, value):
        a, cfg = self.app, self.app.cfg
        if cfg.get(key) == value:
            return
        cfg[key] = value
        if key in ("encoder", "codec"):
            cfg["last_plan"] = None
        if key == "exclude_ui":
            exclude_from_capture(a.root, value)
            exclude_from_capture(self.top, value)
        if key == "mic" and value and not cfg["mic_device"] and a.mics:
            cfg["mic_device"] = a.mics[0]
        save_cfg(cfg)
        if key.startswith("hk_"):
            a._make_btns()
        a._bar_key = None
        a._draw()
        if key in self.REBUILD:
            self.top.after(1, self.render_page)

    # ---- page building
    def render_page(self, scroll_top=False):
        S, cv = self.S, self.cv
        u = lambda v: round(v * S)
        y_keep = 0 if scroll_top else cv.canvasy(0)
        for w in self.widgets:
            w.destroy()
        self.widgets, self.imgs = [], []
        cv.delete("all")
        x0, cw = 28, self.CONTENT_W - 56
        title = next(l for k, g, l in self.NAV if k == self.page)
        cv.create_text(u(x0), u(38), text=title, font=("Segoe UI Semibold", 19), fill=UI_TXT, anchor="w")
        y = 76
        for gtitle, rows in getattr(self, "page_" + self.page)():
            rows = [r for r in rows if r]
            if not rows:
                continue
            if gtitle:
                cv.create_text(u(x0 + 2), u(y + 8), text=gtitle, font=("Segoe UI Semibold", 10), fill=UI_DIM,
                               anchor="w")
                y += 26
            hs = [r.get("h", 66 if r.get("desc") else 54) for r in rows]
            ch = sum(hs)
            img = render_img(cw, ch, S, UI_WIN, lambda d, uu, ch=ch: d.rounded_rectangle(
                [0, 0, uu(cw) - 1, uu(ch) - 1], radius=uu(9), fill=UI_CARD, outline=UI_CARD_BD, width=uu(1)))
            self.imgs.append(img)
            cv.create_image(u(x0), u(y), image=img, anchor="nw")
            ry = y
            for i, (r, h) in enumerate(zip(rows, hs)):
                if i:
                    cv.create_line(u(x0 + 1), u(ry), u(x0 + cw - 1), u(ry), fill=UI_CARD_BD, width=max(1, u(1)))
                ctrl = r["make"](cv) if r.get("make") else None
                ctrl_w = (ctrl.winfo_reqwidth() / S + 24) if ctrl else 0
                tw = cw - 36 - ctrl_w
                cy = ry + h / 2
                if r.get("label") and r.get("desc"):
                    cv.create_text(u(x0 + 18), u(cy - 10), text=r["label"], font=("Segoe UI", 10), fill=UI_TXT,
                                   anchor="w")
                    cv.create_text(u(x0 + 18), u(cy + 10), text=r["desc"], font=("Segoe UI", 9), fill=UI_DIM,
                                   anchor="w", width=u(tw))
                elif r.get("label"):
                    cv.create_text(u(x0 + 18), u(cy), text=r["label"], font=("Segoe UI", 10), fill=UI_TXT,
                                   anchor="w")
                elif r.get("desc"):
                    cv.create_text(u(x0 + 18), u(cy), text=r["desc"], font=("Segoe UI", 9), fill=UI_DIM,
                                   anchor="w", width=u(cw - 36))
                if ctrl:
                    self.widgets.append(ctrl)
                    cv.create_window(u(x0 + cw - 18), u(cy), window=ctrl, anchor="e")
                ry += h
            y += ch + 22
        cv.configure(scrollregion=(0, 0, u(self.CONTENT_W), max(u(self.WIN_H), u(y + 10))))
        cv.yview_moveto(0 if scroll_top else y_keep / max(1, u(y + 10)))

    # ---- helpers that build controls bound to config keys
    def tg(self, key):
        return lambda p: Toggle(p, self.S, self.app.cfg[key], lambda v: self.set(key, v))

    def seg(self, key, options):
        return lambda p: Segmented(p, self.S, options, self.app.cfg[key], lambda v: self.set(key, v))

    def dd(self, key, options, width=260, **kw):
        return lambda p: Dropdown(p, self.S, options, self.app.cfg[key], lambda v: self.set(key, v), width, **kw)

    def num_field(self, key, lo, hi, width=90, as_float=False):
        def commit(v):
            try:
                n = float(v) if as_float else int(float(v))
            except ValueError:
                return False
            if not lo <= n <= hi:
                return False
            self.set(key, n)
        return lambda p: Field(p, self.S, self.app.cfg[key], commit, width, justify="center")

    def composite(self, *makers):
        def make(p):
            f = tk.Frame(p, bg=UI_CARD)
            for i, m in enumerate(makers):
                if isinstance(m, str):
                    tk.Label(f, text=m, bg=UI_CARD, fg=UI_DIM, font=("Segoe UI", 9)).pack(side="left", padx=(8, 4))
                else:
                    m(f).pack(side="left", padx=(0 if i == 0 else 6, 0))
            return f
        return make

    def capture_info(self):
        c, mons = self.app.cfg, get_monitors()
        if c["capture"] == "full":
            m = mons[min(c["monitor"], len(mons) - 1)]
            cw, ch = m["w"], m["h"]
        elif c["capture"] == "region" and c["region"]:
            cw, ch = even(c["region"][2]), even(c["region"][3])
        else:
            h = self.app.window_hwnd
            cw, ch = (even(window_rect(h)[2]), even(window_rect(h)[3])) if h and user32.IsWindow(h) else (0, 0)
        if not (cw and ch):
            return "Pick an area to see the output size."
        W, H = output_size(cw, ch, c)
        return f"Capture {cw} × {ch}   →   Output {W} × {H} at {c['fps']} fps"

    # ---- pages: list of (group title, [rows])
    def page_video(self):
        c, mons = self.app.cfg, get_monitors()
        mon_opts = [(i, f"Display {i + 1} · {m['w']}×{m['h']} · {m['hz']} Hz" + (" · Primary" if m["primary"] else ""))
                    for i, m in enumerate(mons)]
        hz = max(m["hz"] for m in mons)

        def set_region(i):
            def commit(v):
                try:
                    n = int(float(v))
                except ValueError:
                    return False
                reg = list(c["region"] or [0, 0, 1280, 720])
                reg[i] = n
                self.set("region", reg)
            return lambda p: Field(p, self.S, (c["region"] or [0, 0, 1280, 720])[i], commit, 64, justify="center")

        def window_dd(p):
            self.windows = list_windows()
            titles = [(t, t) for _, t in self.windows]
            if c["window_title"] and all(t != c["window_title"] for _, t in titles):
                titles.insert(0, (c["window_title"], c["window_title"] + "  (not open)"))

            def pick(v):
                self.app.window_hwnd = next((h for h, t in self.windows if t == v), None)
                self.set("window_title", v)
            return Dropdown(p, self.S, titles or [("", "No windows found")], c["window_title"], pick, 250)

        def fps_valid(v):
            try:
                n = int(float(v))
            except ValueError:
                return None
            return n if 1 <= n <= 240 else None

        def select_area():
            self.top.withdraw()
            self.app.select_region()
            self.top.after(400, self._wait_pick)

        return [
            ("Capture", [
                {"label": "Display", "make": self.dd("monitor", mon_opts, 300)},
                {"label": "Capture area", "make": self.composite(
                    self.seg("capture", [("full", "Full screen"), ("region", "Region"), ("window", "Window")]),
                    lambda p: Btn(p, self.S, "Select", select_area, glyph=""))},
                {"label": "Region", "desc": "Position and size in pixels", "make": self.composite(
                    "X", set_region(0), "Y", set_region(1), "W", set_region(2), "H", set_region(3))}
                if c["capture"] == "region" else None,
                {"label": "Window", "make": self.composite(
                    window_dd, lambda p: Btn(p, self.S, "", lambda: self.render_page(), glyph=""))}
                if c["capture"] == "window" else None,
                {"label": "Show mouse cursor", "make": self.tg("cursor")},
            ]),
            ("Output", [
                {"label": "Resolution", "make": self.dd("out_res", [(k, k) for k in RESOLUTIONS], 230)},
                {"label": "Custom size", "make": self.composite(
                    self.num_field("custom_w", 16, 7680, 80), "×", self.num_field("custom_h", 16, 4320, 80))}
                if c["out_res"] == "Custom" else None,
                {"label": "Scaling", "make": self.seg("scale_mode", [("Fit", "Keep aspect"), ("Stretch", "Stretch")])},
                {"label": "Frame rate", "desc": f"Type any value from 1 to 240. Your display runs at {hz} Hz.",
                 "make": lambda p: Dropdown(p, self.S, [(int(f), f"{f} fps") for f in FPS_CHOICES], c["fps"],
                                            lambda v: self.set("fps", v), 130, editable=True, validate=fps_valid)},
                {"desc": self.capture_info(), "h": 46},
            ]),
        ]

    def page_encoding(self):
        a, c = self.app, self.app.cfg
        opts = [("auto", "Auto (best available)")]
        vend = [v for v in (a.avail or {}) if not v.startswith("_")] if a.avail is not None else ["qsv", "nvenc", "amf"]
        opts += [(v, VENDORS[v]) for v in vend] + [("x264", VENDORS["x264"])]
        if a.avail is None:
            det = "Detecting hardware encoders…"
        else:
            found = [f"{VENDORS[v]} ({', '.join(CODECS[x].split(' ')[0] for x in cs)})"
                     for v, cs in a.avail.items() if not v.startswith("_")]
            det = ("Found: " + ", ".join(found) if found else "No GPU encoder found, so the CPU is used") + \
                  (" · Colour-exact capture: on" if a.avail.get("_wgc") else " · Update FFmpeg for colour-exact capture")
        c444 = bool(c.get("chroma444"))
        return [
            ("Encoder", [
                {"label": "Encoder", "make": self.dd("encoder", opts, 250)},
                {"label": "Codec", "desc": "Full colour mode always uses HEVC" if c444 else
                 "HEVC and AV1 give smaller files at the same quality",
                 "make": self.seg("codec", [("h264", "H.264"), ("hevc", "HEVC"), ("av1", "AV1")])},
                {"desc": det, "h": 46},
            ]),
            ("Quality", [
                {"label": "Quality", "make": self.seg("quality", [("High", "High"), ("Balanced", "Balanced"),
                                                                   ("Small file", "Small file")])},
                {"label": "Bitrate", "desc": "Megabits per second · 0 = constant quality (recommended)",
                 "make": self.num_field("bitrate", 0, 500, 90, as_float=True)},
                {"label": "Full colour (4:4:4)", "desc": "Sharpest coloured text; needs VLC, mpv or an editor to play",
                 "make": self.tg("chroma444")},
            ]),
            ("File", [
                {"label": "Format", "desc": "Recorded crash-safe and converted when you stop",
                 "make": self.seg("container", [("mp4", "MP4"), ("mkv", "MKV")])},
            ]),
        ]

    def page_audio(self):
        a, c = self.app, self.app.cfg
        ok = soundcard_ok()
        spk = [("Default", "Default output")] + [(s, s) for s in a.speakers]
        mics = [(m, m) for m in a.mics] or [("", "No microphone found")]
        return [
            ("System audio", [
                {"label": "Record system audio", "desc": "Everything you hear: apps, videos, games" if ok else
                 "Needs the soundcard package", "make": self.tg("sys_audio")},
                {"label": "Output device", "make": self.dd("speaker", spk, 260)} if c["sys_audio"] else None,
                {"label": "Volume", "make": lambda p: Slider(p, self.S, c["sys_vol"], lambda v: self.set("sys_vol", v))}
                if c["sys_audio"] else None,
            ]),
            ("Microphone", [
                {"label": "Record microphone", "make": self.tg("mic")},
                {"label": "Device", "make": self.dd("mic_device", mics, 260)} if c["mic"] else None,
                {"label": "Volume", "make": lambda p: Slider(p, self.S, c["mic_vol"], lambda v: self.set("mic_vol", v))}
                if c["mic"] else None,
                {"label": "Noise reduction", "make": self.tg("mic_denoise")} if c["mic"] else None,
            ]),
            ("Tracks", [
                {"label": "Separate audio tracks", "desc": "Mixed + system + mic on their own tracks, for editing",
                 "make": self.tg("separate_tracks")},
            ]),
        ]

    def page_general(self):
        c = self.app.cfg

        def set_dir(v):
            if not v:
                return False
            self.set("out_dir", os.path.normpath(v))

        def browse():
            d = filedialog.askdirectory(parent=self.top, initialdir=c["out_dir"])
            if d:
                self.set("out_dir", os.path.normpath(d))
                self.render_page()

        return [
            ("Recordings", [
                {"label": "Save to", "make": self.composite(
                    lambda p: Field(p, self.S, c["out_dir"], set_dir, 250),
                    lambda p: Btn(p, self.S, "Browse", browse, glyph=""))},
                {"label": "Open folder", "make": lambda p: Btn(p, self.S, "Open", self.app.open_folder, glyph="")},
                {"label": "Show file when saved", "make": self.tg("open_after")},
            ]),
            ("Recording", [
                {"label": "Countdown", "make": self.seg("countdown", [(0, "Off"), (3, "3 s"), (5, "5 s"), (10, "10 s")])},
                {"label": "Stop automatically after", "desc": "Minutes · 0 = never",
                 "make": self.num_field("auto_stop", 0, 1440, 90, as_float=True)},
                {"label": "Border around recorded area", "make": self.tg("border")},
                {"label": "Hide SmoothRecord from recordings", "desc": "Bar, countdown and border never appear in videos",
                 "make": self.tg("exclude_ui")},
            ]),
        ]

    def page_hotkeys(self):
        c = self.app.cfg

        def hk(key):
            def commit(v):
                v = v.lower().replace(" ", "")
                if v and not parse_hotkey(v):
                    return False
                self.set(key, v)
            return lambda p: HotkeyField(p, self.S, c[key], commit, self.app)

        return [
            ("Global shortcuts", [
                {"label": "Start / stop recording", "make": hk("hk_record")},
                {"label": "Pause / resume", "make": hk("hk_pause")},
                {"label": "Screenshot", "make": hk("hk_shot")},
                {"label": "Show / hide the bar", "make": hk("hk_toggle")},
                {"desc": "Click a box, then press the keys you want. Backspace clears it, Esc cancels.", "h": 46},
            ]),
        ]

    # ---- hooks used by the app
    def refresh_devices(self):
        if self.page in ("encoding", "audio"):
            self.render_page()

    def refresh_capture(self):
        if self.page == "video":
            self.render_page()

    def _wait_pick(self):
        if self.app.root.state() == "withdrawn":
            self.top.after(200, self._wait_pick)
        else:
            self.top.deiconify()
            self.render_page()

    def close(self):
        self.app.settings_win = None
        self.top.destroy()


# ---- headless self-test: pythonw SmoothRecord.pyw --selftest [seconds] ----------------
def selftest(secs=4):
    cfg = load_cfg()
    ff = find_ffmpeg(cfg)
    print("ffmpeg:", ff)
    avail = detect_encoders(ff)
    print("encoders:", avail)
    mons = get_monitors()
    print("monitors:", mons)
    cfg.update(out_dir=os.path.join(CFG_DIR, "selftest"), fps=int(os.environ.get("QR_FPS", 120)),
               out_res=os.environ.get("QR_RES", "1920x1080 (1080p)"), container="mp4",
               mic=bool(os.environ.get("QR_MIC")), mic_device=(list_mics(ff) or [""])[0], last_plan=None,
               codec=os.environ.get("QR_CODEC", "h264"), encoder=os.environ.get("QR_ENC", "auto"),
               separate_tracks=bool(os.environ.get("QR_TRACKS")), chroma444=bool(os.environ.get("QR_444")),
               sys_audio=not os.environ.get("QR_NOAUDIO"))
    cfg.update(json.loads(os.environ.get("QR_CFG") or "{}"))
    secs = float(os.environ.get("QR_SECS", secs))
    m = mons[0]
    if os.environ.get("QR_REGION"):
        geo = (0, 100, 100, 1280, 720, False, m)
        rect = (m["x"] + 100, m["y"] + 100, 1280, 720)
    else:
        geo = (0, 0, 0, m["w"], m["h"], True, m)
        rect = None
    os.makedirs(cfg["out_dir"], exist_ok=True)
    root = tk.Tk()
    root.withdraw()
    hb = Heartbeat(root, m, rect)
    out = {}

    def work():
        r = Recorder(ff)
        err = r.begin(cfg, geo, make_plans(cfg, avail))
        print("start:", err or "ok", r.current.plan if r.current else None, flush=True)
        if not err:
            time.sleep(secs)
            if os.environ.get("QR_PAUSE"):
                r.stop_segment()
                time.sleep(1)
                print("resume:", r.new_segment() or "ok", flush=True)
                time.sleep(secs)
            r.stop_segment()
            out["final"] = r.finalize()
            print("final:", out["final"], flush=True)
        root.after(0, root.destroy)

    threading.Thread(target=work, daemon=True).start()
    root.mainloop()


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        App().run()
