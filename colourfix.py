#!/usr/bin/env python3
"""
ColourFix - system-wide colour blindness correction for Windows, macOS and Linux.

It applies a 3x3 colour matrix in the display pipeline, so every window on
every monitor is corrected, not just one app.

    Windows : Magnification API full-screen colour effect
    macOS   : generated ICC display profile, set per display through ColorSync
    Linux   : X11  -> per-output CTM property (kernel/GPU colour matrix)
              KDE Plasma 6 Wayland -> KWin's colour blindness correction effect

Run with no arguments for the window, or from a terminal:

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
# Window
# --------------------------------------------------------------------------

def run_gui(backend):
    import tkinter as tk
    from tkinter import ttk, messagebox

    root = tk.Tk()
    root.title(APP)
    root.resizable(False, False)
    frame = ttk.Frame(root, padding=16)
    frame.grid()

    kind = tk.StringVar(value="deutan")
    mode = tk.StringVar(value="correct")
    strength = tk.DoubleVar(value=0.8)
    enabled = tk.BooleanVar(value=False)
    status = tk.StringVar(value="Off")

    def refresh(*_):
        try:
            if enabled.get():
                sim = mode.get() == "simulate"
                m = build_matrix(kind.get(), strength.get(), sim)
                backend.apply(m, kind.get(), strength.get(), sim)
                status.set("On: %s, %d%%" % (LABELS[kind.get()],
                                             round(strength.get() * 100)))
            else:
                backend.reset()
                status.set("Off")
        except Exception as exc:                       # show, never crash
            enabled.set(False)
            status.set("Off")
            messagebox.showerror(APP, str(exc))

    ttk.Label(frame, text="Colour vision type").grid(sticky="w")
    for key in ("protan", "deutan", "tritan"):
        ttk.Radiobutton(frame, text=LABELS[key], value=key, variable=kind,
                        command=refresh).grid(sticky="w", padx=8)

    ttk.Label(frame, text="Mode").grid(sticky="w", pady=(12, 0))
    ttk.Radiobutton(frame, text="Correct colours", value="correct",
                    variable=mode, command=refresh).grid(sticky="w", padx=8)
    sim_button = ttk.Radiobutton(frame, text="Simulate (show others what I see)",
                                 value="simulate", variable=mode, command=refresh)
    sim_button.grid(sticky="w", padx=8)
    if not backend.supports_simulate:
        sim_button.state(["disabled"])

    ttk.Label(frame, text="Strength").grid(sticky="w", pady=(12, 0))
    scale = ttk.Scale(frame, from_=0.0, to=1.0, variable=strength, length=260)
    scale.grid(sticky="we")
    scale.bind("<ButtonRelease-1>", refresh)   # apply when the slider is let go

    ttk.Checkbutton(frame, text="Enable on every screen", variable=enabled,
                    command=refresh).grid(sticky="w", pady=(14, 0))
    ttk.Label(frame, textvariable=status).grid(sticky="w", pady=(6, 0))
    ttk.Separator(frame).grid(sticky="we", pady=10)
    ttk.Label(frame, text=backend.name, wraplength=260,
              foreground="#666").grid(sticky="w")
    ttk.Label(frame, text=backend.notes, wraplength=260,
              foreground="#666").grid(sticky="w")

    def close():
        try:
            backend.reset()
        except Exception:
            pass
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    root.mainloop()


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
    args = p.parse_args()

    if args.selftest:
        return selftest()

    try:
        backend = get_backend()
    except Exception as exc:
        message = "%s cannot run here: %s" % (APP, exc)
        if not (args.type or args.reset):      # launched as an app: show it
            try:
                import tkinter as tk
                from tkinter import messagebox
                tk.Tk().withdraw()
                messagebox.showerror(APP, message)
            except Exception:
                pass
        sys.exit(message)

    if args.reset:
        backend.reset()
        return print("Colours restored.")

    if not args.type:
        try:
            return run_gui(backend)
        except ImportError:
            sys.exit("Tkinter is missing. Install it (e.g. 'sudo apt install "
                     "python3-tk') or use --type from the terminal.")

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
