#!/usr/bin/env python3
"""
ColourFix - system-wide colour blindness correction for Windows, macOS and Linux.

It applies a 3x3 colour matrix in the display pipeline, so every window on
every monitor is corrected, not just one app.

    Windows : Magnification API full-screen colour effect
    macOS   : generated ICC display profile, set per display through ColorSync
    Linux   : X11  -> per-output CTM property (kernel/GPU colour matrix)
              KDE Plasma 6 Wayland -> KWin's colour blindness correction effect

Run with no arguments for the app window, or from a terminal:

    python colourfix.py --type deutan --strength 0.8
    python colourfix.py --type protan --simulate      (show others what you see)
    python colourfix.py --reset
    python colourfix.py --selftest

No third-party packages needed. Python 3.8+.
"""

import argparse
import atexit
import ctypes
import hashlib
import json
import os
import platform
import shutil
import struct
import subprocess
import sys
import time

APP = "ColourFix"

# --------------------------------------------------------------------------
# Colour maths
# --------------------------------------------------------------------------

IDENTITY = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]

# Machado, Oliveira & Fernandes (2009) simulation matrices, severity 1.0.
SIMULATE = {
    "protan": [[0.152286, 1.052583, -0.204868],
               [0.114503, 0.786281, 0.099216],
               [-0.003882, -0.048116, 1.051998]],
    "deutan": [[0.367322, 0.860646, -0.227968],
               [0.280085, 0.672501, 0.047413],
               [-0.011820, 0.042940, 0.968881]],
    "tritan": [[1.255528, -0.076749, -0.178779],
               [-0.078411, 0.930809, 0.147602],
               [0.004733, 0.691367, 0.303900]],
}

# Daltonisation: where the lost colour information is moved to.
ERROR_SHIFT = {
    "protan": [[0.0, 0.0, 0.0], [0.7, 1.0, 0.0], [0.7, 0.0, 1.0]],
    "deutan": [[0.0, 0.0, 0.0], [0.7, 1.0, 0.0], [0.7, 0.0, 1.0]],
    "tritan": [[1.0, 0.0, 0.7], [0.0, 1.0, 0.7], [0.0, 0.0, 0.0]],
}

LABELS = {
    "protan": "Protan (red-weak)",
    "deutan": "Deutan (green-weak)",
    "tritan": "Tritan (blue-weak)",
}


def mat_mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)]
            for i in range(3)]


def mat_sub(a, b):
    return [[a[i][j] - b[i][j] for j in range(3)] for i in range(3)]


def mat_lerp(a, b, t):
    return [[a[i][j] + (b[i][j] - a[i][j]) * t for j in range(3)]
            for i in range(3)]


def mat_inv(m):
    (a, b, c), (d, e, f), (g, h, i) = m
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(det) < 1e-9:
        raise ValueError("colour matrix is not invertible")
    return [[(e * i - f * h) / det, (c * h - b * i) / det, (b * f - c * e) / det],
            [(f * g - d * i) / det, (a * i - c * g) / det, (c * d - a * f) / det],
            [(d * h - e * g) / det, (b * g - a * h) / det, (a * e - b * d) / det]]


def build_matrix(kind, strength=1.0, simulate=False):
    """Matrix M so that displayed_rgb = M * original_rgb (column vectors)."""
    strength = max(0.0, min(1.0, float(strength)))
    sim = SIMULATE[kind]
    if simulate:
        # Capped just short of full so the matrix stays invertible (macOS).
        return mat_lerp(IDENTITY, sim, min(strength, 0.98))
    lost = mat_sub(IDENTITY, sim)                 # what the viewer cannot see
    shift = mat_mul(ERROR_SHIFT[kind], lost)      # moved to channels they can
    return [[IDENTITY[i][j] + strength * shift[i][j] for j in range(3)]
            for i in range(3)]


# --------------------------------------------------------------------------
# Windows
# --------------------------------------------------------------------------

