#!/usr/bin/env python3
"""
ytm_bridge - PC side of the "YT Music Remote" Flipper Zero app.

Reads what's playing from the desktop media session, pushes
title/artist/progress/volume/album-art to the Flipper over Bluetooth LE, and
runs the playback commands the Flipper sends back.

Transport: the Flipper's BLE serial GATT service (the same one the official
mobile app uses). We connect as BLE central and exchange raw bytes.

Media source: on Linux this is MPRIS via playerctl, on Windows it is the
Windows media session (SMTC, the same source the volume flyout shows) read
through WinRT. Firefox appears in both, but each platform has its own quirks,
which is what the per-platform backends below deal with.

Wire format (PC -> Flipper) - [type:1][len:2 LE][payload:len]
    0x01 META: [state:1][pos:2 LE][dur:2 LE][title utf8]0[artist utf8]0
    0x02 ART : [w:1][h:1][1-bit XBM rows, LSB-first per byte, (w+7)/8 per row]
    0x03 VOL : [volume:1]  0..100
Flipper -> PC: one command byte each (CMD_TRANSPORT / CMD_VOLUME below)
    p play/pause   n next   b previous   - seek back   + seek forward
    u volume up    d volume down
"""

import argparse
import atexit
import asyncio
import concurrent.futures
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import io
import json
import os
import shutil
import socket
import queue
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
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
# Seconds per seek tap. YouTube Music's own seek controls - the back/forward buttons and
# the arrow keys - step 5 s, and that is what the device should match, so every
# path moves exactly this: MPRIS on Linux, SMTC on Windows, the browser
# extension, and the keystroke path below.
SEEK_STEP = 5
# The one path that cannot ask for a distance is Firefox on Windows: it has no
# seek API, so the bridge types a YouTube Music shortcut at the tab. Its h/l
# shortcuts step 10 s, but Shift+H / Shift+L step 1 s - so that path taps the
# 1-second shortcut SEEK_STEP times and lands on the same 5 s as everywhere
# else, from the same constant.
YT_SEEK_TAP_S = 1
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
LIKE_REFRESH_S = 1.0   # how often the like button is re-read (one localhost round trip)
LIKE_PORT = 9222       # Firefox --remote-debugging-port default
EXTENSION_PORT = 8765  # localhost port the browser extension posts the like state to

FRAME_META, FRAME_ART, FRAME_VOL, FRAME_TOAST, FRAME_LIKE = 0x01, 0x02, 0x03, 0x04, 0x05
LIKE_NO, LIKE_YES, LIKE_UNKNOWN = 0, 1, 2
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


# CREATE_NO_WINDOW. A GUI-subsystem process (pythonw.exe, which is what the logon
# shortcut runs) has no console, so spawning a console app - yt-dlp is one - makes
# Windows allocate a fresh console *window*: a terminal flashes up on every new
# track, which looks like the app is doing something wrong. Suppress it.
CREATE_NO_WINDOW = 0x08000000


def hidden_process_kwargs():
    """subprocess kwargs that keep a console child from flashing a window."""
    if not IS_WINDOWS:
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0                      # SW_HIDE
    return {"creationflags": CREATE_NO_WINDOW, "startupinfo": startup}


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


