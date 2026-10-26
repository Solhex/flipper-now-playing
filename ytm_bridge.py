#!/usr/bin/env python3
"""
ytm_bridge — PC side of the "YT Music Remote" Flipper Zero app.

Reads what's playing from the desktop media session, pushes
title/artist/progress/volume/album-art to the Flipper over Bluetooth LE, and
runs the playback commands the Flipper sends back.

Transport: the Flipper's BLE serial GATT service (the same one the official
mobile app uses). We connect as BLE central and exchange raw bytes.

Media source: on Linux this is MPRIS via playerctl, on Windows it is the
Windows media session (SMTC, the same source the volume flyout shows) read
through WinRT. Firefox appears in both, but each platform has its own quirks,
which is what the per-platform backends below deal with.

Wire format (PC -> Flipper) — [type:1][len:2 LE][payload:len]
    0x01 META: [state:1][pos:2 LE][dur:2 LE][title utf8]0[artist utf8]0
    0x02 ART : [w:1][h:1][1-bit XBM rows, LSB-first per byte, (w+7)/8 per row]
    0x03 VOL : [volume:1]  0..100
Flipper -> PC: one command byte each (CMD_TRANSPORT / CMD_VOLUME below)
    p play/pause   n next   b previous   - seek back   + seek forward
    u volume up    d volume down
"""

import argparse
import asyncio
import concurrent.futures
import io
import json
import os
import shutil
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

from PIL import Image

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    sys.exit("Missing dependency: pip install bleak")

IS_WINDOWS = sys.platform == "win32"

# ---- tunables ----
ART_SIZE = 44          # px, square; the largest cover that still leaves a row
                       # between it and the bar (64 rows: cover 44 + bar 7 + time 8)
SEEK_STEP = 5          # seconds per seek tap (MPRIS players)
SEEK_STEP_WIN = 10     # seconds per seek tap on Windows: YT Music's l/h shortcut
VOLUME_STEP = 0.05     # fraction of full scale per volume key press
POLL_HZ = 20           # metadata pushes per second (override with --hz)
POLL_HZ_MAX = 25
CONNECT_TIMEOUT = 10.0 # seconds; bounds a hop onto a dead link
LOG_HEARTBEAT = 5.0    # seconds between keep-alive log lines
WRITE_CHUNK = 160      # max bytes per BLE write (further limited by the MTU)
TITLE_MAX = 64         # keep a META frame inside the device's bounds
ARTIST_MAX = 48
DUR_CACHE_MAX = 200    # distinct tracks to remember lengths for
DUR_RETRY_S = 20       # seconds before retrying a length lookup that failed
WINDOW_CACHE_S = 2.0   # how long the AUMID -> window map is reused
REFRESH_S = 0.4        # Windows: how often the media-session manager is re-requested
ART_SETTLE_S = 4.0     # Windows: how long a new track's cover is still being published
ART_RETRY_S = 0.4      # Windows: cover re-read interval while a track settles
ART_RECHECK_S = 5.0    # Windows: cover re-read interval once settled, if still missing

FRAME_META, FRAME_ART, FRAME_VOL, FRAME_TOAST = 0x01, 0x02, 0x03, 0x04
TOAST_MAX = 24         # the device shows one short line

# ---------------------------------------------------------------- state files
# Linux keeps its scratch under ~/.cache (what the service and check_time.py
# expect); Windows uses LOCALAPPDATA so nothing lands in a hidden dot-directory.
if IS_WINDOWS:
    STATE_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "ytm-remote"
else:
    STATE_DIR = Path.home() / ".cache" / "ytm-remote"
STATUS_PATH = str(STATE_DIR / "status.json")
ADDRESS_PATH = str(STATE_DIR / "flipper.json")

CMD_MAP_SEEK_BACK = f"{SEEK_STEP}-"
CMD_MAP_SEEK_FWD = f"{SEEK_STEP}+"
CMD_TRANSPORT = {
    "p": ["play-pause"],
    "n": ["next"],
    "b": ["previous"],
    "-": ["position", CMD_MAP_SEEK_BACK],
    "+": ["position", CMD_MAP_SEEK_FWD],
}
CMD_VOLUME = {"u": +1, "d": -1}   # direction for do_volume
CMD_CAPS = "k"                    # device -> PC: "I take clock-only META frames"
CMD_LIKE = "l"                    # device -> PC: hold OK, toggle like


def resolve_ytdlp():
    """Find yt-dlp, preferring the one beside our own interpreter.

    Run as a service this process gets a bare PATH (/usr/local/bin:/usr/bin), so
    shutil.which() alone finds nothing and every track length silently stays
    unknown -- install it into the bridge's own venv and it is always found.
    On Windows a venv keeps it in Scripts\\, so that directory is checked too.
    """
    exe = "yt-dlp.exe" if IS_WINDOWS else "yt-dlp"
    calls = [Path(sys.executable).with_name(exe),
             Path(sys.executable).parent / "Scripts" / exe]
    for candidate in calls:
        if candidate.exists():
            return str(candidate)
    found = shutil.which("yt-dlp")
    if not found:
        print("yt-dlp not found: track lengths will show as --:--")
    return found


def load_known_address():
    """BLE address of the Flipper from the last successful connection."""
    try:
        with open(ADDRESS_PATH) as handle:
            return json.load(handle).get("address")
    except (OSError, ValueError):
        return None


def save_known_address(address):
    try:
        os.makedirs(os.path.dirname(ADDRESS_PATH), exist_ok=True)
        tmp = ADDRESS_PATH + ".tmp"
        with open(tmp, "w") as handle:
            json.dump({"address": address}, handle)
        os.replace(tmp, ADDRESS_PATH)
    except OSError:
        pass


def pct(fraction):
    """0.0..1.0 volume -> whole percent, or None if the player reports none."""
    try:
        return max(0, min(100, int(round(float(fraction) * 100))))
    except (TypeError, ValueError):
        return None


