#!/usr/bin/env python3
"""
ytm_bridge — PC side of the "YT Music Remote" Flipper Zero app.

Reads what's playing from the desktop media session (MPRIS, via playerctl),
pushes title/artist/progress/volume/album-art to the Flipper over Bluetooth LE,
and runs the playback commands the Flipper sends back.

Transport: the Flipper's BLE serial GATT service (the same one the official
mobile app uses). We connect as BLE central and exchange raw bytes.

Wire format (PC -> Flipper) — [type:1][len:2 LE][payload:len]
    0x01 META: [state:1][pos:2 LE][dur:2 LE][title utf8]0[artist utf8]0
    0x02 ART : [w:1][h:1][1-bit XBM rows, LSB-first per byte, (w+7)/8 per row]
    0x03 VOL : [volume:1]  0..100
Flipper -> PC: one command byte each (CMD_TRANSPORT / CMD_VOLUME below)
    p play/pause   n next   b previous   - seek back   + seek forward
    u volume up    d volume down

Requires: bleak, pillow  (pip install bleak pillow), playerctl, and optionally
yt-dlp (track length for players that report no mpris:length).
"""

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    sys.exit("Missing dependency: pip install bleak")

# ---- tunables ----
ART_SIZE = 44          # px, square; the largest cover that still leaves a row
                       # between it and the bar (64 rows: cover 44 + bar 7 + time 8)
SEEK_STEP = 5          # seconds per seek tap
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

FRAME_META, FRAME_ART, FRAME_VOL, FRAME_TOAST = 0x01, 0x02, 0x03, 0x04
TOAST_MAX = 24         # the device shows one short line

# Toggling like means typing YouTube Music's web-app shortcut (shift+=), because
# MPRIS has no like action. vkkey is a small Wayland virtual-keyboard client built
# into ~/.local/share/ytm-remote/bin (see its source there); the service gets a
# bare PATH, so it is addressed by path rather than looked up.
VKKEY = Path.home() / ".local/share/ytm-remote/bin/vkkey"
LIKE_KEYS = ["-m", "1", "13"]  # 1 = shift, 13 = '='


def resolve_ytdlp():
    """Find yt-dlp, preferring the one beside our own interpreter.

    Run as a service this process gets a bare PATH (/usr/local/bin:/usr/bin), so
    shutil.which() alone finds nothing and every track length silently stays
    unknown -- install it into the bridge's own venv and it is always found.
    """
    beside = Path(sys.executable).with_name("yt-dlp")
    if beside.exists():
        return str(beside)
    found = shutil.which("yt-dlp")
    if not found:
        print("yt-dlp not found: track lengths will show as --:--")
    return found

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


def toggle_like():
    """Toggle like on the YouTube Music tab. Returns (ok, message).

    The like shortcut belongs to the web app, so it can only be typed at a
    focused window — there is no way to address a browser tab from outside. A
    window whose *title* shows YouTube Music has it as its active tab, so that is
    what gets checked: firing the keystroke blind would type a '+' into whatever
    else happened to have focus.
    """
    if not VKKEY.exists():
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
            time.sleep(0.35)          # let the focus change land before typing
        subprocess.run([str(VKKEY), *LIKE_KEYS], capture_output=True, timeout=5)
    except Exception as e:
        return False, f"Like failed: {e}"
    finally:
        if moved and previous:
            time.sleep(0.15)
            swaymsg(f"[con_id={previous}] focus")   # give focus back
    return True, "Like toggled"


def is_track_url(url):
    """True when the URL addresses the track itself rather than just the site.

    Firefox reports the *tab's* URL, and YouTube Music keeps playing while you
    browse, so it is often only the home page — looking that up gets
    "Unsupported URL" and the track shows no length.
    """
    return bool(url) and ("/watch?" in url or "youtu.be/" in url)


# Live snapshot of what the device is being fed, for tooling (check_time.py).
# Rewritten at most once a second, so reading it costs nothing and a wrong timer
# can be spotted without watching the screen.
STATUS_PATH = os.path.expanduser("~/.cache/ytm-remote/status.json")

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

# The format string is built from the field list, so the two cannot disagree.
META_FIELDS = (
    "status",
    "position",
    "mpris:length",
    "xesam:title",
    "xesam:artist",
    "mpris:artUrl",
    "xesam:url",
    "volume",          # playerctl exposes Volume through the metadata format too
)
META_FMT = "\t".join("{{%s}}" % name for name in META_FIELDS)


# ---------------------------------------------------------------- MPRIS
def run_playerctl(player, *args):
    """playerctl for one player. Returns (ok, stdout); the caller decides what a
    failure means (routine during a poll, worth reporting for a button press)."""
    try:
        out = subprocess.run(
            ["playerctl", "-p", player, *args], capture_output=True, text=True
        )
    except FileNotFoundError:
        return False, ""
    return out.returncode == 0, out.stdout


def track_start_age(art_path, max_age=6 * 3600):
    """Seconds since Firefox wrote this track's artwork.

    Firefox reports no playback position for YouTube Music, but it writes a
    fresh art file when a track starts, so that file's mtime dates the track —
    which is what lets the bridge join a song already in progress and still show
    the true elapsed time. Returns None when the age is implausible.
    """
    if not art_path:
        return None
    try:
        age = time.time() - os.path.getmtime(art_path)
    except OSError:
        return None
    return age if 0 <= age < max_age else None


def pct(fraction):
    """0.0..1.0 volume -> whole percent, or None if the player reports none."""
    try:
        return max(0, min(100, int(round(float(fraction) * 100))))
    except (TypeError, ValueError):
        return None