class WindowsBackend:
    name = "Windows Magnification API (all monitors)"
    notes = "Keep this window open: Windows removes the effect when it closes."
    supports_simulate = True

    def __init__(self):
        self.mag = ctypes.WinDLL("Magnification.dll")
        self.mag.MagInitialize.restype = ctypes.c_bool
        self.mag.MagSetFullscreenColorEffect.restype = ctypes.c_bool
        if not self.mag.MagInitialize():
            raise RuntimeError("could not start the Windows Magnification API")

    def _set(self, m):
        effect = ((ctypes.c_float * 5) * 5)()
        for r in range(5):
            effect[r][r] = 1.0
        for r in range(3):
            for c in range(3):
                effect[c][r] = m[r][c]   # the API uses row vectors: transpose
        if not self.mag.MagSetFullscreenColorEffect(ctypes.byref(effect)):
            raise RuntimeError("Windows rejected the colour effect")

    def apply(self, m, kind=None, strength=1.0, simulate=False):
        self._set(m)

    def reset(self):
        self._set(IDENTITY)


# --------------------------------------------------------------------------
# macOS
# --------------------------------------------------------------------------

SRGB_TO_XYZ_D50 = [[0.4360747, 0.3850649, 0.1430804],
                   [0.2225045, 0.7168786, 0.0606169],
                   [0.0139322, 0.0971045, 0.7141733]]


def _s15f16(v):
    return struct.pack(">i", int(round(v * 65536.0)))


def _xyz_tag(x, y, z):
    return b"XYZ " + b"\0" * 4 + _s15f16(x) + _s15f16(y) + _s15f16(z)


def _desc_tag(text):
    data = text.encode("ascii") + b"\0"
    return (b"desc" + b"\0" * 4 + struct.pack(">I", len(data)) + data
            + b"\0" * 4 + b"\0" * 4 + b"\0" * 2 + b"\0" + b"\0" * 67)


def _text_tag(text):
    return b"text" + b"\0" * 4 + text.encode("ascii") + b"\0"


def _srgb_curve_tag(points=1024):
    out = [b"curv", b"\0" * 4, struct.pack(">I", points)]
    for n in range(points):
        v = n / (points - 1)
        lin = v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
        out.append(struct.pack(">H", int(round(lin * 65535))))
    return b"".join(out)


def build_icc(m, description=APP):
    """ICC v2 display profile that makes a colour-managed OS show M * sRGB."""
    primaries = mat_mul(SRGB_TO_XYZ_D50, mat_inv(m))
    col = lambda j: (primaries[0][j], primaries[1][j], primaries[2][j])
    curve = _srgb_curve_tag()
    tags = [
        (b"desc", _desc_tag(description)),
        (b"cprt", _text_tag("Public domain")),
        (b"wtpt", _xyz_tag(0.9505, 1.0, 1.0891)),
        (b"rXYZ", _xyz_tag(*col(0))),
        (b"gXYZ", _xyz_tag(*col(1))),
        (b"bXYZ", _xyz_tag(*col(2))),
        (b"rTRC", curve), (b"gTRC", curve), (b"bTRC", curve),
    ]
    offset = 128 + 4 + 12 * len(tags)
    table, body, seen = [], b"", {}
    for sig, data in tags:
        if data in seen:                       # share identical tag data
            table.append((sig,) + seen[data])
            continue
        padded = data + b"\0" * (-len(data) % 4)
        seen[data] = (offset + len(body), len(data))
        table.append((sig,) + seen[data])
        body += padded
    tag_table = struct.pack(">I", len(tags)) + b"".join(
        sig + struct.pack(">II", off, size) for sig, off, size in table)
    t = time.gmtime()
    header = (struct.pack(">I", 128 + len(tag_table) + len(body))
              + b"\0" * 4 + struct.pack(">I", 0x02100000)
              + b"mntr" + b"RGB " + b"XYZ "
              + struct.pack(">6H", t.tm_year, t.tm_mon, t.tm_mday,
                            t.tm_hour, t.tm_min, t.tm_sec)
              + b"acsp" + b"APPL" + b"\0" * 4 + b"\0" * 4 + b"\0" * 4
              + b"\0" * 8 + struct.pack(">I", 0)
              + _s15f16(0.9642) + _s15f16(1.0) + _s15f16(0.8249)
              + b"\0" * 4)
    header += b"\0" * (128 - len(header))
    return header + tag_table + body