# ================================================================ Linux: MPRIS
class MprisBackend:
    """Linux/BSD media sessions through playerctl (MPRIS over D-Bus)."""

    name = "mpris"
    seek_step = SEEK_STEP

    def __init__(self, player=None):
        self.player = player

    # ---- helpers shared with the old module-level API
    def run_playerctl(self, *args):
        """playerctl for one player. Returns (ok, stdout); the caller decides what
        a failure means (routine during a poll, worth reporting for a button)."""
        try:
            out = subprocess.run(
                ["playerctl", "-p", self.player, *args], capture_output=True, text=True
            )
        except FileNotFoundError:
            return False, ""
        return out.returncode == 0, out.stdout

    def list(self):
        try:
            out = subprocess.run(["playerctl", "-l"], capture_output=True, text=True)
        except FileNotFoundError:
            sys.exit("playerctl not found — install it to read the media session")
        return [p for p in out.stdout.splitlines() if p.strip()]

    async def read(self):
        """Return dict(title, artist, art, playing, pos, dur, url, vol) or None.

        Runs on the event loop on purpose: playerctl answers in a few
        milliseconds and this is the poll itself (see Bridge.run). The playerctl
        calls triggered by device events go through an executor instead.
        """
        fields = (
            "status",
            "position",
            "mpris:length",
            "xesam:title",
            "xesam:artist",
            "mpris:artUrl",
            "xesam:url",
            "volume",      # playerctl exposes Volume through the metadata format too
        )
        fmt = "\t".join("{{%s}}" % field for field in fields)
        ok, stdout = self.run_playerctl("metadata", "--format", fmt)
        if not ok or not stdout.strip():
            return None

        parts = stdout.rstrip("\n").split("\t")
        parts += [""] * (len(fields) - len(parts))
        f = dict(zip(fields, parts))

        def us(value):
            try:
                return max(0, int(value) // 1_000_000)
            except ValueError:
                return 0

        art = f["mpris:artUrl"]
        art_path = None
        if art.startswith("file://") and os.path.exists(art[len("file://"):]):
            art_path = art[len("file://"):]

        return {
            "title": (f["xesam:title"] or "Unknown").strip(),
            "artist": (f["xesam:artist"] or "").strip(),
            "art": art_path,
            "playing": f["status"].strip().lower() == "playing",
            "pos": us(f["position"]),
            "dur": us(f["mpris:length"]),
            "url": (f["xesam:url"] or "").strip(),
            "vol": pct(f["volume"]),
            "app": self.player or "",
        }

    async def command(self, ch):
        args = CMD_TRANSPORT.get(ch)
        if not args:
            return
        ok, _ = self.run_playerctl(*args)
        if ok:
            print(f"  <- command {ch!r} -> playerctl {' '.join(args)}")
        else:
            print(f"  !! command {ch!r} failed: playerctl {' '.join(args)}")

    def volume(self, sign):
        """Nudge the volume by VOLUME_STEP; returns the new level or None."""
        step = f"{VOLUME_STEP:g}{'+' if sign > 0 else '-'}"
        self.run_playerctl("volume", step)
        ok, out = self.run_playerctl("volume")
        return pct(out) if ok else None

    def like(self):
        """Toggle like on the YouTube Music tab. Returns (ok, message).

        The like shortcut belongs to the web app, so it can only be typed at a
        focused window — there is no way to address a browser tab from outside.
        A window whose *title* shows YouTube Music has it as its active tab, so
        that is what gets checked: firing the keystroke blind would type a '+'
        into whatever else happened to have focus.
        """
        vkkey = Path.home() / ".local/share/ytm-remote/bin/vkkey"
        if not vkkey.exists():
            return False, "Like: vkkey missing"

        windows = sway_windows()
        if not windows:
            return False, "Like: cannot check focus"

        previous = windows[0][0] if windows[0][2] else None
        target = next((w for w in windows if "youtube music" in w[1].lower()), None)
        if target is None:
            return False, "Open the YT Music tab"

        moved = False
        try:
            if not target[2]:
                swaymsg(f"[con_id={target[0]}] focus")
                moved = True
                time.sleep(0.35)      # let the focus change land before typing
            subprocess.run([str(vkkey), "-m", "1", "13"], capture_output=True, timeout=5)
        except Exception as e:
            return False, f"Like failed: {e}"
        finally:
            if moved and previous:
                time.sleep(0.15)
                swaymsg(f"[con_id={previous}] focus")   # give focus back
        return True, "Like toggled"


def sway_socket():
    """The running sway IPC socket, if there is one."""
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    found = sorted(Path(runtime).glob("sway-ipc.*.sock"))
    return str(found[0]) if found else None


def swaymsg(*args, timeout=5):
    sock = sway_socket()
    if not sock:
        return ""
    try:
        out = subprocess.run(
            ["swaymsg", *args], capture_output=True, text=True, timeout=timeout,
            env=dict(os.environ, SWAYSOCK=sock))
        return out.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def sway_windows():
    """[(con_id, title, focused)] for every real window, focused one first."""
    tree = swaymsg("-t", "get_tree", "--raw")
    if not tree:
        return []

    found = []

    def walk(node):
        if node.get("app_id") is not None or node.get("window_properties"):
            found.append((node.get("id"), node.get("name") or "", bool(node.get("focused"))))
        for child in node.get("nodes", []) + node.get("floating_nodes", []):
            walk(child)

    try:
        walk(json.loads(tree))
    except ValueError:
        return []
    found.sort(key=lambda w: not w[2])
    return found


def track_start_age(art_path, max_age=6 * 3600):
    """Seconds since Firefox wrote this track's artwork.

    Firefox reports no playback position for YouTube Music, but it writes a
    fresh art file when a track starts, so that file's mtime dates the track —
    which is what lets the bridge join a song already in progress and still show
    the true elapsed time. Returns None when the age is implausible.
    """
    if not art_path or not isinstance(art_path, str):
        return None
    try:
        age = time.time() - os.path.getmtime(art_path)
    except OSError:
        return None
    return age if 0 <= age < max_age else None


# ============================================================== Windows: SMTC
if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    WM_APPCOMMAND = 0x0319
    APPCOMMAND_MEDIA_NEXTTRACK = 11
    APPCOMMAND_MEDIA_PREVIOUSTRACK = 12
    APPCOMMAND_MEDIA_PLAY_PAUSE = 14

    VK_SHIFT, VK_OEM_PLUS = 0x10, 0xBB
    VK_H, VK_L = 0x48, 0x4C           # YT Music: -10 s / +10 s
    SW_RESTORE = 9

    ULONG_PTR = ctypes.c_ulonglong
    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_EXTENDEDKEY = 0x0001

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ULONG_PTR)]

    class _HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                    ("wParamH", wintypes.WORD)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]

    class _INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    _ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    # COM must be initialised before the shell property store is asked anything,
    # and it has to be the multithreaded apartment: WinRT's async completions
    # arrive on an MTA thread, and an STA caller would wait forever on them
    # without a message pump (which asyncio does not run).
    try:
        ctypes.windll.ole32.CoInitializeEx(None, 0)      # COINIT_MULTITHREADED
    except Exception:
        pass

    class WinWindow:
        """A visible top-level window plus the AUMID the shell groups it under."""

        __slots__ = ("hwnd", "pid", "process", "title", "aumid")

        def __init__(self, hwnd, pid, process, title, aumid):
            self.hwnd, self.pid, self.process = hwnd, pid, process
            self.title, self.aumid = title, aumid

        def __repr__(self):
            return f"<{self.process} {hex(self.hwnd)} {self.title[:24]!r}>"

    class _GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                    ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

        @classmethod
        def parse(cls, text):
            import uuid
            value = uuid.UUID(text)
            return cls(value.time_low, value.time_mid, value.time_hi_version,
                       (ctypes.c_ubyte * 8)(*value.bytes[8:]))

    class _PROPERTYKEY(ctypes.Structure):
        _fields_ = [("fmtid", _GUID), ("pid", ctypes.c_uint)]

    class _PROPVARIANTUNION(ctypes.Union):
        _fields_ = [("pwszVal", ctypes.c_wchar_p), ("ulVal", ctypes.c_ulong),
                    ("ullVal", ctypes.c_ulonglong), ("pointer", ctypes.c_void_p)]

    class _PROPVARIANT(ctypes.Structure):
        _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort),
                    ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                    ("u", _PROPVARIANTUNION)]

    _PKEY_AUMID = _PROPERTYKEY(_GUID.parse("{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}"), 5)
    _IID_IPROPERTYSTORE = _GUID.parse("{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}")
    _VT_LPWSTR = 31

    def _aumid_of_window(hwnd):
        """The window's AppUserModelID, read through the shell property store.

        This is what ties an SMTC session (which only knows an AUMID) back to a
        process: Firefox's session id is a hash of its install path, so the name
        has to come from the window the shell groups it under.

        Called through the raw vtable rather than comtypes on purpose: importing
        comtypes calls CoInitializeEx(STA) on this thread, which leaves the
        WinRT awaits used by the media-session manager waiting for a message pump
        that an asyncio loop never runs -- a hang, not an error.
        """
        shell32 = ctypes.WinDLL("shell32")
        shell32.SHGetPropertyStoreForWindow.argtypes = [
            wintypes.HWND, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
        shell32.SHGetPropertyStoreForWindow.restype = ctypes.c_long
        store = ctypes.c_void_p()
        if shell32.SHGetPropertyStoreForWindow(hwnd, ctypes.byref(_IID_IPROPERTYSTORE),
                                               ctypes.byref(store)) != 0 or not store:
            return None
        try:
            vtable = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            get_value = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p,
                                           ctypes.POINTER(_PROPERTYKEY),
                                           ctypes.POINTER(_PROPVARIANT))(vtable[5])
            value = _PROPVARIANT()
            if get_value(store, ctypes.byref(_PKEY_AUMID), ctypes.byref(value)) != 0:
                return None
            if value.vt != _VT_LPWSTR or not value.u.pwszVal:
                return None
            text = value.u.pwszVal
            ctypes.windll.ole32.PropVariantClear(ctypes.byref(value))
            return text
        finally:
            release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(
                ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents[2])
            release(store)

    def list_windows():
        """Visible, titled top-level windows with process names and AUMIDs."""
        import psutil

        rows = []

        def cb(hwnd, _):
            length = _user32.GetWindowTextLengthW(hwnd)
            if length and _user32.IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(length + 1)
                _user32.GetWindowTextW(hwnd, buf, length + 1)
                pid = wintypes.DWORD()
                _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                try:
                    process = psutil.Process(pid.value).name()
                except Exception:
                    process = ""
                rows.append(WinWindow(hwnd, pid.value, process or "", buf.value,
                                      _aumid_of_window(hwnd)))
            return True

        _user32.EnumWindows(_ENUMPROC(cb), 0)
        return rows

    def _send_key(vk, up=False, extended=False):
        event = _INPUT(type=INPUT_KEYBOARD)
        event.ki = _KEYBDINPUT(wVk=vk, wScan=0,
                               dwFlags=(KEYEVENTF_KEYUP if up else 0)
                               | (KEYEVENTF_EXTENDEDKEY if extended else 0),
                               time=0, dwExtraInfo=0)
        return _user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(_INPUT))

    def tap_key(vk):
        _send_key(vk)
        _send_key(vk, up=True)

    def chord(modifier, vk):
        _send_key(modifier)
        tap_key(vk)
        _send_key(modifier, up=True)

    def set_foreground(hwnd):
        """Bring hwnd forward, working around the foreground lock.

        SetForegroundWindow alone is refused when the caller is not the
        foreground process; attaching to the current foreground thread's input
        queue is the documented way around it.
        """
        foreground = _user32.GetForegroundWindow()
        current = _kernel32.GetCurrentThreadId()
        threads = {_user32.GetWindowThreadProcessId(foreground, None) if foreground else 0,
                   current}
        attached = [t for t in threads if t and _user32.AttachThreadInput(current, t, True)]
        try:
            if _user32.IsIconic(hwnd):
                _user32.ShowWindow(hwnd, SW_RESTORE)
            _user32.BringWindowToTop(hwnd)
            _user32.SetForegroundWindow(hwnd)
            return _user32.GetForegroundWindow() == hwnd
        finally:
            for thread in attached:
                _user32.AttachThreadInput(current, thread, False)

    def _post_appcommand(hwnd, appcommand):
        return bool(_user32.PostMessageW(hwnd, WM_APPCOMMAND, 0, appcommand << 16))

    def media_keys_window():
        """Firefox's hidden "Firefox Media Keys" window, if it has one.

        Firefox keeps a message-only window for media-key handling and routes
        WM_APPCOMMAND from there. Posting at the visible browser window works
        only while that tab happens to own the media element; the hidden window
        is the sink Firefox itself listens on, so it is the reliable target.
        """
        import psutil

        found = []

        def cb(hwnd, _):
            length = _user32.GetWindowTextLengthW(hwnd)
            if length:
                buf = ctypes.create_unicode_buffer(length + 1)
                _user32.GetWindowTextW(hwnd, buf, length + 1)
                if "media keys" in buf.value.lower():
                    pid = wintypes.DWORD()
                    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    try:
                        if psutil.Process(pid.value).name().lower() == "firefox.exe":
                            found.append(hwnd)
                    except Exception:
                        pass
            return True

        _user32.EnumWindows(_ENUMPROC(cb), 0)
        return found[0] if found else None

    class AppVolume:
        """Per-app volume through Core Audio (the Windows mixer's slider).

        pycaw is comtypes-based, and comtypes initialises COM as STA when it is
        imported: on the event-loop thread that would stall the WinRT awaits the
        media-session manager relies on (see _aumid_of_window). So the volume
        work lives on its own thread, which owns that apartment, and the loop
        only waits on a future.
        """

        def __init__(self):
            self.ready = False
            self._queue = queue.Queue()
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="ytm-volume")
            self._thread.start()
            self.ready = True

        def _run(self):
            try:
                from pycaw.pycaw import AudioUtilities
            except Exception as exc:              # noqa: BLE001 - optional dep
                print(f"Volume keys disabled: pycaw not installed "
                      f"({exc.__class__.__name__}) - pip install pycaw")
                self.ready = False
                return
            cache = {"at": 0.0, "session": None}

            def session_for(pid):
                now = time.monotonic()
                cached = cache["session"]
                if cached is not None and now - cache["at"] < 2.0:
                    try:
                        if pid is None or cached.ProcessId == pid:
                            return cached
                    except Exception:
                        cache["session"] = None
                best = None
                for session in AudioUtilities.GetAllSessions():
                    try:
                        if session.Process is None:
                            continue
                        if pid is not None:
                            if session.ProcessId == pid:
                                best = session
                                break
                        elif getattr(session, "State", 0) == 1 and best is None:
                            best = session        # first actively-playing app
                    except Exception:
                        continue
                cache["at"], cache["session"] = now, best
                return best

            while True:
                pid, sign, reply = self._queue.get()
                try:
                    session = session_for(pid)
                    if session is None:
                        reply.set_result(None)
                        continue
                    volume = session.SimpleAudioVolume
                    if sign is None:                     # read
                        reply.set_result(pct(volume.GetMasterVolume()))
                    else:                                # nudge
                        level = max(0.0, min(1.0, volume.GetMasterVolume()
                                              + sign * VOLUME_STEP))
                        volume.SetMasterVolume(level, None)
                        reply.set_result(pct(level))
                except Exception:
                    reply.set_result(None)

        def _call(self, pid, sign=None, timeout=1.5):
            if not self.ready:
                return None
            reply = concurrent.futures.Future()
            self._queue.put((pid, sign, reply))
            try:
                return reply.result(timeout=timeout)
            except Exception:
                return None

        def get(self, pid):
            return self._call(pid)

        def nudge(self, pid, sign):
            return self._call(pid, sign)

    class WindowsBackend:
        """Windows media sessions (SMTC) + Firefox-friendly playback control.

        Two things the platform forces:

        * Firefox neither reports a playback position for YouTube Music nor
          honours SMTC's transport methods (they return True and do nothing), so
          play/pause/next/previous go out as WM_APPCOMMAND messages posted to
          the window instead, which the browser does act on.
        * There is no window-manager API, so the like shortcut (and seek, which
          has no SMTC equivalent for Firefox) is typed at the focused window
          after raising the YouTube Music window the way a user would.
        """

        name = "smtc"
        seek_step = SEEK_STEP_WIN

        def __init__(self, hint=None):
            from winrt.windows.media.control import (
                GlobalSystemMediaTransportControlsSessionManager as Manager,
                GlobalSystemMediaTransportControlsSessionPlaybackStatus as Status,
            )
            self.Manager, self.Status = Manager, Status
            from winrt.windows.storage.streams import DataReader
            self.DataReader = DataReader
            self.hint = (hint or "").strip().lower()
            self._manager = None
            self._manager_at = 0.0
            self._dirty = False
            self.session = None
            self._windows = (0.0, [])
            self._art_key = None
            self._art = None
            self._volume = AppVolume()
            self._pid = None
            self._guard = {"key": None, "pos": 0.0, "trusted": False}
            self._art_try = 0.0
            self._art_started = 0.0
            self._volume_cache = (0.0, None)
            self._warned_missing = False
            self._sink = None
            self._sink_cache = (0.0, None)

        # ---- sessions
        async def _ensure_manager(self, force=False):
            """A session manager, re-requested often enough to stay truthful.

            A manager (and every session handed out by it) freezes the playback
            state it had when it was requested: after a pause from outside, the
            old object reports "playing" forever. Asking again is the only way to
            see the change, and it costs a few milliseconds, so it is done on a
            short timer and immediately after this bridge sends a command.
            """
            now = time.monotonic()
            if force or self._manager is None or now - self._manager_at >= REFRESH_S:
                self._manager = await self.Manager.request_async()
                self._manager_at = now
            return self._manager

        def windows(self):
            """Visible windows, refreshed at most every WINDOW_CACHE_S."""
            since, cached = self._windows
            if time.monotonic() - since < WINDOW_CACHE_S:
                return cached
            try:
                rows = list_windows()
            except Exception:
                rows = []
            self._windows = (time.monotonic(), rows)
            return rows

        def window_for(self, aumid, title_hint=None):
            """The window of a session: by AUMID, else by the track title."""
            rows = self.windows()
            if aumid:
                for row in rows:
                    if row.aumid and row.aumid.lower() == aumid.lower():
                        return row
            if title_hint:
                needle = title_hint.strip().lower()
                for row in rows:
                    if needle and needle[:20] in row.title.lower():
                        return row
            return None

        def label(self, session):
            """Friendly app name for a session ('firefox'), else its AUMID."""
            aumid = getattr(session, "source_app_user_model_id", "") or ""
            row = self.window_for(aumid)
            if row and row.process:
                stem = row.process.rsplit(".", 1)[0].lower()
                return stem
            return aumid

        def _pick(self, sessions):
            """The session to show: the hinted one, else a playing one, else the first."""
            if self.hint:
                matches = [s for s in sessions
                           if self.hint in self.label(s).lower()
                           or self.hint in (getattr(s, "source_app_user_model_id", "") or "").lower()]
                if matches:
                    for session in matches:
                        if session.get_playback_info().playback_status == self.Status.PLAYING:
                            return session
                    return matches[0]
                # A pinned player that is not running must not silently fall back
                # to whatever else is playing: that would put the wrong track on
                # the device.
                if not self._warned_missing:
                    self._warned_missing = True
                    print(f"No media session matches --player {self.hint!r}; "
                          f"waiting for it (see --list)")
                return None
            for session in sessions:
                try:
                    if session.get_playback_info().playback_status == self.Status.PLAYING:
                        return session
                except Exception:
                    continue
            return sessions[0] if sessions else None

        async def list(self):
            """Human-readable session list for --list."""
            manager = await self._ensure_manager()
            rows = []
            for session in manager.get_sessions():
                props = await session.try_get_media_properties_async()
                try:
                    status = self.Status(session.get_playback_info().playback_status).name
                except Exception:
                    status = "?"
                rows.append(f"{self.label(session):<12} {props.title or '?'} — "
                            f"{props.artist or '?'}  [{status.lower()}]")
            return rows

        async def _thumbnail(self, reference):
            stream = await reference.open_read_async()
            size = int(stream.size)
            if size <= 0:
                return None
            reader = self.DataReader(stream)
            await reader.load_async(size)
            buffer = bytearray(size)
            reader.read_bytes(buffer)
            return bytes(buffer)

        async def read(self):
            """Return dict(title, artist, art, playing, pos, dur, url, vol, app).

            Sessions are re-listed on every poll: a cached session object keeps
            returning its old playback status, so a pause made outside the
            bridge would never show up on the device.
            """
            manager = await self._ensure_manager(force=self._dirty)
            self._dirty = False
            session = self._pick(list(manager.get_sessions()))
            self.session = session
            if session is None:
                return None

            props = await session.try_get_media_properties_async()
            title = (props.title or "Unknown").strip()
            artist = (props.artist or props.album_artist or "").strip()
            info = session.get_playback_info()
            playing = info.playback_status == self.Status.PLAYING

            pos, dur = self._timeline(session, (title, artist), playing)

            # Cover art is a byte stream, and reading it is cheap while building
            # the 1-bit bitmap is not — so the bytes are kept and the frame is
            # only rebuilt when they change (Bridge compares them).
            #
            # The catch is *when* to read them. A browser publishes the track
            # before it has the new cover: for a moment the session still hands
            # out the previous track's thumbnail, and a single read per track
            # then left that old cover on the device for the whole song. So the
            # thumbnail is re-read for the first few seconds of a track, and
            # slower afterwards if it never arrived at all.
            now = time.monotonic()
            key = (title, artist)
            if key != self._art_key:
                self._art_key, self._art = key, None
                self._art_started = now
                self._art_try = 0.0
            settling = now - self._art_started < ART_SETTLE_S
            due = now - self._art_try > (ART_RETRY_S if settling else ART_RECHECK_S)
            if due and (settling or self._art is None):
                self._art_try = now
                reference = getattr(props, "thumbnail", None)
                if reference is not None:
                    try:
                        candidate = await self._thumbnail(reference)
                    except Exception as exc:
                        candidate = None
                        print("  art read failed:", exc)
                    if candidate and candidate != self._art:
                        self._art = candidate

            self._pid = self._pid_for(session)
            return {
                "title": title,
                "artist": artist,
                "art": self._art,
                "playing": playing,
                "pos": pos,
                "dur": dur,
                "url": "",
                "vol": self._volume_level(),
                "app": self.label(session),
            }

        def _volume_level(self):
            """Current app volume, sampled twice a second.

            Reading it means a round trip to the volume thread, so it is cached:
            the level only changes when someone moves a slider or presses a key.
            """
            since, level = self._volume_cache
            if time.monotonic() - since < 0.5:
                return level
            level = self._volume.get(self._pid)
            self._volume_cache = (time.monotonic(), level)
            return level

        def _timeline(self, session, key, playing):
            """(position, duration) in seconds, or (0, 0) when the player has none.

            Firefox's SMTC timeline is a snapshot from whenever YouTube Music last
            published it: the position sits at zero and the total can still be the
            *previous* track's. So a timeline is only believed once its position
            has been seen to move during this track; otherwise the bridge falls
            back to what it does on Linux — count elapsed time itself and look the
            length up with yt-dlp — rather than show a wrong number.
            """
            try:
                timeline = session.get_timeline_properties()
                start = timeline.start_time.total_seconds()
                end = timeline.end_time.total_seconds()
                raw = max(0.0, timeline.position.total_seconds())
                dur = max(0, int(end - start)) if end > start else 0
            except Exception:
                return 0, 0

            guard = self._guard
            if key != guard["key"]:
                guard.update(key=key, pos=raw, trusted=False)
            elif playing and raw > guard["pos"] + 0.05:
                guard["pos"], guard["trusted"] = raw, True
            else:
                guard["pos"] = raw

            if not guard["trusted"]:
                return 0, 0
            return int(raw), dur

        def _pid_for(self, session):
            row = self.window_for(getattr(session, "source_app_user_model_id", "") or "")
            return row.pid if row else None

        # ---- commands
        def _refresh_sink(self):
            """Where Firefox's media keys should be posted (cached for a moment)."""
            since, hwnd = self._sink_cache
            if time.monotonic() - since < WINDOW_CACHE_S:
                self._sink = hwnd
                return hwnd
            hwnd = media_keys_window()
            if hwnd is None and self.session is not None:
                row = self.window_for(getattr(self.session, "source_app_user_model_id", "") or "")
                hwnd = row.hwnd if row else None
            self._sink_cache = (time.monotonic(), hwnd)
            self._sink = hwnd
            return hwnd

        def _is_firefox(self, session):
            aumid = (getattr(session, "source_app_user_model_id", "") or "").lower()
            row = self.window_for(aumid)
            process = (row.process if row else "").lower()
            return "firefox" in process or "firefox" in self.label(session).lower()

        async def command(self, ch):
            session = self.session
            if session is None:
                return
            if ch in ("-", "+"):
                await self.seek(-1 if ch == "-" else +1)
                return

            if self._is_firefox(session):
                self._refresh_sink()
                hwnd = self._sink
                if hwnd is None:
                    print(f"  !! command {ch!r}: no Firefox window to address")
                    return
                appcommand = {"p": APPCOMMAND_MEDIA_PLAY_PAUSE,
                              "n": APPCOMMAND_MEDIA_NEXTTRACK,
                              "b": APPCOMMAND_MEDIA_PREVIOUSTRACK}.get(ch)
                if appcommand is None:
                    return
                self._dirty = True          # state just changed: re-read at once
                ok = _post_appcommand(hwnd, appcommand)
                print(f"  <- command {ch!r} -> Firefox WM_APPCOMMAND {appcommand}"
                      + ("" if ok else " (message dropped)"))
                return

            method = {"p": "try_toggle_play_pause_async",
                      "n": "try_skip_next_async",
                      "b": "try_skip_previous_async"}.get(ch)
            if method is None:
                return
            try:
                self._dirty = True          # state just changed: re-read at once
                ok = await getattr(session, method)()
                print(f"  <- command {ch!r} -> {method} ({'ok' if ok else 'refused'})")
            except Exception as exc:
                print(f"  !! command {ch!r} failed: {exc}")

        async def seek(self, direction):
            """±seek_step. Firefox has no SMTC seek, so it types l/h at the tab."""
            session = self.session
            if session is None:
                return
            if self._is_firefox(session):
                row = self._yt_window() or self.window_for(
                    getattr(session, "source_app_user_model_id", "") or "")
                if row is None:
                    print("  !! seek: no Firefox window to address")
                    return
                key = VK_L if direction > 0 else VK_H
                if self._type_at(row, [key]):
                    print(f"  <- seek {'+' if direction > 0 else '-'}{self.seek_step}s "
                          f"-> Firefox shortcut {'l' if direction > 0 else 'h'}")
                return

            try:
                position = session.get_timeline_properties().position.total_seconds()
            except Exception:
                position = 0
            target = max(0.0, position + direction * self.seek_step)
            try:
                # SMTC takes the position as 100 ns ticks, not a timedelta.
                ok = await session.try_change_playback_position_async(int(target * 10**7))
                print(f"  <- seek -> {target:.0f}s ({'ok' if ok else 'refused'})")
            except Exception as exc:
                print(f"  !! seek failed: {exc}")

        # ---- volume / like
        def volume(self, sign):
            level = self._volume.nudge(self._pid, sign)
            if level is None:
                print("  !! volume: no audio session for this player")
            return level

        def _yt_window(self):
            """The Firefox window that has YouTube Music as its active tab.

            Typing at a window only reaches the *active* tab, so the window is
            identified by its title: YouTube Music puts the site name there,
            and while a track plays the tab title carries the track too.
            """
            rows = [row for row in self.windows() if "firefox" in row.process.lower()]
            for row in rows:
                if "youtube music" in row.title.lower():
                    return row
            return None

        def _type_at(self, row, keys, settle=0.35):
            """Focus a window, type keys at it, then give the focus back.

            Each entry in `keys` is either a virtual-key code to tap, or a
            (modifier, key) pair sent as a chord.
            """
            previous = _user32.GetForegroundWindow()
            if not set_foreground(row.hwnd):
                return False
            try:
                time.sleep(settle)
                for key in keys:
                    if isinstance(key, tuple):
                        chord(*key)
                    else:
                        tap_key(key)
                    time.sleep(0.05)
            finally:
                if previous and previous != row.hwnd:
                    time.sleep(0.15)
                    set_foreground(previous)
            return True

        def like(self):
            row = self._yt_window()
            if row is None:
                return False, "Open the YT Music tab"
            if not self._type_at(row, [(VK_SHIFT, VK_OEM_PLUS)]):
                return False, "Like: cannot focus"
            return True, "Like toggled"