# ============================================ like state (browser extension)
class BrowserLink:
    """The little localhost server the browser extension talks to.

    Only the page knows whether the current track is liked, and asking it through
    an extension is the least fragile way: unlike the debug port it does not care
    how the browser was started, the same code runs in Chrome, Edge and Firefox,
    and it also lets the bridge press the real button instead of typing a
    shortcut at a window.

    Two directions, both over plain HTTP on the loopback interface:

        POST /state   extension -> bridge, whenever the like button changes
                      (and every couple of seconds as a heartbeat)
        GET  /wait    extension long-polls; the bridge answers with a command
                      ("toggle") when the Flipper's like button is pressed

    Long-polling instead of a websocket keeps this to the standard library and
    survives the extension being suspended and restarted.
    """

    def __init__(self, port=8765):
        self.port = int(port)
        # One record per reporting tab. With YouTube Music open in two browsers
        # (or two tabs) the bridge must follow the one the user is actually
        # using, not whichever posted last.
        self.clients = {}
        self.active_id = None
        self.liked = None
        self.title = ""
        self.artist = ""
        self.playing = None
        self.updated = 0.0          # monotonic, when /state last arrived
        self.requests = 0           # how many reports arrived since start
        self.changes = 0            # how many times "liked" actually changed
        self.polls = 0              # long-poll requests: proof the extension listens
        self._commands = queue.Queue()
        self._lock = threading.Lock()
        self.wait_timeout = 20.0    # how long a poll may block before it answers empty
        self._server = None
        self._thread = None
        self.remote_base = ""
        self.error = ""

    # ---- state
    def update(self, liked, title="", artist="", playing=None, client=None,
               visible=None, focused=None, last_active=None, diag=None,
               worker=""):
        """Record one tab's report and re-pick which tab the bridge follows."""
        client_id = client or "default"
        with self._lock:
            record = self.clients.setdefault(client_id, {})
            record.update(liked=liked, title=title or "", artist=artist or "",
                          playing=playing, visible=visible, focused=focused,
                          lastActive=last_active, diag=diag,
                          worker=worker or record.get("worker", ""),
                          updated=time.monotonic())
            self.requests += 1
        return self._reselect()

    def _select(self):
        """The tab to follow: focused and visible first, then most recently used.

        `lastActive` is when the user last touched that tab (click, key, focus);
        it is what makes "the tab I am looking at" win over a background one that
        happens to post later.
        """
        live = [(client_id, record) for client_id, record in self.clients.items()
                if record.get("updated") and
                time.monotonic() - record["updated"] < 6.0]
        if not live:
            return None

        def rank(item):
            record = item[1]
            return (bool(record.get("focused") and record.get("visible")),
                    bool(record.get("visible")),
                    record.get("lastActive") or 0,
                    record.get("updated") or 0)

        return max(live, key=rank)

    def _reselect(self):
        with self._lock:
            chosen = self._select()
            if chosen is None:
                return False
            client_id, record = chosen
            liked = record.get("liked")
            changed = liked != self.liked
            self.active_id = client_id
            self.liked = liked
            self.title = record.get("title", "")
            self.artist = record.get("artist", "")
            self.playing = record.get("playing")
            self.updated = record.get("updated", 0.0)
            if changed:
                self.changes += 1
            return changed

    def diag(self):
        """What the page last said about its like button (None if it never did)."""
        with self._lock:
            record = self.clients.get(self.active_id or "", {})
            return record.get("diag")

    def fresh(self, max_age=6.0):
        """True while at least one tab is reporting (heartbeat every 2 s)."""
        with self._lock:
            return self._select() is not None

    def request_activate(self, client=None):
        """Ask the extension to bring the reporting tab to the front for a keystroke."""
        target = client or self.active_id
        with self._lock:
            worker = self.clients.get(target or "", {}).get("worker", "")
        self._commands.put({"cmd": "activate", "client": target, "worker": worker})

    def request_restore(self, client=None):
        """Put the tab that was in front back where it was."""
        target = client or self.active_id
        with self._lock:
            worker = self.clients.get(target or "", {}).get("worker", "")
        self._commands.put({"cmd": "restore", "client": target, "worker": worker})

    def state(self):
        with self._lock:
            record = self.clients.get(self.active_id, {}) if self.active_id else {}
            return {"liked": self.liked, "title": self.title, "artist": self.artist,
                    "playing": self.playing, "reports": self.requests,
                    "changes": self.changes, "polls": self.polls,
                    "clients": len(self.clients), "client": self.active_id,
                    "focused": record.get("focused"), "visible": record.get("visible"),
                    "diag": record.get("diag"),
                    "fresh": self._select() is not None}

    # ---- commands
    def request_toggle(self, title="", client=None, worker=""):
        if not worker:
            target = client or self.active_id
            with self._lock:
                worker = self.clients.get(target or "", {}).get("worker", "")
        if self.remote_base:
            try:
                request = urllib.request.Request(
                    f"{self.remote_base}/toggle",
                    data=json.dumps({"title": title, "client": client}).encode(),
                    method="POST")
                urllib.request.urlopen(request, timeout=5).read()
            except Exception as exc:
                print("  toggle: could not reach the running bridge:", exc)
            return
        self._commands.put({"cmd": "toggle", "title": title,
                            "client": client or self.active_id,
                            "worker": worker})

    def _answers(self):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/state", timeout=1.5) as reply:
                data = json.load(reply)
            return "liked" in data and "polls" in data
        except Exception:
            return False

    def _mirror(self):
        """Follow the running bridge's state while this process only helps out."""
        while True:
            try:
                with urllib.request.urlopen(f"{self.remote_base}/state", timeout=3) as reply:
                    data = json.load(reply)
                if data.get("fresh"):
                    # Go through update() so the mirrored value becomes a client
                    # record like any other; fresh() and the selection logic both
                    # work off records, not off bare attributes.
                    self.update(data.get("liked"), data.get("title", ""),
                                data.get("artist", ""), data.get("playing"),
                                client=data.get("client") or "remote",
                                visible=data.get("visible"),
                                focused=data.get("focused"),
                                diag=data.get("diag"))
                    with self._lock:
                        self.changes = max(self.changes, data.get("changes") or 0)
            except Exception:
                pass
            time.sleep(1.0)

    # ---- server
    def start(self):
        if self._server is not None:
            return True
        link = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def _reply(self, code, payload=b"{}", timeout=0):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                # Deliberately no CORS headers. The extension reaches this port
                # through its host_permissions, so it needs none - and
                # `Access-Control-Allow-Origin: *` plus the private-network
                # opt-in is exactly what let ANY site you visit POST /toggle
                # (a click, and on the fallback path a real Enter at your
                # focused window) or fake the track on the Flipper.
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b"{}"
                if self.path.startswith("/toggle"):
                    # Test hook: the same command the Flipper's like button sends,
                    # so the round trip can be checked with curl and no hardware.
                    title = client = ""
                    try:
                        payload = json.loads(body or b"{}") or {}
                        title = payload.get("title", "")
                        client = payload.get("client") or (link.active_id or "")
                    except ValueError:
                        pass
                    link.request_toggle(title, client)
                    self._reply(200, b'{"queued":"toggle"}')
                    return
                try:
                    data = json.loads(body or b"{}")
                except ValueError:
                    self._reply(400)
                    return
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                changed = link.update(
                    data.get("liked"), data.get("title", ""), data.get("artist", ""),
                    data.get("playing"), client=data.get("client"),
                    visible=data.get("visible"), focused=data.get("focused"),
                    last_active=data.get("lastActive"), diag=data.get("diag"),
                    worker=query.get("worker", [""])[0])
                if changed:
                    print(f"  <- browser: liked={link.liked} ({link.title[:30]!r}) "
                          f"[{link.active_id}{', focused' if data.get('focused') else ''}]")
                self._reply(200, b'{"ok":true}')

            def do_GET(self):
                if self.path.startswith("/wait"):
                    with link._lock:
                        link.polls += 1
                    query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    worker = query.get("worker", [""])[0]
                    browser = query.get("browser", [""])[0]
                    started = time.monotonic()
                    deadline = started + link.wait_timeout
                    command = {"cmd": None}
                    while True:
                        left = deadline - time.monotonic()
                        if left <= 0:
                            break
                        try:
                            candidate = link._commands.get(timeout=left)
                        except queue.Empty:
                            break
                        wanted = candidate.get("worker")
                        if wanted and worker and wanted != worker:
                            # Another install (say the Firefox one) asked first.
                            # Handing it over would click in the wrong browser, so
                            # put it back for the worker that owns the tab.
                            link._commands.put(candidate)
                            time.sleep(0.05)
                            continue
                        command = candidate
                        break
                    if command.get("cmd"):
                        wanted = command.get("worker")
                        note = ""
                        if wanted and worker and wanted != worker:
                            note = f" (WANTED worker {wanted}!)"
                        print(f"  <- extension picked up "
                              f"{command['cmd']} after {time.monotonic() - started:.1f}s"
                              f" by worker {worker or '?'}{'/' + browser if browser else ''}{note}")
                    self._reply(200, json.dumps(command).encode())
                else:
                    self._reply(200, json.dumps(link.state()).encode())

            def handle_one_request(self):
                # The extension aborts its 20 s poll when the worker restarts, so
                # a reset here is routine; the stock handler prints a traceback
                # for it and fills the log.
                try:
                    super().handle_one_request()
                except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                    self.close_connection = True

            def log_message(self, format, *args):
                pass                        # a poll every second is not news

        # Ask before binding: on Windows SO_REUSEADDR lets a second process bind
        # the same port, which would quietly split the extension's reports between
        # two servers. A live answer here means the real bridge owns the port.
        if self._answers():
            self.remote_base = f"http://127.0.0.1:{self.port}"
            threading.Thread(target=self._mirror, daemon=True,
                             name="ytm-browser-link-mirror").start()
            print(f"Using the running bridge on port {self.port} for the like state")
            return True

        class Server(ThreadingHTTPServer):
            # Same reason as handle_one_request: a client hanging up is routine.
            def handle_error(self, request, client_address):
                import sys as _sys
                exc = _sys.exc_info()[1]
                if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                                    BrokenPipeError)):
                    return
                super().handle_error(request, client_address)

        try:
            server = Server(("127.0.0.1", self.port), Handler)
        except OSError as exc:
            # The port is taken - almost always by an already-running bridge. Then
            # this process is a helper (--dump, --cmd) and asks *that* one instead
            # of hosting: the extension can only report to a single owner.
            self.error = str(exc)
            self.remote_base = f"http://127.0.0.1:{self.port}"
            threading.Thread(target=self._mirror, daemon=True,
                             name="ytm-browser-link-mirror").start()
            print(f"Using the running bridge on port {self.port} for the like state")
            return True
        server.daemon_threads = True
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True,
                                        name="ytm-browser-link")
        self._thread.start()
        return True

    def close(self):
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
        self._server = None