class MacBackend:
    name = "macOS ColorSync display profile (all displays)"
    notes = ("Replaces each display's colour profile while active; Reset "
             "restores the factory profile.")
    supports_simulate = True

    def __init__(self):
        self.folder = os.path.expanduser("~/Library/ColorSync/Profiles")
        os.makedirs(self.folder, exist_ok=True)
        self.cf = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.app = ctypes.CDLL(
            "/System/Library/Frameworks/ApplicationServices.framework/"
            "ApplicationServices")
        vp = ctypes.c_void_p
        self.cf.CFURLCreateFromFileSystemRepresentation.restype = vp
        self.cf.CFURLCreateFromFileSystemRepresentation.argtypes = [
            vp, ctypes.c_char_p, ctypes.c_long, ctypes.c_bool]
        self.cf.CFDictionaryCreateMutable.restype = vp
        self.cf.CFDictionaryCreateMutable.argtypes = [vp, ctypes.c_long, vp, vp]
        self.cf.CFDictionarySetValue.argtypes = [vp, vp, vp]
        self.cf.CFRelease.argtypes = [vp]
        self.app.CGGetActiveDisplayList.argtypes = [
            ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32)]
        self.app.CGDisplayCreateUUIDFromDisplayID.restype = vp
        self.app.CGDisplayCreateUUIDFromDisplayID.argtypes = [ctypes.c_uint32]
        self.app.ColorSyncDeviceSetCustomProfiles.restype = ctypes.c_bool
        self.app.ColorSyncDeviceSetCustomProfiles.argtypes = [vp, vp, vp]

    def _const(self, lib, name):
        return ctypes.c_void_p.in_dll(lib, name)

    def _displays(self):
        ids = (ctypes.c_uint32 * 16)()
        count = ctypes.c_uint32(0)
        self.app.CGGetActiveDisplayList(16, ids, ctypes.byref(count))
        return [ids[n] for n in range(count.value)]

    def _set_profile(self, value):
        """value is a CFURL for a profile, or kCFNull for the factory one."""
        key_cb = ctypes.addressof(
            ctypes.c_char.in_dll(self.cf, "kCFTypeDictionaryKeyCallBacks"))
        val_cb = ctypes.addressof(
            ctypes.c_char.in_dll(self.cf, "kCFTypeDictionaryValueCallBacks"))
        info = self.cf.CFDictionaryCreateMutable(None, 0, key_cb, val_cb)
        self.cf.CFDictionarySetValue(
            info, self._const(self.app, "kColorSyncDeviceDefaultProfileID"),
            value)
        self.cf.CFDictionarySetValue(
            info, self._const(self.app, "kColorSyncProfileUserScope"),
            self._const(self.cf, "kCFPreferencesCurrentUser"))
        ok = True
        for display in self._displays():
            uuid = self.app.CGDisplayCreateUUIDFromDisplayID(display)
            if not uuid:
                continue
            ok &= bool(self.app.ColorSyncDeviceSetCustomProfiles(
                self._const(self.app, "kColorSyncDisplayDeviceClass"),
                uuid, info))
            self.cf.CFRelease(uuid)
        self.cf.CFRelease(info)
        if not ok:
            raise RuntimeError("ColorSync refused the profile on a display")

    def _clean(self, keep=None):
        for f in os.listdir(self.folder):
            if f.startswith(APP + "-") and f.endswith(".icc") and f != keep:
                try:
                    os.remove(os.path.join(self.folder, f))
                except OSError:
                    pass

    def apply(self, m, kind=None, strength=1.0, simulate=False):
        data = build_icc(m, APP)
        # A fresh file name each time, as macOS caches profiles by path.
        name = "%s-%s.icc" % (APP, hashlib.sha1(data).hexdigest()[:10])
        path = os.path.join(self.folder, name)
        with open(path, "wb") as f:
            f.write(data)
        raw = path.encode("utf-8")
        url = self.cf.CFURLCreateFromFileSystemRepresentation(
            None, raw, len(raw), False)
        try:
            self._set_profile(url)
        finally:
            self.cf.CFRelease(url)
        self._clean(keep=name)

    def reset(self):
        self._set_profile(self._const(self.cf, "kCFNull"))
        self._clean()


# --------------------------------------------------------------------------
# Linux
# --------------------------------------------------------------------------

def _run(cmd):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True)


def ctm_words(m):
    """3x3 matrix -> the 18 32-bit words xrandr wants for the CTM property."""
    words = []
    for row in m:
        for v in row:
            mag = int(round(abs(v) * (1 << 32)))
            if v < 0:
                mag |= 1 << 63                 # sign-magnitude S31.32
            words += [mag & 0xFFFFFFFF, mag >> 32]
    return ",".join(str(w) for w in words)