# ---------------------------------------------------------------- imaging
# PIL's "1" mode packs rows MSB-first, while XBM (what canvas_draw_xbm wants) is
# LSB-first, so every byte's bits have to be reversed.
_BIT_REVERSED = bytes(int(f"{i:08b}"[::-1], 2) for i in range(256))


def art_to_xbm(art, size=ART_SIZE):
    """Downscale cover art to a 1-bit bitmap; returns (w, h, bytes).

    `art` is a file path (MPRIS artUrl) or raw image bytes (SMTC thumbnail).
    """
    source = Image.open(io.BytesIO(art)) if isinstance(art, (bytes, bytearray)) else Image.open(art)
    img = source.convert("L").resize((size, size), Image.LANCZOS)
    img = img.point(lambda v: 255 - v)              # invert: dark pixels -> set bits
    bw = img.convert("1", dither=Image.Dither.FLOYDSTEINBERG)
    return size, size, bytes(_BIT_REVERSED[b] for b in bw.tobytes())


# ---------------------------------------------------------------- framing
def frame(ftype, payload):
    return bytes([ftype]) + len(payload).to_bytes(2, "little") + payload


def meta_frame(m, with_text=True):
    """state/pos/dur, plus title/artist unless the caller says they are unchanged.

    A clock-only frame is 8 bytes and fits in one BLE write; carrying the text
    makes it ~80 bytes, which is three or four writes at MTU 23 — that was most
    of the delay between a second ticking and the Flipper showing it.
    """
    payload = (
        bytes([1 if m["playing"] else 0])
        + int(m["pos"]).to_bytes(2, "little", signed=False)
        + int(m["dur"]).to_bytes(2, "little", signed=False)
    )
    if with_text:
        payload += (
            m["title"].encode("utf-8", "replace")[:TITLE_MAX]
            + b"\0"
            + m["artist"].encode("utf-8", "replace")[:ARTIST_MAX]
            + b"\0"
        )
    return frame(FRAME_META, payload)