def link_toggle(link, title="", timeout=2.5, attempts=2, client=None):
    """Ask the extension to press like; returns the new state, or None.

    Waits for the *like state* to move, not for any report: the page heartbeats
    every couple of seconds regardless, and a fresh service worker that has not
    attached its content script yet swallows the first command outright - the
    message simply has nobody to land on. Hence the retry, which costs nothing
    when the first click worked (the state moved, so it returns at once).

    None means "no extension is reporting", not "failed": the caller then falls
    back to the debug port or to typing at the window.
    """
    if link is None or not link.fresh():
        return None
    for _ in range(max(1, attempts)):
        before = link.changes
        link.request_toggle(title, client)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(0.05)
            if link.changes != before:
                return link.liked
    return None


# =================================================== like state (Firefox BiDi)
class FirefoxDebug:
    """Reads and toggles YouTube Music's like button through Firefox's remote agent.

    Neither MPRIS nor the Windows media session exposes "is this track liked", so
    the state has to come from the page. Firefox started with
    `--remote-debugging-port=<port>` serves a WebDriver BiDi agent, and BiDi can
    run script in a tab: `script.evaluate` reads the like button's aria-pressed,
    and clicking that button is what the site itself does.

    Firefox does *not* speak Chrome's CDP any more (158 answers /json/list with
    404) and the socket is the handshake endpoint itself: GET /session upgrades,
    then session.new / browsingContext.getTree / script.evaluate travel over it.
    Only one BiDi session may exist per browser, so the connection is kept open
    rather than opened per poll.
    """

    LIKE = "ytmusic-like-button-renderer #button-shape-like button"
    STATE_EXPR = ("(() => { const b = document.querySelector('" + LIKE + "');"
                  " return b ? b.getAttribute('aria-pressed') : null; })()")
    CLICK_EXPR = "document.querySelector('" + LIKE + "')?.click(), true"
    TITLE_EXPR = "document.title"

    def __init__(self, port):
        self.port = int(port)
        # One socket, two callers: the poll loop's background refresh and a
        # button press. Firefox allows a single BiDi session, so they serialise.
        self._lock = threading.RLock()
        self.ws = None
        self.counter = 0
        self.context = None
        self.context_at = 0.0
        self.error = ""
        self.reported = ""

    # ---- protocol plumbing
    def _call(self, method, params=None, timeout=8.0):
        socket = self.ws
        if socket is None:
            raise RuntimeError("not connected")
        self.counter += 1
        message_id = self.counter
        socket.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(method)
            socket.settimeout(left)
            message = json.loads(socket.recv())
            if message.get("id") != message_id:
                continue                    # an event, not our reply
            if message.get("type") == "error":
                raise RuntimeError(f"{message.get('error')}: {message.get('message')}")
            return message.get("result", {})

    def _evaluate(self, context, expression):
        result = self._call("script.evaluate", {
            "expression": expression,
            "target": {"context": context},
            "awaitPromise": True,
            "resultOwnership": "none",
        })
        remote = result.get("result", {})
        return remote.get("value")

    def connect(self):
        with self._lock:
            return self._connect()

    def _connect(self):
        if self.ws is not None:
            return True
        try:
            import websocket
        except ImportError:
            self.error = "websocket-client is not installed"
            return False
        # Cheap check first: a closed port is the normal case when the extension
        # is the one supplying the like state, and it should not read as an error.
        try:
            socket.create_connection(("127.0.0.1", self.port), timeout=0.3).close()
        except OSError:
            self.error = f"no debug port on {self.port}"
            return False
        try:
            self.ws = websocket.create_connection(
                f"ws://127.0.0.1:{self.port}/session", timeout=5,
                suppress_origin=True, header=["Sec-WebSocket-Protocol: WebDriver-BiDi"])
            self._call("session.new", {"capabilities": {}})
            self.context = None
            self.error = ""
            return True
            atexit.register(self.end)
        except Exception as exc:              # port closed, another session, ...
            self.error = str(exc) or exc.__class__.__name__
            if "active sessions" in self.error:
                # Firefox allows one BiDi session at a time. Either the bridge is
                # already holding it (normal) or a crashed client left it stuck.
                self.error = ("another client holds Firefox's debug session "
                              "(stop the bridge, or restart Firefox)")
            self.close()
            return False

    def end(self):
        """End the BiDi session before dropping the socket.

        Firefox allows one BiDi session per browser and does not reliably notice
        a client that just vanished, so a stale session would otherwise block the
        next run with "Maximum number of active sessions".
        """
        with self._lock:
            if self.ws is not None:
                try:
                    self._call("session.end", timeout=3)
                except Exception:
                    pass
            self.close()

    def close(self):
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
        self.ws = None
        self.context = None

    def _note(self, message):
        """Say why the like state is unavailable once, not on every poll."""
        if message != self.reported:
            self.reported = message
            print("  like state:", message)

    # ---- the page
    def _context_for(self, title):
        """The browsing context showing YouTube Music (rechecked every few seconds)."""
        if self.context and time.monotonic() - self.context_at < 5.0:
            return self.context
        tree = self._call("browsingContext.getTree")
        pages = [c for c in tree.get("contexts", [])
                 if "music.youtube.com" in (c.get("url") or "")]
        if not pages:
            self.context = None
            return None
        # several YT Music tabs are possible: prefer the one playing this track
        for page in pages:
            try:
                if title and title.strip().lower()[:24] in (self._evaluate(page["context"], self.TITLE_EXPR) or "").lower():
                    self.context, self.context_at = page["context"], time.monotonic()
                    return self.context
            except Exception:
                continue
        self.context, self.context_at = pages[0]["context"], time.monotonic()
        return self.context

    def like_state(self, title=""):
        """True / False / None (unknown) for the current track."""
        with self._lock:
            return self._like_state(title)

    def _like_state(self, title=""):
        if not self.connect():
            self._note(f"unavailable ({self.error})")
            return None
        try:
            context = self._context_for(title)
            if context is None:
                self._note("no YouTube Music tab found")
                return None
            pressed = self._evaluate(context, self.STATE_EXPR)
            if pressed is None:
                self._note("like button not found in the tab")
                return None
            self.reported = ""
            return str(pressed).lower() == "true"
        except Exception as exc:
            self.error = str(exc) or exc.__class__.__name__
            self.close()
            self._note(f"unavailable ({self.error})")
            return None

    def toggle(self, title=""):
        """Click the like button; returns the state afterwards, or None."""
        with self._lock:
            return self._toggle(title)

    def _toggle(self, title=""):
        if not self.connect():
            return None
        try:
            context = self._context_for(title)
            if context is None:
                return None
            self._evaluate(context, self.CLICK_EXPR)
            time.sleep(0.6)                 # let the site update the button
            self.context_at = 0.0           # force a fresh context lookup
            return self._like_state(title)
        except Exception as exc:
            self.error = str(exc) or exc.__class__.__name__
            self.close()
            return None