class X11Backend:
    notes = "Stays active after closing; use Reset (or --reset) to undo."
    supports_simulate = True

    def __init__(self):
        if not shutil.which("xrandr"):
            raise RuntimeError("xrandr is not installed")
        self.outputs = self._ctm_outputs()
        if not self.outputs:
            raise RuntimeError(
                "your graphics driver does not expose a colour matrix (CTM) "
                "on X11. It needs the 'modesetting' or 'amdgpu' driver.")
        self.name = "X11 colour matrix on " + ", ".join(self.outputs)

    @staticmethod
    def _ctm_outputs():
        outputs, current = [], None
        for line in _run(["xrandr", "--prop"]).stdout.splitlines():
            if line and not line[0].isspace():
                parts = line.split()
                current = parts[0] if len(parts) > 1 and parts[1] == "connected" else None
            elif current and line.strip().startswith("CTM:"):
                outputs.append(current)
        return outputs

    def _set(self, m):
        words = ctm_words(m)
        for out in self.outputs:
            r = _run(["xrandr", "--output", out, "--set", "CTM", words])
            if r.returncode != 0:
                raise RuntimeError("xrandr failed on %s: %s" % (out, r.stderr.strip()))

    def apply(self, m, kind=None, strength=1.0, simulate=False):
        self._set(m)

    def reset(self):
        self._set(IDENTITY)


class KWinBackend:
    """KDE Plasma 6 on Wayland: drives KWin's own correction effect."""
    name = "KDE Plasma (KWin colour blindness correction effect)"
    notes = ("Uses KDE's built-in filter, so the matrix is KDE's own. "
             "Simulation mode is not available here.")
    supports_simulate = False
    EFFECT = "colorblindnesscorrection"
    MODES = {"protan": "0", "deutan": "1", "tritan": "2"}

    def __init__(self):
        self.write = shutil.which("kwriteconfig6")
        self.qdbus = shutil.which("qdbus6") or shutil.which("qdbus")
        if not (self.write and self.qdbus):
            raise RuntimeError("kwriteconfig6 / qdbus6 not found (Plasma 6 needed)")

    def _cfg(self, group, key, value):
        _run([self.write, "--file", "kwinrc", "--group", group, "--key", key, value])

    def _effects(self, method):
        return _run([self.qdbus, "org.kde.KWin", "/Effects",
                     "org.kde.kwin.Effects." + method, self.EFFECT])

    def apply(self, m, kind=None, strength=1.0, simulate=False):
        if simulate:
            raise RuntimeError("simulation mode is not supported on KDE Wayland")
        group = "Effect-" + self.EFFECT
        self._cfg(group, "Mode", self.MODES[kind])
        self._cfg(group, "Intensity", "%.2f" % strength)
        self._cfg("Plugins", self.EFFECT + "Enabled", "true")
        self._effects("loadEffect")
        self._effects("reconfigureEffect")

    def reset(self):
        self._cfg("Plugins", self.EFFECT + "Enabled", "false")
        self._effects("unloadEffect")


def linux_backend():
    wayland = (os.environ.get("XDG_SESSION_TYPE") == "wayland"
               or bool(os.environ.get("WAYLAND_DISPLAY")))
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    if wayland:
        if "kde" in desktop:
            return KWinBackend()
        raise RuntimeError(
            "this Wayland desktop (%s) does not let outside programs recolour "
            "the screen. Log in to an X11 session, or use KDE Plasma 6."
            % (desktop or "unknown"))
    return X11Backend()


def get_backend():
    system = platform.system()
    if system == "Windows":
        return WindowsBackend()
    if system == "Darwin":
        return MacBackend()
    if system == "Linux":
        return linux_backend()
    raise RuntimeError("unsupported operating system: " + system)


# --------------------------------------------------------------------------
# Settings and start-at-login
# --------------------------------------------------------------------------

DEFAULTS = {"kind": "deutan", "mode": "correct", "strength": 0.8,
            "auto_on": False}


def config_dir():
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif system == "Darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP)


def load_settings():
    settings = dict(DEFAULTS)
    try:
        with open(os.path.join(config_dir(), "settings.json")) as f:
            saved = json.load(f)
        for key, default in DEFAULTS.items():
            if isinstance(saved.get(key), type(default)):
                settings[key] = saved[key]
    except (OSError, ValueError):
        pass
    if settings["kind"] not in SIMULATE:
        settings["kind"] = DEFAULTS["kind"]
    if settings["mode"] not in ("correct", "simulate"):
        settings["mode"] = DEFAULTS["mode"]
    settings["strength"] = max(0.0, min(1.0, settings["strength"]))
    return settings