def toast_frame(text):
    """One short line for the device to show for a moment."""
    return frame(FRAME_TOAST, text.encode("utf-8", "replace")[:TOAST_MAX])


def art_frame(art):
    w, h, data = art_to_xbm(art)
    return frame(FRAME_ART, bytes([w, h]) + data)


def vol_frame(level):
    return frame(FRAME_VOL, bytes([max(0, min(100, int(level)))]))


# ---------------------------------------------------------------- BLE
async def send_toast_once(name_hint, text):
    """Connect, send one toast, disconnect — a way to check the toast UI."""
    dev = await find_flipper(name_hint)
    if not dev:
        for address in known_addresses(name_hint):
            if drop_stale_link(address):
                await asyncio.sleep(2)
            dev = await find_flipper(name_hint) or address
            break
    if not dev:
        sys.exit("no device found")

    async with BleakClient(dev, timeout=CONNECT_TIMEOUT) as client:
        write_char, _ = pick_chars(client)
        if not write_char:
            sys.exit("serial characteristics not found")
        blob = toast_frame(text)
        await client.write_gatt_char(write_char, blob, response=False)
        print(f"toast sent ({len(blob)} bytes): {text!r}")
        await asyncio.sleep(1.0)


async def pair_flipper(name_hint, address_hint=None):
    """Pair with the Flipper once (Windows needs a bond for the serial profile)."""
    dev = address_hint or (await find_flipper(name_hint)) or load_known_address()
    if not dev:
        sys.exit("no device found — is the Flipper on and advertising?")
    print(f"Pairing with {dev} ...")
    async with BleakClient(dev, timeout=CONNECT_TIMEOUT) as client:
        await client.pair()
        print("Paired. The bridge can connect now.")
        save_known_address(getattr(dev, "address", dev))