# ================================================================ Linux: MPRIS
class MprisBackend:
    """Linux/BSD media sessions through playerctl (MPRIS over D-Bus)."""

    name = "mpris"
    seek_step = SEEK_STEP

    def __init__(self, player=None, link=None):
        self.player = player
        self.link = link

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
            sys.exit("playerctl not found - install it to read the media session")
        return [p for p in out.stdout.splitlines() if p.strip()]

    def close(self):
        if self.link is not None:
            self.link.close()

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
            # MPRIS carries no like state: the browser extension or the Firefox
            # debug port is what knows it.
            "liked": self.link.liked if (self.link and self.link.fresh()) else None,
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
        focused window - there is no way to address a browser tab from outside.
        A window whose *title* shows YouTube Music has it as its active tab, so
        that is what gets checked: firing the keystroke blind would type a '+'
        into whatever else happened to have focus.
        """
        state = link_toggle(self.link)
        if state is not None:
            return True, ("Liked" if state else "Unliked"), state

        vkkey = Path.home() / ".local/share/ytm-remote/bin/vkkey"
        if not vkkey.exists():
            return False, "Like: vkkey missing", None

        windows = sway_windows()
        if not windows:
            return False, "Like: cannot check focus", None

        previous = windows[0][0] if windows[0][2] else None
        target = next((w for w in windows if "youtube music" in w[1].lower()), None)
        if target is None:
            return False, "Open the YT Music tab", None

        moved = False
        try:
            if not target[2]:
                swaymsg(f"[con_id={target[0]}] focus")
                moved = True
                time.sleep(0.35)      # let the focus change land before typing
            subprocess.run([str(vkkey), "-m", "1", "13"], capture_output=True, timeout=5)
        except Exception as e:
            return False, f"Like failed: {e}", None
        finally:
            if moved and previous:
                time.sleep(0.15)
                swaymsg(f"[con_id={previous}] focus")   # give focus back
        return True, "Like toggled", None


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
    fresh art file when a track starts, so that file's mtime dates the track -
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

    VK_SHIFT, VK_OEM_PLUS, VK_RETURN = 0x10, 0xBB, 0x0D
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

            def session_for(pid, exe):
                """This app's audio session.

                The pid alone is not enough: Chromium browsers play audio from a
                *different* process than the one owning the window (Brave's window
                was 1384 while its audio session was 7988), so the window's pid
                finds nothing and the volume keys silently do nothing. Match the
                process name as well, preferring a session that is actually
                playing.
                """
                now = time.monotonic()
                cached = cache["session"]
                if cached is not None and now - cache["at"] < 2.0:
                    try:
                        same_pid = pid is not None and cached.ProcessId == pid
                        same_proc = (exe and cached.Process
                                     and cached.Process.name().lower() == exe.lower())
                        if same_pid or same_proc:
                            return cached
                    except Exception:
                        cache["session"] = None

                exact = None
                family = None
                playing = None
                for session in AudioUtilities.GetAllSessions():
                    try:
                        if session.Process is None:
                            continue
                        name = session.Process.name().lower()
                        active = getattr(session, "State", 0) == 1
                        if pid is not None and session.ProcessId == pid:
                            exact = session
                        elif exe and name == exe.lower():
                            family = family or session
                            if active and playing is None:
                                playing = session
                        elif pid is None and exe is None and active and playing is None:
                            playing = session      # no hint: first active app
                    except Exception:
                        continue
                best = exact or playing or family
                cache["at"], cache["session"] = now, best
                return best

            while True:
                pid, exe, sign, reply = self._queue.get()
                try:
                    session = session_for(pid, exe)
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

        def _call(self, pid, exe=None, sign=None, timeout=1.5):
            if not self.ready:
                return None
            reply = concurrent.futures.Future()
            self._queue.put((pid, exe, sign, reply))
            try:
                return reply.result(timeout=timeout)
            except Exception:
                return None

        def get(self, pid, exe=None):
            return self._call(pid, exe)

        def nudge(self, pid, sign, exe=None):
            return self._call(pid, exe, sign)

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
        seek_step = SEEK_STEP

        def __init__(self, hint=None, firefox_port=None, link=None):
            self.link = link
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
            self._exe = None
            self._warned_missing = False
            self._sink = None
            self._sink_cache = (0.0, None)
            self._title = ""
            self._debug = FirefoxDebug(firefox_port) if firefox_port else None
            self._liked = None
            self._liked_at = 0.0
            self._liked_task = None

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
                rows.append(f"{self.label(session):<12} {props.title or '?'} - "
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
            # the 1-bit bitmap is not - so the bytes are kept and the frame is
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

            self._pid, self._exe = self._process_for(session)
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
                "liked": self.link.liked if (self.link and self.link.fresh())
                         else await self._liked_state(title),
            }

        async def _liked_state(self, title):
            """True / False / None, refreshed in a worker thread.

            The BiDi round trip blocks on a socket, so it runs off the event loop
            and read() simply returns the last answer; at 20 Hz a blocking call
            here would stall the metadata stream.
            """
            self._title = title
            if self._debug is None:
                return None
            now = time.monotonic()
            if self._liked_task is None and now - self._liked_at >= LIKE_REFRESH_S:
                self._liked_at = now
                loop = asyncio.get_running_loop()
                task = asyncio.ensure_future(
                    loop.run_in_executor(None, self._debug.like_state, title))
                self._liked_task = task
                task.add_done_callback(self._liked_done)
            return self._liked

        def _liked_done(self, task):
            self._liked_task = None
            try:
                self._liked = task.result()
            except Exception:
                self._liked = None

        def _volume_level(self):
            """Current app volume, sampled twice a second.

            Reading it means a round trip to the volume thread, so it is cached:
            the level only changes when someone moves a slider or presses a key.
            """
            since, level = self._volume_cache
            if time.monotonic() - since < 0.5:
                return level
            level = self._volume.get(self._pid, self._exe)
            self._volume_cache = (time.monotonic(), level)
            return level

        def _timeline(self, session, key, playing):
            """(position, duration) in seconds, or (0, 0) when the player has none.

            Firefox's SMTC timeline is a snapshot from whenever YouTube Music last
            published it: the position sits at zero and the total can still be the
            *previous* track's. So a timeline is only believed once its position
            has been seen to move during this track; otherwise the bridge falls
            back to what it does on Linux - count elapsed time itself and look the
            length up with yt-dlp - rather than show a wrong number.
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

        def close(self):
            """Release the debug session and the extension port."""
            if self._debug is not None:
                self._debug.end()
            if self.link is not None:
                self.link.close()

        def _process_for(self, session):
            """(pid, exe) of the app behind the session, for the volume controls."""
            row = self.window_for(getattr(session, "source_app_user_model_id", "") or "")
            return (row.pid, row.process) if row else (None, None)

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
            """+/-SEEK_STEP on every path, including the keystroke one."""
            session = self.session
            if session is None:
                return
            if self._is_firefox(session):
                row = self._yt_window() or self.window_for(
                    getattr(session, "source_app_user_model_id", "") or "")
                if row is None:
                    print("  !! seek: no Firefox window to address")
                    return
                # No seek API in Firefox's media session, so tap the 1-second
                # shortcut until the distance matches what every other platform
                # moves. Shift+L forward, Shift+H back (plain l/h would jump
                # 10 s and disagree with the device).
                chord_key = (VK_SHIFT, VK_L if direction > 0 else VK_H)
                taps = max(1, int(round(self.seek_step / YT_SEEK_TAP_S)))
                if self._type_at(row, [chord_key] * taps):
                    print(f"  <- seek {'+' if direction > 0 else '-'}"
                          f"{taps * YT_SEEK_TAP_S}s -> {taps} x YT Music "
                          f"{'Shift+L' if direction > 0 else 'Shift+H'} ({YT_SEEK_TAP_S}s each)")
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
            level = self._volume.nudge(self._pid, sign, self._exe)
            if level is None:
                print("  !! volume: no audio session for this player")
            return level

        def _yt_window(self):
            """The browser window that has YouTube Music as its active tab.

            Typing at a window only reaches the *active* tab, so the window is
            identified by its title: YouTube Music puts the site name there, and
            while a track plays the tab title carries the track too.

            The player's own browser is tried first (Brave, Chrome, Firefox...),
            because a keystroke aimed at the wrong application types into it.
            """
            rows = self.windows()
            exe = (self._exe or "").lower()
            preferred = [row for row in rows if exe and row.process.lower() == exe]
            rest = [row for row in rows if row not in preferred]
            for row in preferred + rest:
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

        def _await_like(self, before, timeout=2.5):
            """Wait for the page to report a new like state; None if it never did."""
            if self.link is None:
                return None
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                state = self.link.liked
                if state is not None and state != before:
                    return state
                time.sleep(0.1)
            return None

        def like(self):
            """Toggle like: the button itself when the debug port is open, else keys.

            Clicking through BiDi does not need the tab to be on screen, so it
            works while the bridge is in the background - the keystroke path can
            only type at a focused window.
            """
            state = link_toggle(self.link, self._title, attempts=1)
            if state is not None:
                self._liked, self._liked_at = state, 0.0
                return True, ("Liked" if state else "Unliked"), state
            first = (self.link.diag() or {}) if self.link is not None else {}
            if not (first.get("click") or {}).get("buttonFocused"):
                # No verdict from the page yet (a fresh worker has not attached its
                # content script), so one retry is worth 2.5 s - but not once the
                # page has already told us its own click does nothing.
                state = link_toggle(self.link, self._title, attempts=1)
                if state is not None:
                    self._liked, self._liked_at = state, 0.0
                    return True, ("Liked" if state else "Unliked"), state
            # Chromium drops untrusted events, so the page's own click can land
            # and do nothing. It leaves the like button focused for exactly this
            # case: a real Enter keypress at the browser window is trusted input,
            # and a focused button treats Enter as a click.
            diag = self.link.diag() if self.link is not None else None
            click = (diag or {}).get("click") or {}
            if click.get("buttonFocused"):
                before = self.link.liked if self.link is not None else None
                activated = False
                if not click.get("windowFocused") and self.link is not None:
                    # The page is behind another tab, so a keystroke would land in
                    # that one. Let the extension bring its own tab forward first;
                    # it puts the previous tab back afterwards.
                    self.link.request_activate()
                    deadline = time.monotonic() + 2.0
                    while time.monotonic() < deadline:
                        current = (self.link.diag() or {}).get("click") or {}
                        if current.get("windowFocused"):
                            break
                        time.sleep(0.1)
                    activated = True
                row = self._yt_window()
                if row is not None and self._type_at(row, [VK_RETURN]):
                    state = self._await_like(before, timeout=2.5)
                    if activated and self.link is not None:
                        self.link.request_restore()
                    if state is not None:
                        self._liked, self._liked_at = state, 0.0
                        return True, ("Liked" if state else "Unliked"), state
                elif activated and self.link is not None:
                    self.link.request_restore()
            if self.link is not None and self.link.fresh():
                # Nothing else can reach this page, so report what happened
                # instead of pointing at a tab that is already open.
                return False, "Browser did not click", None
            if self.link is not None and self.link.polls > 0:
                # The worker is polling, so the extension is installed - but no
                # tab is reporting, which means its content script is not in the
                # open page (reloading an extension does not re-inject).
                return False, "Reload the YT Music tab", None

            if self._debug is not None:
                state = self._debug.toggle(self._title)
                if state is not None:
                    self._liked, self._liked_at = state, 0.0
                    return True, ("Liked" if state else "Unliked"), state
            row = self._yt_window()
            if row is None:
                return False, "Open the YT Music tab", None
            if not self._type_at(row, [(VK_SHIFT, VK_OEM_PLUS)]):
                return False, "Like: cannot focus", None
            return True, "Like toggled", None


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