def save_settings(settings):
    try:
        os.makedirs(config_dir(), exist_ok=True)
        with open(os.path.join(config_dir(), "settings.json"), "w") as f:
            json.dump(settings, f, indent=2)
    except OSError:
        pass


def launch_command():
    if getattr(sys, "frozen", False):                  # packaged app
        return [sys.executable, "--minimised"]
    return [sys.executable, os.path.abspath(__file__), "--minimised"]


def _autostart_file():
    if platform.system() == "Darwin":
        return os.path.expanduser("~/Library/LaunchAgents/com.colourfix.app.plist")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "autostart", "colourfix.desktop")


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_enabled():
    if platform.system() == "Windows":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                winreg.QueryValueEx(key, APP)
            return True
        except OSError:
            return False
    return os.path.exists(_autostart_file())


def set_autostart(on):
    cmd = launch_command()
    if platform.system() == "Windows":
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            if on:
                winreg.SetValueEx(key, APP, 0, winreg.REG_SZ,
                                  " ".join('"%s"' % c for c in cmd))
            else:
                try:
                    winreg.DeleteValue(key, APP)
                except OSError:
                    pass
        return
    path = _autostart_file()
    if not on:
        if os.path.exists(path):
            os.remove(path)
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if platform.system() == "Darwin":
        import plistlib
        with open(path, "wb") as f:
            plistlib.dump({"Label": "com.colourfix.app",
                           "ProgramArguments": cmd, "RunAtLoad": True}, f)
    else:
        with open(path, "w") as f:
            f.write("[Desktop Entry]\nType=Application\nName=%s\nExec=%s\n"
                    "X-GNOME-Autostart-enabled=true\n"
                    % (APP, " ".join('"%s"' % c for c in cmd)))


# --------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------

BG, PANEL, INK = "#eef1f6", "#ffffff", "#18203a"
MUTED, LINE, ACCENT, HILITE = "#5d667d", "#d5dae5", "#2350e0", "#ffc533"
OFF_TRACK, SOFT = "#c3cad8", "#c5cbe0"

SWATCHES = ["#d6362b", "#3f9b3a", "#e98a1f", "#9c9a2a", "#7a4a21",
            "#2f6b3c", "#e37fa5", "#8f97a3", "#7b3fb0", "#2f5fd0"]

CARDS = [("protan", "Protan", "Red-weak"),
         ("deutan", "Deutan", "Green-weak"),
         ("tritan", "Tritan", "Blue-weak")]


def preview_hex(colour, m):
    """What a swatch looks like once the matrix has been applied."""
    lin = []
    for n in (1, 3, 5):
        v = int(colour[n:n + 2], 16) / 255.0
        lin.append(v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4)
    out = "#"
    for row in m:
        v = max(0.0, min(1.0, sum(row[k] * lin[k] for k in range(3))))
        v = 12.92 * v if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055
        out += "%02x" % int(round(v * 255))
    return out


def start_windows_hotkey(flag):
    """Ctrl+Alt+C from anywhere. Sets flag['hit']; the window polls it."""
    import threading
    from ctypes import wintypes

    def listen():
        user32 = ctypes.windll.user32
        if not user32.RegisterHotKey(None, 1, 0x0001 | 0x0002, 0x43):
            return
        flag["ready"] = True
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312:                  # WM_HOTKEY
                flag["hit"] = True

    threading.Thread(target=listen, daemon=True).start()