async def find_flipper(name_hint):
    print("Scanning for the Flipper (make sure it's advertising)...")
    try:
        devices = await BleakScanner.discover(timeout=8.0)
    except Exception as exc:                      # radio off, no adapter, service down
        # Windows says "Bluetooth radio is not powered on" here; a bridge started
        # at login can easily beat the radio, so this retries instead of dying.
        print(f"Bluetooth unavailable: {exc}")
        return None
    for d in devices:
        nm = d.name or ""
        if name_hint.lower() in nm.lower():
            print(f"Found {nm!r} [{d.address}]")
            return d
    print("No device matched hint", repr(name_hint))
    for d in devices:
        print(f"  seen: {d.address}  {d.name!r}")
    return None


def known_addresses(name_hint):
    """Addresses already known for this device.

    BlueZ: a peripheral it still believes is connected stops advertising, so an
    empty scan does not mean the Flipper is gone — connecting straight to the
    known address is what makes an always-on bridge recover on its own.
    Windows keeps that address in a small cache file instead (there is no
    bluetoothctl to ask), which also skips the scan on a busy radio.
    """
    if IS_WINDOWS:
        cached = load_known_address()
        return [cached] if cached else []
    try:
        out = subprocess.run(["bluetoothctl", "devices"], capture_output=True, text=True)
    except FileNotFoundError:
        return []
    found = []
    for line in out.stdout.splitlines():
        parts = line.split(" ", 2)
        if len(parts) == 3 and name_hint.lower() in parts[2].lower():
            found.append(parts[1])
    return found