# --- What the device can actually draw ---------------------------------------
# The Flipper's fonts are Latin-only bitmap faces (the SDK has FontPrimary,
# FontSecondary, FontKeyboard and FontBigNumbers, nothing else), and the app
# draws text byte by byte, so a Korean or Japanese title arrives on screen as blank space.
# Convert on this side, where a dictionary can live, and keep the raw spelling
# for everything that is not the display: the cache key, the yt-dlp search and
# the log all still use the original.
_HANGUL_INITIAL = ("g", "kk", "n", "d", "tt", "r", "m", "b", "pp", "s", "ss", "",
                   "j", "jj", "ch", "k", "t", "p", "h")
_HANGUL_MEDIAL = ("a", "ae", "ya", "yae", "eo", "e", "yeo", "ye", "o", "wa", "wae",
                  "oe", "yo", "u", "wo", "we", "wi", "yu", "eu", "ui", "i")
_HANGUL_FINAL = ("", "k", "k", "k", "n", "n", "n", "t", "l", "k", "m", "p", "l", "l",
                 "p", "l", "m", "p", "p", "t", "t", "ng", "t", "t", "k", "t", "p", "t")
HANGUL_FIRST, HANGUL_LAST = 0xAC00, 0xD7A3
KANA_RANGES = ((0x3040, 0x30FF), (0x31F0, 0x31FF), (0xFF66, 0xFF9D))
HAN_RANGES = ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF), (0xFF00, 0xFFEF))