class AppWindow:
    W, H, PAD = 480, 618, 24

    def __init__(self, backend, backend_error=None, minimised=False):
        import tkinter as tk
        self.tk = tk
        self.backend, self.backend_error = backend, backend_error
        self.settings = load_settings()
        self.enabled = False
        self.error = None
        self.hits, self.job, self.dragging = [], None, False
        self.hotkey = {"hit": False, "ready": False}
        try:
            self.autostart = autostart_enabled()
        except Exception:
            self.autostart = False
        if backend and not backend.supports_simulate:
            self.settings["mode"] = "correct"

        self.root = tk.Tk()
        self.root.title(APP)
        self.root.resizable(False, False)
        self.root.configure(bg=BG)
        family = {"Windows": "Segoe UI", "Darwin": "Helvetica Neue"}.get(
            platform.system(), "DejaVu Sans")
        self.font = lambda size, bold=False: (family, -size, "bold" if bold else "normal")
        self.cv = tk.Canvas(self.root, width=self.W, height=self.H, bg=BG,
                            highlightthickness=0)
        self.cv.pack()
        self.cv.bind("<Button-1>", self.on_press)
        self.cv.bind("<B1-Motion>", self.on_drag)
        self.cv.bind("<ButtonRelease-1>", self.on_release)
        self.root.bind("<space>", lambda e: self.toggle())
        for n, card in enumerate(CARDS):
            self.root.bind(str(n + 1), lambda e, k=card[0]: self.choose("kind", k))
        self.root.bind("<Left>", lambda e: self.nudge(-0.05))
        self.root.bind("<Right>", lambda e: self.nudge(0.05))
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        if platform.system() == "Windows" and backend:
            try:
                start_windows_hotkey(self.hotkey)
                self.root.after(150, self.poll_hotkey)
            except Exception:
                pass
        if backend and self.settings["auto_on"]:
            self.enabled = True
            self.apply_now()
        self.draw()
        if minimised:
            self.root.iconify()

    # ---- state -----------------------------------------------------------

    def matrix(self):
        s = self.settings
        return build_matrix(s["kind"], s["strength"], s["mode"] == "simulate")

    def apply_now(self):
        self.job = None
        if not self.backend:
            return
        s = self.settings
        try:
            if self.enabled:
                self.backend.apply(self.matrix(), s["kind"], s["strength"],
                                   s["mode"] == "simulate")
            else:
                self.backend.reset()
            self.error = None
        except Exception as exc:
            self.enabled = False
            self.error = str(exc)
        self.draw()

    def schedule(self, delay=200):
        """Debounced, so dragging the slider does not flood the display."""
        if self.job:
            self.root.after_cancel(self.job)
        self.job = self.root.after(delay, self.apply_now)

    def toggle(self):
        if not self.backend:
            return
        self.enabled = not self.enabled
        self.apply_now()

    def choose(self, key, value):
        if key == "mode" and value == "simulate" and self.backend \
                and not self.backend.supports_simulate:
            return
        self.settings[key] = value
        save_settings(self.settings)
        self.draw()
        if self.enabled:
            self.schedule(0)

    def nudge(self, amount):
        self.set_strength(self.settings["strength"] + amount)
        save_settings(self.settings)

    def set_strength(self, value):
        self.settings["strength"] = round(max(0.0, min(1.0, value)), 2)
        self.draw()
        if self.enabled:
            self.schedule()

    def flip(self, key):
        if key == "autostart":
            try:
                set_autostart(not self.autostart)
                self.autostart = not self.autostart
            except Exception as exc:
                self.error = "Could not change start-up: %s" % exc
        else:
            self.settings[key] = not self.settings[key]
            save_settings(self.settings)
        self.draw()

    def poll_hotkey(self):
        if self.hotkey["hit"]:
            self.hotkey["hit"] = False
            self.toggle()
        self.root.after(150, self.poll_hotkey)

    def close(self):
        try:
            if self.backend:
                self.backend.reset()
        except Exception:
            pass
        self.root.destroy()

    # ---- pointer ---------------------------------------------------------

    def on_press(self, event):
        for x1, y1, x2, y2, action in self.hits:
            if x1 <= event.x <= x2 and y1 <= event.y <= y2:
                if action == "slider":
                    self.dragging = True
                    self.on_drag(event)
                else:
                    action()
                return

    def on_drag(self, event):
        if self.dragging:
            x1, x2 = self.slider
            self.set_strength((event.x - x1) / float(x2 - x1))

    def on_release(self, event):
        if self.dragging:
            self.dragging = False
            save_settings(self.settings)

    # ---- drawing ---------------------------------------------------------

    def rrect(self, x1, y1, x2, y2, r, **kw):
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
               x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return self.cv.create_polygon(pts, smooth=True, **kw)

    def tick(self, x, y, colour, size=1.0):
        self.cv.create_line(x - 5 * size, y, x - 1.5 * size, y + 3.5 * size,
                            x + 5 * size, y - 4 * size, fill=colour,
                            width=2.2 * size, capstyle="round", joinstyle="round")

    def status(self):
        s = self.settings
        if not self.backend:
            return "Preview only on this computer"
        if self.error:
            return "Could not switch on"
        if not self.enabled:
            return "Off. Your screen is showing its normal colours."
        verb = "Simulating" if s["mode"] == "simulate" else "Correcting for"
        return "%s %s at %d%%, on every screen." % (
            verb, LABELS[s["kind"]].lower(), round(s["strength"] * 100))

    def draw(self):
        cv, s, P, W = self.cv, self.settings, self.PAD, self.W
        cv.delete("all")
        self.hits = []
        text = lambda x, y, t, size=13, bold=False, fill=INK, **kw: cv.create_text(
            x, y, text=t, font=self.font(size, bold), fill=fill,
            anchor=kw.pop("anchor", "nw"), **kw)

        # Header and the main switch
        text(P, 18, APP, 24, True)
        text(P, 52, self.status(), 13, fill=MUTED, width=W - 2 * P - 100)
        tx2, ty = W - P, 22
        on, usable = self.enabled, bool(self.backend)
        self.rrect(tx2 - 84, ty, tx2, ty + 36, 18,
                   fill=ACCENT if on else (OFF_TRACK if usable else LINE))
        kx = tx2 - 18 if on else tx2 - 66
        cv.create_oval(kx - 13, ty + 5, kx + 13, ty + 31, fill=PANEL, outline="")
        text(tx2 - 58 if on else tx2 - 26, ty + 18, "On" if on else "Off", 13, True,
             fill=PANEL if on else (INK if usable else MUTED), anchor="center")
        self.hits.append((tx2 - 84, ty, tx2, ty + 36, self.toggle))

        # Live preview
        self.rrect(P, 84, W - P, 214, 14, fill=PANEL)
        simulate = s["mode"] == "simulate"
        m = self.matrix()
        rows = [("Colours as they are", IDENTITY),
                ("As seen with %s vision" % s["kind"] if simulate
                 else "With ColourFix on", m)]
        for r, (label, matrix) in enumerate(rows):
            y = 96 + r * 58
            text(P + 16, y, label, 12, fill=MUTED)
            step = (W - 2 * P - 32 - 36) / 9.0
            for n, colour in enumerate(SWATCHES):
                x = P + 16 + n * step
                self.rrect(x, y + 20, x + 36, y + 48, 6,
                           fill=preview_hex(colour, matrix))

        # Colour vision type
        text(P, 232, "Colour vision type", 13, True)
        cw = (W - 2 * P - 20) / 3.0
        for n, (key, title, sub) in enumerate(CARDS):
            x = P + n * (cw + 10)
            picked = s["kind"] == key
            self.rrect(x, 256, x + cw, 316, 12, fill=INK if picked else PANEL)
            text(x + 14, 268, title, 15, True, fill=PANEL if picked else INK)
            text(x + 14, 290, sub, 12, fill=SOFT if picked else MUTED)
            if picked:
                cv.create_oval(x + cw - 32, 266, x + cw - 12, 286,
                               fill=HILITE, outline="")
                self.tick(x + cw - 22, 276, INK, 0.8)
            self.hits.append((x, 256, x + cw, 316,
                              lambda k=key: self.choose("kind", k)))

        # Mode
        text(P, 334, "What it should do", 13, True)
        self.rrect(P, 358, W - P, 396, 12, fill=PANEL)
        half = (W - 2 * P) / 2.0
        can_sim = not self.backend or self.backend.supports_simulate
        for n, (key, label) in enumerate((("correct", "Correct my colours"),
                                          ("simulate", "Show others what I see"))):
            x = P + n * half
            picked = s["mode"] == key
            if picked:
                self.rrect(x + 4, 362, x + half - 4, 392, 9, fill=INK)
            text(x + half / 2, 377, label, 13, picked, anchor="center",
                 fill=PANEL if picked else (INK if key == "correct" or can_sim
                                            else OFF_TRACK))
            self.hits.append((x, 358, x + half, 396,
                              lambda k=key: self.choose("mode", k)))

        # Strength
        text(P, 414, "Strength", 13, True)
        text(W - P, 414, "%d%%" % round(s["strength"] * 100), 13, True, anchor="ne")
        x1, x2, y = P + 12, W - P - 12, 450
        self.slider = (x1, x2)
        kx = x1 + (x2 - x1) * s["strength"]
        cv.create_line(x1, y, x2, y, fill=LINE, width=6, capstyle="round")
        cv.create_line(x1, y, kx, y, fill=ACCENT, width=6, capstyle="round")
        cv.create_oval(kx - 12, y - 12, kx + 12, y + 12, fill=PANEL,
                       outline=INK, width=2)
        self.hits.append((P, y - 16, W - P, y + 16, "slider"))

        # Options
        options = [("auto_on", s["auto_on"], "Switch on as soon as ColourFix opens"),
                   ("autostart", self.autostart, "Open ColourFix when I log in")]
        for n, (key, value, label) in enumerate(options):
            y = 480 + n * 30
            self.rrect(P, y, P + 20, y + 20, 5, fill=ACCENT if value else PANEL,
                       outline="" if value else OFF_TRACK)
            if value:
                self.tick(P + 10, y + 10, PANEL, 0.8)
            text(P + 30, y + 10, label, 13, anchor="w")
            self.hits.append((P, y - 4, W - P, y + 24, lambda k=key: self.flip(k)))

        # Footer
        cv.create_line(P, 546, W - P, 546, fill=LINE)
        if self.error or self.backend_error:
            note = self.error or self.backend_error
            note = note[0].upper() + note[1:]
        elif self.hotkey["ready"] or platform.system() == "Windows":
            note = ("Ctrl+Alt+C switches it on or off from anywhere. Minimise "
                    "to keep it running; closing this window switches it off.")
        else:
            note = ("Space switches it on or off while this window is in "
                    "front. Closing this window switches it off.")
        text(P, 556, note, 12, fill=INK if (self.error or self.backend_error)
             else MUTED, width=W - 2 * P)

    def run(self):
        self.root.mainloop()