def bluetoothctl(*args, timeout=10):
    try:
        return subprocess.run(["bluetoothctl", *args], capture_output=True,
                              text=True, timeout=timeout).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def drop_stale_link(address):
    """Clear a link BlueZ still holds after the previous bridge died.

    While BlueZ thinks the Flipper is connected it will not advertise, and
    connecting again just hangs on the dead link — dropping it first is what lets
    the bridge recover unattended, which is the whole point of running it as a
    service. Windows' BLE stack owns the link itself and releases it with the
    process, so there is nothing to clear there.
    """
    if IS_WINDOWS:
        return False
    if "Connected: yes" not in bluetoothctl("info", address):
        return False
    print(f"{address} is still marked connected (stale link); dropping it")
    bluetoothctl("disconnect", address)
    return True


# Flipper serial service. UUIDs and roles from the firmware's own
# serial_service.c / serial_service_uuid.inc:
#   RX (PC->Flipper)  write/write-without-response/read
#   TX (Flipper->PC)  read + INDICATE          <- the data char
#   FLOW_CONTROL      read + notify            (peripheral RX credit, BE u32)
#   RPC_STATUS        read/write/notify        (RPC session flag)
# Subscribing to FLOW_CONTROL instead of TX is the classic mistake: the app's
# bytes arrive as *indications* on TX and simply never show up.
SERIAL_SVC = "8fe5b3d5-2e7f-4a98-2a48-7acc60fe0000"
RX_UUID = "19ed82ae-ed21-4c9d-4145-228e62fe0000"     # PC -> Flipper (write)
TX_UUID = "19ed82ae-ed21-4c9d-4145-228e61fe0000"     # Flipper -> PC (indicate)
FLOW_CTRL_UUID = "19ed82ae-ed21-4c9d-4145-228e63fe0000"
RPC_STATUS_UUID = "19ed82ae-ed21-4c9d-4145-228e64fe0000"


def pick_chars(client):
    """Find (write_char, notify_char) on the Flipper serial service by UUID.

    A blind property scan over all services returns GATT 'Service Changed' as
    the notify char and the flow-control char as the write char, which silently
    sends frames nowhere, so the known UUIDs win and properties are only a
    fallback inside the serial service.
    """
    svc = next((s for s in client.services if s.uuid.lower() == SERIAL_SVC), None)
    if svc is not None:
        skip = (FLOW_CTRL_UUID, RPC_STATUS_UUID)
        w = next((c for c in svc.characteristics if c.uuid.lower() == RX_UUID), None)
        n = next((c for c in svc.characteristics if c.uuid.lower() == TX_UUID), None)
        if w is None:
            w = next((c for c in svc.characteristics
                      if c.uuid.lower() not in skip
                      and ("write-without-response" in c.properties
                           or "write" in c.properties)), None)
        if n is None:
            n = next((c for c in svc.characteristics
                      if c.uuid.lower() not in skip and "indicate" in c.properties), None)
        if n is None:
            n = next((c for c in svc.characteristics
                      if c.uuid.lower() not in skip and "notify" in c.properties), None)
        if w and n:
            return w, n
    for service in client.services:                 # fallback: skip standard GATT
        if service.uuid.lower().startswith(("00001800", "00001801", "0000180a")):
            continue
        w = n = None
        for ch in service.characteristics:
            if "write" in ch.properties or "write-without-response" in ch.properties:
                w = ch
            if "notify" in ch.properties or "indicate" in ch.properties:
                n = ch
        if w and n:
            return w, n
    return None, None


async def prepare(client):
    """Negotiate the ATT MTU so writes fit; returns the chunk size.

    BlueZ needs an explicit _acquire_mtu() (and discovers services first);
    Windows negotiates on its own and just reports the result through mtu_size.
    """
    mtu = 23
    acquire = getattr(client, "_acquire_mtu", None)
    if acquire is not None:
        try:
            await acquire()                 # needs services already discovered
            mtu = getattr(client, "mtu_size", 23) or 23
        except Exception as e:
            print("  MTU negotiation unavailable:", e)
    else:
        try:
            mtu = int(client.mtu_size) or 23
        except Exception:
            mtu = 23
    return max(20, min(WRITE_CHUNK, mtu - 3))


class TrackClock:
    """Elapsed-time clock for players that do not report a position.

    Firefox exposes Position = 0 for the whole of YouTube Music (and no
    mpris:length either), so elapsed time is accumulated locally while the
    player says "Playing", reset when the track changes, and resynced whenever
    a player *does* report a real position (mpv, VLC, local files).

    Caveat: with no position from the player the count starts when the bridge
    first sees the track, so joining mid-song shows elapsed time from that
    point until the track changes.
    """

    def __init__(self):
        self.key = None
        self.last = time.monotonic()
        self.acc = 0.0
        self.authoritative = False

    def update(self, key, playing, real_pos, start_age=None):
        now = time.monotonic()
        dt = now - self.last
        self.last = now

        if key != self.key:                     # new track
            self.key = key
            if real_pos:
                self.acc = float(real_pos)      # the player told us where it is
            elif start_age is not None and playing:
                self.acc = float(start_age)     # joined mid-track: date it
            else:
                self.acc = 0.0                  # started here, or paused: unknown
            self.authoritative = bool(real_pos)
            return int(self.acc)

        # accumulate whenever this tick brought no position from the player
        # (so a player that reports one only now and then cannot freeze us)
        if playing and not real_pos:
            self.acc += dt

        if real_pos:                            # trust the player when it has one
            if not self.authoritative or abs(real_pos - self.acc) > 2:
                self.acc = float(real_pos)
            self.authoritative = True
        return int(self.acc)

    def nudge(self, seconds):
        self.acc = max(0.0, self.acc + seconds)