def _in_ranges(ch, ranges):
    return any(low <= ord(ch) <= high for low, high in ranges)


def hangul_to_latin(text):
    """Romanise Hangul syllables (simplified Revised Romanization).

    Hangul decomposes mechanically into initial/medial/final, so this needs no
    dictionary. One assimilation rule is enough for it to read correctly in
    practice: a final -l doubles into a following -r onset, so the Korean for aluminium comes out as
    alluminyum rather than alruminyum. Mirrored in extension/remote.js.
    """
    out, previous_l = [], False
    for ch in text:
        code = ord(ch)
        if not HANGUL_FIRST <= code <= HANGUL_LAST:
            out.append(ch)
            previous_l = False
            continue
        offset = code - HANGUL_FIRST
        initial = _HANGUL_INITIAL[offset // 588]
        medial = _HANGUL_MEDIAL[(offset % 588) // 28]
        final = _HANGUL_FINAL[offset % 28]
        if previous_l and initial == "r":
            initial = "l"
        out.append(initial + medial + final)
        previous_l = final == "l"
    return "".join(out)


_OPTIONAL_MODULES = {}


def _optional(module):
    """Import an optional romanisation dictionary once, or None if absent."""
    if module not in _OPTIONAL_MODULES:
        try:
            _OPTIONAL_MODULES[module] = importlib.import_module(module)
        except ImportError:
            _OPTIONAL_MODULES[module] = None
    return _OPTIONAL_MODULES[module]


def _cjk_to_latin(text):
    """Japanese/Chinese -> Latin, when the optional dictionaries are installed.

    Kana means Japanese (pykakasi); hanzi with no kana means Chinese (pypinyin).
    Without the dictionary the text is returned unchanged and the caller marks
    it, which is the whole story on the browser side - the extension cannot
    bundle megabytes of readings, so it always marks these.
    """
    if any(_in_ranges(c, KANA_RANGES) for c in text):
        kakasi = _optional("pykakasi")
        if kakasi is None:
            return text
        try:
            return "".join(p.get("hepburn") or p.get("orig", "")
                           for p in kakasi.kakasi().convert(text))
        except Exception:
            return text
    if any(_in_ranges(c, HAN_RANGES) for c in text):
        pinyin = _optional("pypinyin")
        if pinyin is None:
            return text
        try:
            return " ".join(pinyin.lazy_pinyin(text))
        except Exception:
            return text
    return text


def device_text(text):
    """Make a string drawable by the Flipper, whose fonts are Latin-only.

    Folds accents to their base letters, romanises what can be romanised, and
    replaces whatever is left with '?' so a title never silently renders as
    blank space. Called on the way to the device only.
    """
    if not text:
        return ""
    if any(ord(c) > 0x7F for c in text):
        text = _cjk_to_latin(hangul_to_latin(text))
        text = unicodedata.normalize("NFKD", text)
        text = "".join(c for c in text if not unicodedata.combining(c))
        text = "".join(c if 0x20 <= ord(c) < 0x7F else "?" for c in text)
    return text.strip()


def device_meta(m):
    """The meta dict as the device sees it: text its fonts can actually draw.

    Everything else keeps the original spelling - the cache key, the yt-dlp
    search and the log line all read the dict that came in.
    """
    return dict(m, title=device_text(m["title"]), artist=device_text(m["artist"]))


def meta_frame(m, with_text=True):
    """state/pos/dur, plus title/artist unless the caller says they are unchanged.

    A clock-only frame is 8 bytes and fits in one BLE write; carrying the text
    makes it ~80 bytes, which is three or four writes at MTU 23 - that was most
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


def toast_frame(text, icon=0):
    """One short line for the device to show for a moment.

    The first payload byte is an icon id (0 none, 1 heart, 2 broken heart) that
    the device draws before the text, so "Liked" can arrive with a heart beside
    it - the LCD fonts have no such glyph.
    """
    body = text.encode("utf-8", "replace")[:TOAST_MAX - 1]
    return frame(FRAME_TOAST, bytes([max(0, min(2, int(icon)))]) + body)


def art_frame(art):
    w, h, data = art_to_xbm(art)
    return frame(FRAME_ART, bytes([w, h]) + data)


def like_frame(state):
    """0 = not liked, 1 = liked, 2 = unknown (the device draws nothing)."""
    return frame(FRAME_LIKE, bytes([max(LIKE_NO, min(LIKE_UNKNOWN, int(state)))]))


def vol_frame(level):
    return frame(FRAME_VOL, bytes([max(0, min(100, int(level)))]))


# ---------------------------------------------------------------- BLE
async def send_toast_once(name_hint, text):
    """Connect, send one toast, disconnect - a way to check the toast UI."""
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
        blob = toast_frame(device_text(text))
        await client.write_gatt_char(write_char, blob, response=False)
        print(f"toast sent ({len(blob)} bytes): {text!r}")
        await asyncio.sleep(1.0)


async def pair_flipper(name_hint, address_hint=None):
    """Pair with the Flipper once (Windows needs a bond for the serial profile)."""
    dev = address_hint or (await find_flipper(name_hint)) or load_known_address()
    if not dev:
        sys.exit("no device found - is the Flipper on and advertising?")
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
    empty scan does not mean the Flipper is gone - connecting straight to the
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
    connecting again just hangs on the dead link - dropping it first is what lets
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
        self.last_liked = None
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
                **hidden_process_kwargs(),
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
            # - better to start at 0 than to seed a wildly wrong position. The
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

    async def toast(self, text, icon=0):
        """Show a short message on the device (the like outcome, say)."""
        if self.write_char is None:
            return
        await self.write(toast_frame(device_text(text), icon))

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
                ok, message, state = await loop.run_in_executor(None, self.backend.like)
                icon = 1 if state is True else (2 if state is False else 0)
                print(f"  <- like: {message}" + ("" if ok else " (nothing typed)"))
                await self.toast(message, icon)

    async def push(self, m):
        if self.write_char is None:
            return
        now = time.monotonic()

        if now - self.last_sent >= 1.0 / self.hz:
            # text goes out only when it changes: the device keeps the last one,
            # so the per-tick frame stays 8 bytes and lands in a single write
            text = (m["title"], m["artist"])
            # The device's fonts are Latin-only, and the frame takes its text
            # through device_meta(), so what leaves here is drawable while the
            # raw spelling still keys the cache, the yt-dlp lookup and the log.
            shown = device_meta(m)
            # Until the device advertises that it keeps its own text, repeat it
            # every frame: an older build blanks the title on a text-less one.
            await self.write(meta_frame(
                shown, with_text=(text != self.last_text) or not self.device_caps))
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

        liked = LIKE_UNKNOWN if m.get("liked") is None else (
            LIKE_YES if m["liked"] else LIKE_NO)
        # Also resent when the track changes: the state lives per track, and one
        # dropped frame would otherwise leave the heart wrong for the whole song.
        like_key = (liked, m["title"])
        if like_key != self.last_liked:
            await self.write(like_frame(liked))
            self.last_liked = like_key
            if liked != LIKE_UNKNOWN:
                print(f"  -> like {'yes' if liked == LIKE_YES else 'no'}")

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
                        print("Serial service not found - is the app running and BLE on?")
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
                    self.last_liked = None
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
    browse, so it is often only the home page - looking that up gets
    "Unsupported URL" and the track shows no length.
    """
    return bool(url) and ("/watch?" in url or "youtu.be/" in url)


# ---------------------------------------------------------------- main
async def dump_once(bridge, wait=12.0):
    """Print what the bridge sees, and exit. No Flipper needed.

    It goes through the real pipeline (clock, yt-dlp length lookup), so this is
    the same numbers the device would be sent - minus the BLE hop.
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
        print("No media session found - start playback first.")
        return 1

    print(f"app      : {meta['app'] or '?'}")
    print(f"title    : {meta['title']}")
    print(f"artist   : {meta['artist'] or '?'}")
    print(f"state    : {'playing' if meta['playing'] else 'paused'}")
    print(f"position : {meta['pos']}s")
    print(f"length   : {meta['dur']}s" + ("" if meta["dur"] else "  (unknown)"))
    print(f"volume   : {'?' if meta['vol'] is None else str(meta['vol']) + '%'}")
    print(f"art      : {'none' if not meta['art'] else 'yes'}")
    liked = meta.get("liked")
    link = getattr(bridge.backend, "link", None)
    source = "  (browser extension)" if (link is not None and link.fresh()) else ""
    print(f"liked    : {'?' if liked is None else ('yes' if liked else 'no')}{source}")

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
    firefox_port = getattr(args, "firefox_port", LIKE_PORT) or None
    link = None
    ext_port = getattr(args, "ext_port", EXTENSION_PORT)
    if ext_port:
        link = BrowserLink(ext_port)
        if not link.start():
            link = None

    if args.list:
        if IS_WINDOWS:
            backend = WindowsBackend(args.player, firefox_port, link)
            rows = await backend.list()
            if not rows:
                print("No media sessions - start playback in Firefox/YouTube Music first.")
            for row in rows:
                print(row)
            return 0
        mpris = MprisBackend(args.player, link)
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
        backend = WindowsBackend(args.player, firefox_port, link)
    else:
        player = args.player
        if not player:
            mpris = MprisBackend(None, link)
            players = mpris.list()
            waited = getattr(args, "wait_for_player", False)
            while not players and waited:
                print("Waiting for a media player (start YouTube Music)...")
                time.sleep(5)
                players = mpris.list()
            if not players:
                sys.exit("No MPRIS players found - start YouTube Music / a video first.")
            player = players[0]
            print("Using player:", player)
        backend = MprisBackend(player, link)

    if args.cmd:
        if args.cmd not in ("p", "n", "b", "-", "+", "u", "d", "l"):
            sys.exit("--cmd takes one of p n b - + u d l")
        meta = await backend.read()
        if meta is None:
            sys.exit("No media session found - start playback first.")
        print(f"player: {meta['app'] or '?'} - {meta['title']} / {meta['artist']}")
        if args.cmd in CMD_VOLUME:
            level = backend.volume(CMD_VOLUME[args.cmd])
            print("volume:", "unavailable" if level is None else f"{level}%")
        elif args.cmd == "l":
            ok, message, _state = backend.like()
            print("like:", message, "" if ok else "(nothing typed)")
        else:
            await backend.command(args.cmd)
        backend.close()
        return 0

    if args.dump:
        try:
            return await dump_once(Bridge(backend, args.name, args.hz, args.address))
        finally:
            backend.close()

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
                         "(no Flipper needed - good first check)")
    ap.add_argument("--cmd", metavar="KEY",
                    help="send one device command and exit: p n b - + u d l")
    ap.add_argument("--pair", action="store_true",
                    help="pair with the Flipper and exit (Windows: pair once before first use)")
    ap.add_argument("--address", help="connect to this BLE address instead of scanning")
    ap.add_argument("--ext-port", type=int, default=EXTENSION_PORT, metavar="PORT",
                    help="localhost port the browser extension talks to "
                         f"(default {EXTENSION_PORT}, 0 = off)")
    ap.add_argument("--firefox-port", type=int, default=LIKE_PORT, metavar="PORT",
                    help="Firefox --remote-debugging-port used to read (and toggle) the "
                         f"YouTube Music like button (default {LIKE_PORT}, 0 = off)")
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