# --------------------------------------------------------------------------
# Self-test and command line
# --------------------------------------------------------------------------

def selftest():
    for kind in SIMULATE:
        for sim in (False, True):
            m = build_matrix(kind, 1.0, sim)
            for row in m:                      # white must stay white
                assert abs(sum(row) - 1.0) < 1e-4, (kind, sim, row)
            check = mat_mul(m, mat_inv(m))
            assert all(abs(check[i][j] - IDENTITY[i][j]) < 1e-6
                       for i in range(3) for j in range(3))
            icc = build_icc(m)
            assert struct.unpack(">I", icc[:4])[0] == len(icc)
            assert icc[36:40] == b"acsp"
    assert ctm_words(IDENTITY) == "0,1,0,0,0,0,0,0,0,1,0,0,0,0,0,0,0,1"
    assert ctm_words([[-0.5, 0, 0]] * 3).split(",")[:2] == ["2147483648", "2147483648"]
    assert build_matrix("deutan", 0.0) == IDENTITY
    print("Self-test passed.")


def main():
    p = argparse.ArgumentParser(description="System-wide colour blindness correction.")
    p.add_argument("--type", choices=sorted(SIMULATE), help="colour vision type")
    p.add_argument("--strength", type=float, default=0.8, help="0.0 to 1.0")
    p.add_argument("--simulate", action="store_true",
                   help="simulate the deficiency instead of correcting it")
    p.add_argument("--reset", action="store_true", help="restore normal colours")
    p.add_argument("--selftest", action="store_true", help="check the maths")
    p.add_argument("--minimised", action="store_true",
                   help="open the window minimised (used at log-in)")
    args = p.parse_args()

    if args.selftest:
        return selftest()

    backend, problem = None, None
    try:
        backend = get_backend()
    except Exception as exc:
        problem = "%s cannot recolour this screen: %s" % (APP, exc)

    if not (args.type or args.reset):
        try:
            import tkinter  # noqa: F401
        except ImportError:
            sys.exit("Tkinter is missing. Install it (e.g. 'sudo apt install "
                     "python3-tk') or use --type from the terminal.")
        return AppWindow(backend, problem, args.minimised).run()

    if not backend:
        sys.exit(problem)
    if args.reset:
        backend.reset()
        return print("Colours restored.")

    m = build_matrix(args.type, args.strength, args.simulate)
    backend.apply(m, args.type, args.strength, args.simulate)
    print("%s is on via %s." % (APP, backend.name))
    if isinstance(backend, WindowsBackend):
        atexit.register(backend.reset)
        print("Press Ctrl+C to turn it off.")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    else:
        print("Run with --reset to turn it off.")


if __name__ == "__main__":
    main()