class Bridge:
    def __init__(self, backend, name_hint, hz=POLL_HZ, address=None):
        self.backend = backend
        self.name_hint = name_hint
        self.address = address
        self.hz = max(1, min(POLL_HZ_MAX, hz))
        self.client = None
        self.write_char = None
        self.chunk = WRITE_CHUNK
        self.last_art = None
        self.last_vol = None
        self.last_text = None
        self.device_caps = False
        self.last_sent = 0.0
        self.last_sig = None
        self.last_log = 0.0
        self.clock = TrackClock()
        self.durations = {}
        self.length_tries = {}
        self.length_pending = set()
        self.ytdlp = resolve_ytdlp()
        self.status = {}
        self.status_path = STATUS_PATH

    def write_status(self, m):
        """Snapshot for check_time.py, at most once a second."""
        snapshot = {
            "time": time.time(),
            "title": m["title"],
            "artist": m["artist"],
            "playing": m["playing"],
            "pos": m["pos"],
            "dur": m["dur"],
            "vol": m["vol"],
        }
        if (snapshot["pos"], snapshot["title"], snapshot["playing"]) == (
            self.status.get("pos"), self.status.get("title"), self.status.get("playing")
        ):
            return
        self.status = snapshot
        try:
            os.makedirs(os.path.dirname(self.status_path), exist_ok=True)
            tmp = self.status_path + ".tmp"
            with open(tmp, "w") as handle:
                json.dump(snapshot, handle)
            os.replace(tmp, self.status_path)
        except OSError as e:
            print("  status file:", e)

    async def duration_for(self, url, query):
        """Cached track length, or 0 while a lookup is in flight.

        Looked up by URL when the URL is the track's, otherwise by searching for
        the title and artist. Only successes are cached and failures retry after
        DUR_RETRY_S, so one bad lookup cannot pin an unknown length on the track
        for its whole runtime. The key is whatever identified the track: keying a
        search by URL would hand every track playing from the home page the same
        (wrong) length.

        This never waits for yt-dlp: resolving a length takes ~1.5 s, and awaiting
        it here held up the whole metadata push, so skipping a track took a second
        or two to appear on the device. The lookup runs in the background and the
        next tick (<= 50 ms) picks the answer up from the cache.
        """
        if self.ytdlp is None:
            return 0
        target = url if is_track_url(url) else (f"ytsearch1:{query}" if query else "")
        if not target:
            return 0
        if target in self.durations:
            return self.durations[target]
        if len(self.length_tries) > DUR_CACHE_MAX:
            self.durations.clear()
            self.length_tries.clear()

        now = time.monotonic()
        if target in self.length_pending:
            return 0
        if now - self.length_tries.get(target, 0.0) < DUR_RETRY_S:
            return 0
        self.length_tries[target] = now
        self.length_pending.add(target)
        asyncio.create_task(self.resolve_duration(target))
        return 0

    async def resolve_duration(self, target):
        """The background half of duration_for: yt-dlp, then into the cache."""
        try:
            loop = asyncio.get_running_loop()
            length = await loop.run_in_executor(None, self.yt_duration, target)
            if length:
                self.durations[target] = length
        except Exception as e:
            print("  length lookup failed:", e)
        finally:
            self.length_pending.discard(target)

    def yt_duration(self, target):
        try:
            out = subprocess.run(
                [self.ytdlp, "--no-warnings", "--no-playlist", "--skip-download",
                 "--print", "%(duration)s", target],
                capture_output=True, text=True, timeout=30,
            )
            return int(float(out.stdout.strip().splitlines()[0]))
        except Exception:
            return 0            # the caller decides whether to retry

    async def prepare_meta(self, m):
        """Fill in what the player does not report: elapsed time and length."""
        # Keyed on the track, not the URL: the URL is often just the YouTube Music
        # home page, which would make every track look like the same one and stop
        # the elapsed clock from resetting.
        key = f'{m["title"]}|{m["artist"]}'
        if key == "|":
            key = m["url"]
        if m["dur"] == 0:
            m["dur"] = await self.duration_for(
                m["url"], f'{m["title"]} {m["artist"]}'.strip())

        start_age = None
        if key != self.clock.key and m["playing"]:
            # First sight of this track: date it from Firefox's art file (one
            # stat per track, not per poll). Firefox does NOT always rewrite
            # that file, so a file older than the track itself tells us nothing
            # — better to start at 0 than to seed a wildly wrong position. The
            # Windows backend has no art file, so it simply reports no age.
            age = track_start_age(m["art"])
            if age is not None and (not m["dur"] or age <= m["dur"]):
                start_age = age

        m["pos"] = self.clock.update(key, m["playing"], m["pos"], start_age)
        if m["dur"] and m["pos"] > m["dur"]:
            m["pos"] = m["dur"]
        return m

    async def send_volume(self, level):
        """The only writer of volume frames, so last_vol cannot be raced."""
        await self.write(vol_frame(level))
        self.last_vol = level

    async def toast(self, text):
        """Show a short message on the device (the like outcome, say)."""
        if self.write_char is None:
            return
        await self.write(toast_frame(text))

    async def on_notify(self, _char, data: bytearray):
        loop = asyncio.get_event_loop()
        for byte in data:
            ch = chr(byte)
            if ch == CMD_CAPS:
                # the device can keep the title/artist itself, so the per-tick
                # frame drops to 8 bytes (one BLE write instead of three or four)
                self.device_caps = True
                print("  <- device takes clock-only frames")
            elif ch in CMD_VOLUME:
                level = await loop.run_in_executor(
                    None, self.backend.volume, CMD_VOLUME[ch])
                if level is None:
                    print(f"  <- volume {ch!r}: player exposes no volume")
                else:
                    print(f"  <- command {ch!r} -> volume {level}%")
                    await self.send_volume(level)
            elif ch in CMD_TRANSPORT:
                await self.backend.command(ch)
                # keep our own clock in step with a seek we just issued
                if ch == "-":
                    self.clock.nudge(-self.backend.seek_step)
                elif ch == "+":
                    self.clock.nudge(self.backend.seek_step)
            elif ch == CMD_LIKE:
                ok, message = await loop.run_in_executor(None, self.backend.like)
                print(f"  <- like: {message}" + ("" if ok else " (nothing typed)"))
                await self.toast(message)

    async def push(self, m):
        if self.write_char is None:
            return
        now = time.monotonic()

        if now - self.last_sent >= 1.0 / self.hz:
            # text goes out only when it changes: the device keeps the last one,
            # so the per-tick frame stays 8 bytes and lands in a single write
            text = (m["title"], m["artist"])
            # Until the device advertises that it keeps its own text, repeat it
            # every frame: an older build blanks the title on a text-less one.
            await self.write(meta_frame(
                m, with_text=(text != self.last_text) or not self.device_caps))
            self.last_text = text
            self.last_sent = now
            # log on change, plus a heartbeat, so a fast stream stays readable
            sig = (m["title"], m["artist"], m["playing"])
            if sig != self.last_sig or now - self.last_log >= LOG_HEARTBEAT:
                self.last_sig = sig
                self.last_log = now
                print(f"  -> {m['title'][:38]!r} / {m['artist'][:22]!r} "
                      f"{m['pos']}s/{m['dur']}s {'play' if m['playing'] else 'pause'}")

        if m["art"] and m["art"] != self.last_art:
            try:
                blob = art_frame(m["art"])
                await self.write(blob)
                self.last_art = m["art"]
                print(f"  -> art {len(blob)} bytes")
            except Exception as e:
                print("  art failed:", e)

        if m["vol"] is not None and m["vol"] != self.last_vol:
            await self.send_volume(m["vol"])
            print(f"  -> volume {m['vol']}%")

        self.write_status(m)

    async def write(self, blob):
        for i in range(0, len(blob), self.chunk):
            part = blob[i:i + self.chunk]
            for attempt in range(4):
                try:
                    await self.client.write_gatt_char(self.write_char, part, response=False)
                    break
                except Exception as e:
                    if attempt == 3:
                        raise
                    # A write that lands while the Flipper's serial profile is
                    # still coming up is answered with 0x0E "Unlikely Error".
                    # Back off and retry instead of dropping the connection.
                    print(f"  write retry ({e}); waiting")
                    await asyncio.sleep(0.4 * (attempt + 1))

    async def find_device(self):
        """A scanned device, or the known address if nothing is advertising."""
        if self.address:
            return self.address          # pinned with --address: skip the scan
        dev = await find_flipper(self.name_hint)
        if dev:
            return dev

        for address in known_addresses(self.name_hint):
            if drop_stale_link(address):
                await asyncio.sleep(2)          # let it advertise again
                dev = await find_flipper(self.name_hint)
                if dev:
                    return dev
            print(f"Not advertising; connecting straight to {address}")
            return address
        return None

    async def run(self):
        while True:
            dev = await self.find_device()
            if not dev:
                await asyncio.sleep(5)
                continue
            try:
                async with BleakClient(dev, timeout=CONNECT_TIMEOUT) as client:
                    self.client = client
                    self.write_char, notify_char = pick_chars(client)
                    if not self.write_char or not notify_char:
                        print("Serial service not found — is the app running and BLE on?")
                        await asyncio.sleep(5)
                        continue
                    self.chunk = await prepare(client)
                    save_known_address(getattr(dev, "address", str(dev)))
                    print(f"Connected. write={self.write_char.uuid} "
                          f"notify={notify_char.uuid} chunk={self.chunk}")
                    await client.start_notify(notify_char, self.on_notify)
                    # Only one central may hold the Flipper's serial link: a
                    # second connection (an earlier bridge, a phone) silently
                    # kicks the first one off.
                    await asyncio.sleep(0.5)
                    self.last_art = None
                    self.last_vol = None
                    self.last_text = None
                    self.device_caps = False  # whatever connects must announce again
                    while client.is_connected:
                        m = await self.backend.read()
                        if m:
                            await self.push(await self.prepare_meta(m))
                        await asyncio.sleep(1.0 / self.hz)
            except Exception as e:
                print("Connection error:", e)
            finally:
                self.client = None
                self.write_char = None
            print("Disconnected; retrying...")
            await asyncio.sleep(3)