def list_players():
    try:
        out = subprocess.run(["playerctl", "-l"], capture_output=True, text=True)
    except FileNotFoundError:
        sys.exit("playerctl not found — install it to read the media session")
    return [p for p in out.stdout.splitlines() if p.strip()]


def read_meta(player):
    """Return dict(title, artist, art_path, playing, pos, dur, url, vol) or None.

    Runs on the event loop on purpose: playerctl answers in a few milliseconds
    and this is the poll itself (see Bridge.run). The playerctl calls that are
    triggered by device events go through an executor instead.
    """
    ok, stdout = run_playerctl(player, "metadata", "--format", META_FMT)
    if not ok or not stdout.strip():
        return None

    parts = stdout.rstrip("\n").split("\t")
    parts += [""] * (len(META_FIELDS) - len(parts))
    f = dict(zip(META_FIELDS, parts))

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
        "art_path": art_path,
        "playing": f["status"].strip().lower() == "playing",
        "pos": us(f["position"]),
        "dur": us(f["mpris:length"]),
        "url": (f["xesam:url"] or "").strip(),
        "vol": pct(f["volume"]),
    }


def do_command(player, ch):
    args = CMD_TRANSPORT.get(ch)
    if not args:
        return
    ok, _ = run_playerctl(player, *args)
    if ok:
        print(f"  <- command {ch!r} -> playerctl {' '.join(args)}")
    else:
        print(f"  !! command {ch!r} failed: playerctl {' '.join(args)}")


def do_volume(player, sign):
    """Nudge the volume by VOLUME_STEP; returns the new level or None."""
    step = f"{VOLUME_STEP:g}{'+' if sign > 0 else '-'}"
    run_playerctl(player, "volume", step)
    ok, out = run_playerctl(player, "volume")
    return pct(out) if ok else None


# ---------------------------------------------------------------- imaging
# PIL's "1" mode packs rows MSB-first, while XBM (what canvas_draw_xbm wants) is
# LSB-first, so every byte's bits have to be reversed.
_BIT_REVERSED = bytes(int(f"{i:08b}"[::-1], 2) for i in range(256))


def art_to_xbm(path, size=ART_SIZE):
    """Load an image and downscale it to a 1-bit bitmap; returns (w, h, bytes)."""
    img = Image.open(path).convert("L").resize((size, size), Image.LANCZOS)
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


def art_frame(path):
    w, h, data = art_to_xbm(path)
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


async def find_flipper(name_hint):
    print("Scanning for the Flipper (make sure it's advertising)...")
    devices = await BleakScanner.discover(timeout=8.0)
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
    """Addresses BlueZ already knows for this device.

    A peripheral that BlueZ still believes is connected stops advertising, so an
    empty scan does not mean the Flipper is gone — connecting straight to the
    known address is what makes an always-on bridge recover on its own.
    """
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
    service.
    """
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
    """Ask BlueZ for the real ATT MTU so writes fit; returns the chunk size."""
    mtu = 23
    acquire = getattr(client, "_acquire_mtu", None)
    if acquire is not None:
        try:
            await acquire()                 # needs services already discovered
            mtu = getattr(client, "mtu_size", 23) or 23
        except Exception as e:
            print("  MTU negotiation unavailable:", e)
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
    def __init__(self, player, name_hint, hz=POLL_HZ):
        self.player = player
        self.name_hint = name_hint
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
            # — better to start at 0 than to seed a wildly wrong position.
            age = track_start_age(m["art_path"])
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
                level = await loop.run_in_executor(None, do_volume, self.player, CMD_VOLUME[ch])
                if level is None:
                    print(f"  <- volume {ch!r}: player exposes no volume")
                else:
                    print(f"  <- command {ch!r} -> volume {level}%")
                    await self.send_volume(level)
            elif ch in CMD_TRANSPORT:
                await loop.run_in_executor(None, do_command, self.player, ch)
                # keep our own clock in step with a seek we just issued
                if ch == "-":
                    self.clock.nudge(-SEEK_STEP)
                elif ch == "+":
                    self.clock.nudge(SEEK_STEP)
            elif ch == CMD_LIKE:
                ok, message = await loop.run_in_executor(None, toggle_like)
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

        if m["art_path"] and m["art_path"] != self.last_art:
            try:
                blob = art_frame(m["art_path"])
                await self.write(blob)
                self.last_art = m["art_path"]
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
                        m = read_meta(self.player)
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


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Flipper YT Music Remote bridge")
    ap.add_argument("--player", help="MPRIS player name (default: auto)")
    ap.add_argument("--name", default=os.environ.get("FLIPPER_NAME", "Flipper"),
                    help="BLE name substring of your Flipper")
    ap.add_argument("--list", action="store_true", help="list players and exit")
    ap.add_argument("--hz", type=float, default=POLL_HZ,
                    help=f"metadata pushes per second (default {POLL_HZ}, max {POLL_HZ_MAX})")
    ap.add_argument("--toast", metavar="TEXT",
                    help="connect, show one toast on the device, and exit "
                         "(checks the device's toast UI without the buttons)")
    ap.add_argument("--wait-for-player", action="store_true",
                    help="sit and wait for a media player instead of exiting "
                         "(for running as a service from login)")
    args = ap.parse_args()

    if args.list:
        for p in list_players():
            print(p)
        return

    if args.toast:
        asyncio.run(send_toast_once(args.name, args.toast))
        return

    player = args.player
    if not player:
        players = list_players()
        while not players and args.wait_for_player:
            print("Waiting for a media player (start YouTube Music)...")
            time.sleep(5)
            players = list_players()
        if not players:
            sys.exit("No MPRIS players found — start YouTube Music / a video first.")
        player = players[0]
        print("Using player:", player)

    bridge = Bridge(player, args.name, args.hz)
    print(f"Polling at {bridge.hz:g} Hz")
    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