def is_track_url(url):
    """True when the URL addresses the track itself rather than just the site.

    Firefox reports the *tab's* URL, and YouTube Music keeps playing while you
    browse, so it is often only the home page — looking that up gets
    "Unsupported URL" and the track shows no length.
    """
    return bool(url) and ("/watch?" in url or "youtu.be/" in url)


# ---------------------------------------------------------------- main
async def dump_once(bridge, wait=12.0):
    """Print what the bridge sees, and exit. No Flipper needed.

    It goes through the real pipeline (clock, yt-dlp length lookup), so this is
    the same numbers the device would be sent — minus the BLE hop.
    """
    deadline = time.monotonic() + wait
    meta = None
    while time.monotonic() < deadline:
        meta = await bridge.backend.read()
        if meta:
            meta = await bridge.prepare_meta(meta)
            if meta["dur"]:
                break            # length resolved, nothing left to wait for
        await asyncio.sleep(0.5)
    if not meta:
        print("No media session found — start playback first.")
        return 1

    print(f"app      : {meta['app'] or '?'}")
    print(f"title    : {meta['title']}")
    print(f"artist   : {meta['artist'] or '?'}")
    print(f"state    : {'playing' if meta['playing'] else 'paused'}")
    print(f"position : {meta['pos']}s")
    print(f"length   : {meta['dur']}s" + ("" if meta["dur"] else "  (unknown)"))
    print(f"volume   : {'?' if meta['vol'] is None else str(meta['vol']) + '%'}")
    print(f"art      : {'none' if not meta['art'] else 'yes'}")

    if meta["art"]:
        try:
            w, h, data = art_to_xbm(meta["art"])
            out = Path(os.environ.get("TEMP", ".")) / "ytm-art.xbm.bin"
            out.write_bytes(data)
            print(f"art xbm  : {w}x{h}, {len(data)} bytes -> {out}")
        except Exception as exc:
            print("art convert failed:", exc)
    return 0


async def main_async(args):
    if args.list:
        if IS_WINDOWS:
            backend = WindowsBackend(args.player)
            rows = await backend.list()
            if not rows:
                print("No media sessions — start playback in Firefox/YouTube Music first.")
            for row in rows:
                print(row)
            return 0
        mpris = MprisBackend(args.player)
        for player in mpris.list():
            print(player)
        return 0

    if args.toast:
        await send_toast_once(args.name, args.toast)
        return 0

    if args.pair:
        await pair_flipper(args.name, args.address)
        return 0

    if IS_WINDOWS:
        backend = WindowsBackend(args.player)
    else:
        player = args.player
        if not player:
            mpris = MprisBackend(None)
            players = mpris.list()
            waited = getattr(args, "wait_for_player", False)
            while not players and waited:
                print("Waiting for a media player (start YouTube Music)...")
                time.sleep(5)
                players = mpris.list()
            if not players:
                sys.exit("No MPRIS players found — start YouTube Music / a video first.")
            player = players[0]
            print("Using player:", player)
        backend = MprisBackend(player)

    if args.cmd:
        if args.cmd not in ("p", "n", "b", "-", "+", "u", "d", "l"):
            sys.exit("--cmd takes one of p n b - + u d l")
        meta = await backend.read()
        if meta is None:
            sys.exit("No media session found — start playback first.")
        print(f"player: {meta['app'] or '?'} — {meta['title']} / {meta['artist']}")
        if args.cmd in CMD_VOLUME:
            level = backend.volume(CMD_VOLUME[args.cmd])
            print("volume:", "unavailable" if level is None else f"{level}%")
        elif args.cmd == "l":
            ok, message = backend.like()
            print("like:", message, "" if ok else "(nothing typed)")
        else:
            await backend.command(args.cmd)
        return 0

    if args.dump:
        return await dump_once(Bridge(backend, args.name, args.hz, args.address))

    bridge = Bridge(backend, args.name, args.hz, args.address)
    print(f"Polling at {bridge.hz:g} Hz")
    await bridge.run()
    return 0


def setup_log(path, max_bytes=2_000_000):
    """Send everything this process prints to a file, for a console-less start.

    Started from a Startup shortcut (pythonw.exe) there is no stdout at all, so
    prints would either vanish or crash. Appending to a file keeps the same
    messages the console build shows; it is rotated once when it grows too big so
    a machine that has been up for weeks does not carry a huge log.
    """
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        if target.exists() and target.stat().st_size > max_bytes:
            target.replace(target.with_name(target.name + ".1"))
    except OSError:
        pass
    handle = open(target, "a", encoding="utf-8", errors="replace", buffering=1)
    sys.stdout = handle
    sys.stderr = handle
    return handle


def main():
    ap = argparse.ArgumentParser(description="Flipper YT Music Remote bridge")
    ap.add_argument("--player",
                    help="MPRIS player name (Linux) / app name, exe or AUMID substring "
                         "(Windows, e.g. firefox); default: auto")
    ap.add_argument("--name", default=os.environ.get("FLIPPER_NAME", "Flipper"),
                    help="BLE name substring of your Flipper")
    ap.add_argument("--list", action="store_true", help="list media players and exit")
    ap.add_argument("--hz", type=float, default=POLL_HZ,
                    help=f"metadata pushes per second (default {POLL_HZ}, max {POLL_HZ_MAX})")
    ap.add_argument("--toast", metavar="TEXT",
                    help="connect, show one toast on the device, and exit "
                         "(checks the device's toast UI without the buttons)")
    ap.add_argument("--wait-for-player", action="store_true",
                    help="sit and wait for a media player instead of exiting "
                         "(for running as a service from login)")
    ap.add_argument("--dump", action="store_true",
                    help="print the track the bridge would send, then exit "
                         "(no Flipper needed — good first check)")
    ap.add_argument("--cmd", metavar="KEY",
                    help="send one device command and exit: p n b - + u d l")
    ap.add_argument("--pair", action="store_true",
                    help="pair with the Flipper and exit (Windows: pair once before first use)")
    ap.add_argument("--address", help="connect to this BLE address instead of scanning")
    ap.add_argument("--log", metavar="PATH",
                    help="append all output to this file instead of the console "
                         "(for starting hidden at login)")
    args = ap.parse_args()

    if args.log:
        setup_log(args.log)
    # Line-buffered output: run as a service (or with its output in a file) a
    # block-buffered stdout would hide the log for minutes.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    try:
        sys.exit(asyncio.run(main_async(args)) or 0)
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
