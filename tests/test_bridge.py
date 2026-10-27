"""Tests for the platform-neutral half of the bridge.

The Linux path cannot be executed on a Windows machine and the wire format is
consumed by firmware that is awkward to run in a test, so both are pinned here
against stubs: MPRIS is fed canned `playerctl` output, and the frames are parsed
back byte by byte the way the device does.

    python -m unittest discover -s tests -v
"""

import asyncio
import contextlib
import io
import json
import signal
import socket
import sys
import threading
import time
import types
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

import ytm_bridge as y  # noqa: E402


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def parse_frame(blob):
    """[type:1][len:2 LE][payload] - the device's own layout."""
    assert len(blob) >= 3, blob
    kind = blob[0]
    length = int.from_bytes(blob[1:3], "little")
    return kind, blob[3:3 + length]


class FrameTests(unittest.TestCase):
    def test_meta_frame_layout(self):
        meta = {"playing": True, "pos": 42, "dur": 300,
                "title": "Title", "artist": "Artist"}
        kind, payload = parse_frame(y.meta_frame(meta))
        self.assertEqual(kind, y.FRAME_META)
        self.assertEqual(payload[0], 1)
        self.assertEqual(int.from_bytes(payload[1:3], "little"), 42)
        self.assertEqual(int.from_bytes(payload[3:5], "little"), 300)
        self.assertEqual(payload[5:], b"Title\0Artist\0")

    def test_meta_frame_without_text_is_five_bytes(self):
        meta = {"playing": False, "pos": 0, "dur": 1, "title": "x", "artist": ""}
        _, payload = parse_frame(y.meta_frame(meta, with_text=False))
        self.assertEqual(len(payload), 5)

    def test_meta_frame_truncates_text(self):
        meta = {"playing": True, "pos": 0, "dur": 0,
                "title": "t" * 500, "artist": "a" * 500}
        _, payload = parse_frame(y.meta_frame(meta))
        title, _, artist = payload[5:].partition(b"\0")
        self.assertEqual(len(title), y.TITLE_MAX)
        self.assertEqual(len(artist.rstrip(b"\0")), y.ARTIST_MAX)

    def test_positions_past_16_bits_are_clamped_not_crashed(self):
        # Nothing caps the clock when the player reports no length (a stream),
        # and to_bytes raises above 65535 - which used to take the push loop down
        # into a reconnect every 3 s. The extension clamps identically.
        meta = {"playing": True, "pos": 70000, "dur": 999999,
                "title": "x", "artist": "y"}
        _, payload = parse_frame(y.meta_frame(meta, with_text=False))
        self.assertEqual(payload[1:5], b"\xff\xff\xff\xff")

    def test_like_frame_clamps_state(self):
        for state, expected in ((0, 0), (1, 1), (2, 2), (7, 2), (-5, 0)):
            kind, payload = parse_frame(y.like_frame(state))
            self.assertEqual(kind, y.FRAME_LIKE)
            self.assertEqual(payload, bytes([expected]))

    def test_vol_frame(self):
        kind, payload = parse_frame(y.vol_frame(140))
        self.assertEqual((kind, payload), (y.FRAME_VOL, b"\x64"))

    def test_volume_can_say_unknown(self):
        # A paused player's slider is not the loudness in the room, so the
        # device has to be able to stop claiming a number ('--').
        kind, payload = parse_frame(y.vol_frame(None))
        self.assertEqual((kind, payload), (y.FRAME_VOL, bytes([y.VOL_UNKNOWN])))
        self.assertEqual(y.VOL_UNKNOWN, 255)

    def test_toast_frame_carries_icon(self):
        kind, payload = parse_frame(y.toast_frame("Liked", icon=1))
        self.assertEqual(kind, y.FRAME_TOAST)
        self.assertEqual(payload[0], 1)
        self.assertEqual(payload[1:], b"Liked")

    def test_toast_text_leaves_room_for_the_icon(self):
        _, payload = parse_frame(y.toast_frame("x" * 200, icon=2))
        self.assertLessEqual(len(payload), y.TOAST_MAX)


class ArtTests(unittest.TestCase):
    def test_black_square_becomes_all_set_bits(self):
        buffer = io.BytesIO()
        Image.new("L", (120, 120), 0).save(buffer, "PNG")
        width, height, data = y.art_to_xbm(buffer.getvalue())
        self.assertEqual((width, height), (y.ART_SIZE, y.ART_SIZE))
        row_bytes = (y.ART_SIZE + 7) // 8      # XBM pads each row to a byte
        self.assertEqual(len(data), y.ART_SIZE * row_bytes)
        # 44 px is 5 whole bytes plus 4 bits: the last byte of each row is
        # padded, and PIL leaves those bits clear, so it reads 0x0F not 0xFF.
        padded = (y.ART_SIZE % 8)
        expected_last = (1 << padded) - 1 if padded else 0xFF
        for row in range(y.ART_SIZE):
            chunk = data[row * row_bytes:(row + 1) * row_bytes]
            self.assertTrue(all(b == 0xFF for b in chunk[:-1]), f"row {row} body")
            self.assertEqual(chunk[-1], expected_last, f"row {row} padding")

    def test_white_square_becomes_all_clear_bits(self):
        buffer = io.BytesIO()
        Image.new("L", (120, 120), 255).save(buffer, "PNG")
        _, _, data = y.art_to_xbm(buffer.getvalue())
        self.assertTrue(all(b == 0x00 for b in data))


class ClockTests(unittest.TestCase):
    def test_starts_at_zero_without_a_position(self):
        clock = y.TrackClock()
        self.assertEqual(clock.update("a|b", playing=True, real_pos=0), 0)

    def test_accumulates_while_playing(self):
        clock = y.TrackClock()
        clock.update("a|b", True, 0)
        clock.acc = 0.0
        clock.last -= 5.0            # pretend five seconds went by
        self.assertGreaterEqual(clock.update("a|b", True, 0), 5)

    def test_paused_does_not_accumulate(self):
        clock = y.TrackClock()
        clock.update("a|b", False, 0)
        clock.last -= 5.0
        self.assertLess(clock.update("a|b", False, 0), 1)

    def test_real_position_wins(self):
        clock = y.TrackClock()
        clock.update("a|b", True, 30)
        # A jump larger than the jitter window snaps to what the player says
        self.assertEqual(clock.update("a|b", True, 45), 45)
        # ... while a second of drift against our own count is ignored, so a
        # player that reports only now and then cannot make the UI jump about.
        self.assertEqual(clock.update("a|b", True, 46), 45)

    def test_new_track_resets(self):
        clock = y.TrackClock()
        clock.update("a|b", True, 100)
        self.assertEqual(clock.update("c|d", True, 0), 0)

    def test_nudge_never_goes_negative(self):
        clock = y.TrackClock()
        clock.update("a|b", True, 2)
        clock.nudge(-10)
        self.assertEqual(clock.acc, 0.0)


class MprisParsingTests(unittest.TestCase):
    """The Linux side, driven through canned playerctl output."""

    def backend(self, line):
        backend = y.MprisBackend("firefox")

        def fake(*_args):
            return True, line

        backend.run_playerctl = fake
        backend.stream = lambda force=False, running_only=False: (None, None)   # no mixer in a test
        return backend

    def test_the_mixer_level_wins_over_mpris(self):
        # The mixer is the gain you hear; MPRIS only reports the player's own idea
        # of its volume (which said 15% while the stream was at 100%).
        line = "Playing\t0\t0\tSong\tArtist\t\t\t0.42\n"
        backend = y.MprisBackend("firefox")
        backend.run_playerctl = lambda *_a: (True, line)
        backend.stream = lambda force=False, running_only=False: (414, 35)
        meta = asyncio.run(backend.read())
        assert meta is not None
        self.assertEqual(meta["vol"], 35)

    def test_a_missing_playerctl_does_not_end_the_program(self):
        # It used to sys.exit() from list(), which the poll loop reaches through
        # _refollow(): a bridge whose playerctl went away killed itself, and on a
        # machine without playerctl (the CI runner) every read() died with it.
        backend = y.MprisBackend("firefox")
        original = y.subprocess.run

        def missing(*_args, **_kwargs):
            raise FileNotFoundError("playerctl")

        y.subprocess.run = missing
        try:
            self.assertEqual(backend.list(), [])
            self.assertTrue(backend.missing_playerctl)
        finally:
            y.subprocess.run = original

    def test_metadata_line_is_parsed(self):
        line = ("Playing\t42000000\t215000000\tSong\tArtist\t"
                "file:///tmp/cover.png\t/data/x.opus\t0.42\n")
        meta = asyncio.run(self.backend(line).read())
        assert meta is not None
        self.assertEqual(meta["title"], "Song")
        self.assertEqual(meta["artist"], "Artist")
        self.assertEqual(meta["pos"], 42)
        self.assertEqual(meta["dur"], 215)
        self.assertTrue(meta["playing"])
        self.assertEqual(meta["vol"], 42)
        self.assertIsNone(meta["liked"])          # MPRIS carries no like state

    def test_empty_output_means_no_player(self):
        self.assertIsNone(asyncio.run(self.backend("").read()))

    def test_missing_fields_do_not_crash(self):
        meta = asyncio.run(self.backend("Paused\n").read())
        assert meta is not None
        self.assertEqual(meta["title"], "Unknown")
        self.assertEqual(meta["pos"], 0)
        self.assertFalse(meta["playing"])

    def test_a_paused_player_still_reports_its_volume(self):
        # Pausing must not blank the number: it is still the level the music
        # resumes at, and a row that flickers on every pause is noise.
        line = ("Paused\t0\t0\tSong\tArtist\t\t\t0.9\n")
        meta = asyncio.run(self.backend(line).read())
        assert meta is not None
        self.assertEqual(meta["vol"], 90)

    def test_seek_step_matches_the_site(self):
        # YouTube Music's own seek controls step 5 s, so the device must not
        # jump 10.
        self.assertEqual(y.SEEK_STEP, 5)
        self.assertEqual(y.MprisBackend.seek_step, y.SEEK_STEP)

    def test_the_keystroke_path_lands_on_the_same_distance(self):
        # The Windows-Firefox path can only tap YouTube Music's 1-second
        # shortcut, so it must derive its tap count from SEEK_STEP rather than
        # carry a distance of its own.
        self.assertEqual(y.YT_SEEK_TAP_S, 1)
        self.assertEqual(y.SEEK_STEP % y.YT_SEEK_TAP_S, 0)
        self.assertEqual(int(round(y.SEEK_STEP / y.YT_SEEK_TAP_S)) * y.YT_SEEK_TAP_S,
                         y.SEEK_STEP)


class TrackUrlTests(unittest.TestCase):
    def test_watch_and_short_links_are_tracks(self):
        self.assertTrue(y.is_track_url("https://music.youtube.com/watch?v=abc"))
        self.assertTrue(y.is_track_url("https://youtu.be/abc"))

    def test_home_page_is_not(self):
        self.assertFalse(y.is_track_url("https://music.youtube.com/"))
        self.assertFalse(y.is_track_url(""))


class LinkToggleTests(unittest.TestCase):
    """What `link_toggle` promises the like button on the device."""

    def setUp(self):
        self.port = free_port()
        self.link = y.BrowserLink(self.port)
        self.assertTrue(self.link.start(), self.link.error)
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.link.close()

    def post(self, liked, title="Song"):
        body = json.dumps({"liked": liked, "title": title}).encode()
        request = urllib.request.Request(f"{self.base}/state", data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
        urllib.request.urlopen(request, timeout=5).read()

    def test_no_extension_means_none(self):
        self.assertIsNone(y.link_toggle(self.link, timeout=0.2, attempts=1))

    def test_returns_the_new_state_once_it_moves(self):
        self.post(False)

        def page():
            # the extension: take the command, click, report the result
            with urllib.request.urlopen(f"{self.base}/wait", timeout=5) as reply:
                json.load(reply)
            self.post(True, "Song (liked)")

        threading.Thread(target=page, daemon=True).start()
        self.assertIs(y.link_toggle(self.link, timeout=3.0), True)

    def test_retries_when_the_first_command_is_swallowed(self):
        self.post(False)
        calls = []

        def page():
            for index in range(2):
                with urllib.request.urlopen(f"{self.base}/wait", timeout=5) as reply:
                    json.load(reply)
                calls.append(index)
                if index == 1:                 # first one lands nowhere
                    self.post(True, "Song (liked)")

        threading.Thread(target=page, daemon=True).start()
        self.assertIs(y.link_toggle(self.link, timeout=0.6, attempts=3), True)
        self.assertEqual(len(calls), 2)

    def test_heartbeats_do_not_count_as_a_like(self):
        self.post(False)
        stop = threading.Event()

        def heartbeats():
            while not stop.is_set():
                self.post(False)               # unchanged: same state
                time.sleep(0.1)

        threading.Thread(target=heartbeats, daemon=True).start()
        try:
            self.assertIsNone(y.link_toggle(self.link, timeout=0.5, attempts=1))
        finally:
            stop.set()


class BrowserLinkTests(unittest.TestCase):
    """The localhost protocol the extension speaks."""

    def setUp(self):
        self.port = free_port()
        self.link = y.BrowserLink(self.port)
        self.assertTrue(self.link.start(), self.link.error)
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.link.close()

    def post_state(self, payload):
        body = json.dumps(payload).encode()
        request = urllib.request.Request(f"{self.base}/state", data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
        urllib.request.urlopen(request, timeout=5).read()

    def test_state_round_trip(self):
        self.assertFalse(self.link.fresh())
        self.post_state({"liked": True, "title": "Song", "artist": "A", "playing": True})
        self.assertTrue(self.link.fresh())
        self.assertTrue(self.link.liked)
        self.assertEqual(self.link.state()["title"], "Song")

    def test_preflight_is_answered_for_extension_origins_only(self):
        # An add-on whose 127.0.0.1 host permission is not in effect is
        # preflighted before every POST; a 501 there silently eats the like
        # state. A web page must still get nothing, or any site could drive the
        # helper - and a page cannot set an extension Origin, so this is safe.
        def options(origin):
            request = urllib.request.Request(
                f"{self.base}/state", method="OPTIONS",
                headers={"Origin": origin, "Access-Control-Request-Method": "POST"})
            try:
                with urllib.request.urlopen(request, timeout=5) as reply:
                    return reply.status, reply.headers.get("Access-Control-Allow-Origin")
            except urllib.error.HTTPError as exc:
                answer = (exc.code, exc.headers.get("Access-Control-Allow-Origin"))
                exc.close()
                return answer

        self.assertEqual(options("moz-extension://abc"), (204, "moz-extension://abc"))
        self.assertEqual(options("https://evil.example"), (501, None))

    def test_probe_arrives_over_get_when_the_post_is_blocked(self):
        # An add-on with no granted hosts is CORS-preflighted on every POST, so
        # the probe has to travel on a GET - otherwise the one reading that says
        # "no site access" is precisely the one that never arrives.
        query = urllib.parse.urlencode({"probe": json.dumps({"siteAccess": False,
                                                             "mode": "bridge"})})
        urllib.request.urlopen(f"{self.base}/diag?{query}", timeout=5).read()
        self.assertEqual(self.link.probe, {"siteAccess": False, "mode": "bridge"})

    def test_focused_tab_wins_over_a_later_background_one(self):
        self.post_state({"client": "a", "liked": True, "title": "Tab A",
                         "focused": True, "visible": True, "lastActive": 1000})
        self.post_state({"client": "b", "liked": False, "title": "Tab B",
                         "focused": False, "visible": True, "lastActive": 2000})
        # B posted last, but A is the tab the user is looking at
        self.assertEqual(self.link.state()["title"], "Tab A")
        self.assertEqual(self.link.state()["client"], "a")

    def test_last_active_breaks_the_tie(self):
        self.post_state({"client": "a", "title": "Old", "focused": False,
                         "visible": True, "lastActive": 1000})
        self.post_state({"client": "b", "title": "Recent", "focused": False,
                         "visible": True, "lastActive": 5000})
        self.assertEqual(self.link.state()["title"], "Recent")

    def test_toggle_is_aimed_at_the_followed_tab(self):
        self.post_state({"client": "a", "liked": False, "title": "Tab A",
                         "focused": True, "visible": True})
        self.link.request_toggle("Tab A")
        with urllib.request.urlopen(f"{self.base}/wait", timeout=5) as reply:
            command = json.load(reply)
        self.assertEqual(command["cmd"], "toggle")
        self.assertEqual(command["client"], "a")

    def test_state_goes_stale(self):
        self.post_state({"liked": True, "title": "Song"})
        self.assertTrue(self.link.fresh())
        # age the tab's record: freshness is per client, not one global clock
        for record in self.link.clients.values():
            record["updated"] -= 10
        self.assertFalse(self.link.fresh())

    def test_toggle_is_handed_to_the_next_poll(self):
        self.post_state({"liked": False, "title": "Song"})
        self.link.request_toggle()
        with urllib.request.urlopen(f"{self.base}/wait", timeout=5) as reply:
            self.assertEqual(json.load(reply)["cmd"], "toggle")

    def test_wait_returns_empty_without_a_command(self):
        self.link.wait_timeout = 0.4       # do not sit out the real 20 s poll
        self.link.request_toggle()
        with urllib.request.urlopen(f"{self.base}/wait", timeout=5) as reply:
            json.load(reply)
        with urllib.request.urlopen(f"{self.base}/wait", timeout=5) as reply:
            self.assertIsNone(json.load(reply)["cmd"])

    def test_second_server_mirrors_the_first(self):
        self.post_state({"liked": True, "title": "Live"})
        helper = y.BrowserLink(self.port)          # port already owned
        self.assertTrue(helper.start(), helper.error)
        time.sleep(1.4)                            # the mirror polls once a second
        self.assertTrue(helper.fresh())
        self.assertTrue(helper.liked)
        helper.close()

    def test_the_extension_probe_is_kept_and_exposed(self):
        # The bridge has no other way to learn what the browser can see: the
        # probe is what makes "the page never reports" attributable.
        probe = {"siteAccess": False, "tabs": [{"id": 7, "discarded": False}],
                 "inject": "failed: no permission", "mode": "bridge"}
        request = urllib.request.Request(
            f"{self.base}/diag", data=json.dumps(probe).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as reply:
            self.assertTrue(json.load(reply)["ok"])
        with urllib.request.urlopen(f"{self.base}/state", timeout=5) as reply:
            self.assertEqual(json.load(reply)["probe"], probe)

    def test_a_junk_probe_does_not_take_the_link_down(self):
        request = urllib.request.Request(
            f"{self.base}/diag", data=b"not json", method="POST")
        with urllib.request.urlopen(request, timeout=5) as reply:
            self.assertTrue(json.load(reply)["ok"])
        with urllib.request.urlopen(f"{self.base}/state", timeout=5) as reply:
            self.assertIn("error", json.load(reply)["probe"])


class HttpTrustTests(unittest.TestCase):
    """The loopback port is reachable from any page; only the add-on may act.

    A cross-origin POST with `text/plain` is a "simple request" - no preflight,
    sent by the browser whatever the server thinks - so `Origin` is the only
    thing that separates a visited site from the add-on, and a page cannot forge
    an extension scheme. Both holes here were measured, not imagined.
    """

    def setUp(self):
        self.port = free_port()
        self.link = y.BrowserLink(self.port)
        self.assertTrue(self.link.start(), self.link.error)
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.link.close()

    def request(self, path, data=None, origin=None, method="POST"):
        headers = {"Content-Type": "text/plain"}
        if origin:
            headers["Origin"] = origin
        request = urllib.request.Request(f"{self.base}{path}", data=data,
                                         method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as reply:
                return reply.status
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            return code

    def test_a_page_cannot_queue_a_like(self):
        # Before this guard, this exact request liked a real track.
        self.assertEqual(self.request("/toggle", origin="https://evil.example"), 403)
        self.assertTrue(self.link._commands.empty())

    def test_a_page_cannot_swallow_a_queued_command(self):
        # The poll hands a command out of the queue: a page that gets there first
        # eats it, and the Flipper's press silently does nothing.
        self.link.request_toggle("Song", "tab")
        self.assertEqual(self.request("/wait", method="GET",
                                      origin="https://evil.example"), 403)
        self.assertEqual(self.link._commands.qsize(), 1)

    def test_a_page_cannot_fake_the_probe(self):
        self.assertEqual(self.request("/diag", method="GET",
                                      origin="https://evil.example"), 403)
        self.assertIsNone(self.link.probe)

    def test_the_add_on_is_still_allowed(self):
        self.assertEqual(self.request("/toggle", origin="moz-extension://abc"), 200)

    def test_a_local_tool_without_an_origin_is_still_allowed(self):
        self.assertEqual(self.request("/toggle"), 200)

    def test_a_bad_content_length_is_answered_instead_of_killing_the_handler(self):
        # Measured: the handler died with ValueError and the client got nothing
        # at all (curl reported status 000), while the traceback filled the log.
        request = urllib.request.Request(
            f"{self.base}/state", data=b"{}", method="POST",
            headers={"Content-Length": "abc", "Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()


class SinkInputParseTests(unittest.TestCase):
    """The mixer is the layer that decides how loud the music really is.

    The page's own volume claimed 15% while Firefox's stream sat at 100%, so
    every press "did nothing" and the music stayed loud.
    """

    SAMPLE = (
        "Sink Input #414\n"
        "\tMute: no\n"
        "\tVolume: front-left: 65536 / 100% / 0.00 dB,   front-right: 65536 / 100% / 0.00 dB\n"
        "\t\tapplication.name = \"Firefox\"\n"
        "\tmodule-stream-restore.id = \"sink-input-by-application-name:Firefox\"\n"
        "\n"
        "Sink Input #976\n"
        "\tMute: no\n"
        "\tVolume: front-left: 32768 /  50% / -6.02 dB,   front-right: 32768 /  50% / -6.02 dB\n"
        "\t\tapplication.name = \"vesktop\"\n"
    )

    def test_streams_come_out_with_their_levels(self):
        self.assertEqual(y.parse_streams(self.SAMPLE),
                         [(414, "Firefox", 100, False), (976, "vesktop", 50, False)])

    def test_junk_gives_no_streams(self):
        self.assertEqual(y.parse_streams("no sink inputs here\n"), [])

    def test_the_player_name_picks_its_own_stream(self):
        backend = y.MprisBackend("firefox.instance_1_44")
        original = y.pactl_streams
        y.pactl_streams = lambda: y.parse_streams(self.SAMPLE)
        try:
            self.assertEqual(backend.stream(force=True), (414, 100))
        finally:
            y.pactl_streams = original

    TWO_STREAMS = (
        "Sink Input #414\n"
        "\tCorked: yes\n"
        "\tVolume: front-left: 19655 /  30% / -31.38 dB,   front-right: 19655 /  30% / -31.38 dB\n"
        "\t\tapplication.name = \"Firefox\"\n"
        "\n"
        "Sink Input #2432\n"
        "\tCorked: no\n"
        "\tVolume: front-left: 19655 /  30% / -31.38 dB,   front-right: 19655 /  30% / -31.38 dB\n"
        "\t\tapplication.name = \"Firefox\"\n"
    )

    def test_the_running_stream_wins_over_a_corked_one(self):
        # Measured here: Firefox owned #414 (corked) and #2432 (running). Moving
        # the corked one changed the Flipper's number and nothing else.
        backend = y.MprisBackend("firefox.instance_1_44")
        original = y.pactl_streams
        y.pactl_streams = lambda: y.parse_streams(self.TWO_STREAMS)
        try:
            self.assertEqual(backend.stream(force=True), (2432, 30))
        finally:
            y.pactl_streams = original

    def test_a_corked_stream_is_better_than_none_when_paused(self):
        # Paused: every stream may be corked, and its level is still the one the
        # music resumes at.
        backend = y.MprisBackend("firefox.instance_1_44")
        original = y.pactl_streams
        y.pactl_streams = lambda: y.parse_streams(self.SAMPLE)
        try:
            self.assertEqual(backend.stream(force=True), (414, 100))
        finally:
            y.pactl_streams = original

    def test_a_corked_stream_does_not_count_while_playing(self):
        # A track change tears the running stream down; the corked leftovers must
        # not answer for it, or the device shows their old level (95%) for a
        # second on every song change.
        backend = y.MprisBackend("firefox.instance_1_44")
        original = y.pactl_streams
        y.pactl_streams = lambda: y.parse_streams(
            "Sink Input #414\n"
            "\tCorked: yes\n"
            "\tVolume: front-left: 1 /  95% / x\n"
            "\t\tapplication.name = \"Firefox\"\n")
        try:
            self.assertEqual(backend.stream(force=True, running_only=True), (None, None))
            self.assertEqual(backend.stream(force=True), (414, 95))
        finally:
            y.pactl_streams = original

    def test_a_missing_stream_holds_the_last_level(self):
        # The number must not jump to another source while the stream is rebuilt.
        line = "Playing\t0\t0\tSong\tArtist\t\t\t0.42\n"
        backend = y.MprisBackend("firefox")
        backend.run_playerctl = lambda *_a: (True, line)
        answers = [(414, 35), (None, None)]
        backend.stream = lambda force=False, running_only=False: answers.pop(0)
        first = asyncio.run(backend.read())
        assert first is not None
        self.assertEqual(first["vol"], 35)
        backend.stream = lambda force=False, running_only=False: (None, None)
        second = asyncio.run(backend.read())
        assert second is not None
        self.assertEqual(second["vol"], 35)   # held, not the player's 42


class LinkVolumeTests(unittest.TestCase):
    """The volume readout and keys use the page's own slider, not MPRIS.

    MPRIS's volume for Firefox is a separate value: nudging it moved the
    Flipper's number while the site's slider stayed put, and the two then
    disagreed for the rest of the track.
    """

    def setUp(self):
        self.port = free_port()
        self.link = y.BrowserLink(self.port)
        self.assertTrue(self.link.start(), self.link.error)
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.link.close()

    def post(self, vol):
        body = json.dumps({"liked": False, "title": "Song", "playing": True,
                           "vol": vol}).encode()
        request = urllib.request.Request(f"{self.base}/state", data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
        urllib.request.urlopen(request, timeout=5).read()

    def test_no_extension_means_none(self):
        self.assertIsNone(y.link_volume(self.link, +1, timeout=0.2))

    def test_the_page_is_asked_and_its_new_level_comes_back(self):
        self.post(50)
        seen = []

        def page():
            with urllib.request.urlopen(f"{self.base}/wait", timeout=5) as reply:
                seen.append(json.load(reply).get("cmd"))
            self.post(55)              # the page moved its own slider

        threading.Thread(target=page, daemon=True).start()
        self.assertEqual(y.link_volume(self.link, +1, timeout=3.0), 55)
        self.assertEqual(seen, ["volup"])

    def test_staying_at_the_top_reports_where_it_is(self):
        # At 100 the page clamps, so nothing changes - the answer is still 100,
        # not "unknown" and not a fallback to a different volume.
        self.post(100)

        def page():
            with urllib.request.urlopen(f"{self.base}/wait", timeout=5) as reply:
                json.load(reply)

        threading.Thread(target=page, daemon=True).start()
        self.assertEqual(y.link_volume(self.link, +1, timeout=0.5), 100)

    def test_the_page_volume_is_used_when_there_is_no_mixer(self):
        self.post(30)
        line = "Playing\t0\t0\tSong\tArtist\t\t\t0.9\n"
        backend = y.MprisBackend("firefox", self.link)
        backend.run_playerctl = lambda *_a: (True, line)
        backend.stream = lambda force=False, running_only=False: (None, None)
        meta = asyncio.run(backend.read())
        assert meta is not None
        self.assertEqual(meta["vol"], 30)      # the page, not MPRIS's 90

    def test_the_test_hook_queues_the_page_nudge(self):
        self.post(50)
        request = urllib.request.Request(f"{self.base}/volume", data=b'{"sign":-1}',
                                         method="POST")
        urllib.request.urlopen(request, timeout=5).read()
        with urllib.request.urlopen(f"{self.base}/wait", timeout=5) as reply:
            self.assertEqual(json.load(reply)["cmd"], "voldown")

    def test_a_live_page_is_not_overridden_by_mpris(self):
        # No mixer, page reporting: the page is the control. A page that does not
        # answer gives back its unchanged level - switching the number's source
        # mid-press is what made the readout impossible to trust.
        self.post(30)
        backend = y.MprisBackend("firefox", self.link)
        backend.run_playerctl = lambda *_a: (True, "0.55\n")
        backend.stream = lambda force=False, running_only=False: (None, None)
        self.assertEqual(backend.volume(+1), 30)

    def test_the_key_moves_the_mixer_stream(self):
        backend = y.MprisBackend("firefox")
        backend.stream = lambda force=False, running_only=False: (414, 100)
        calls = []
        original = y.subprocess.run
        y.subprocess.run = lambda args, **kw: calls.append(args) or types.SimpleNamespace(returncode=0)
        try:
            backend.volume(-1)
        finally:
            y.subprocess.run = original
        self.assertIn(["pactl", "set-sink-input-volume", "414", "-5%"], calls)

    def test_without_a_page_or_mixer_the_volume_keys_still_work(self):
        backend = y.MprisBackend("firefox", None)
        backend.stream = lambda force=False, running_only=False: (None, None)
        calls = []

        def fake(*args):
            calls.append(args)
            return True, "0.55\n"

        backend.run_playerctl = fake
        self.assertEqual(backend.volume(+1), 55)
        self.assertEqual(calls[0], ("volume", "0.05+"))


class TransportRetryTests(unittest.TestCase):
    """A button press has to survive the player changing underneath it."""

    def backend(self, outcomes):
        backend = y.MprisBackend("firefox")
        calls = []

        def fake(*args):
            calls.append(args)
            return outcomes[min(len(calls) - 1, len(outcomes) - 1)], ""

        backend.run_playerctl = fake
        return backend, calls

    def test_a_quick_failure_is_retried_once(self):
        # playerctl refuses while Firefox rebuilds its media session between two
        # tracks, which is what made play/pause look broken for three presses.
        backend, calls = self.backend([False, True])
        asyncio.run(backend.command("p"))
        self.assertEqual(len(calls), 2)

    def test_a_dead_player_is_tried_twice_and_then_left_alone(self):
        backend, calls = self.backend([False])
        asyncio.run(backend.command("p"))
        self.assertEqual(len(calls), 2)

    def test_a_press_that_works_is_not_repeated(self):
        backend, calls = self.backend([True])
        asyncio.run(backend.command("p"))
        self.assertEqual(len(calls), 1)


class StaleExtensionHintTests(unittest.TestCase):
    """An old extension polls exactly like a current one; only silence differs."""

    def link(self, polls, probe=None):
        link = y.BrowserLink(59999)          # never started: state only
        link.polls = polls
        link.probe = probe
        return link

    def test_a_polling_but_silent_extension_is_called_out(self):
        link = self.link(5)
        link.note_probe_gap()
        self.assertTrue(link._gap_noted)

    def test_a_current_extension_is_not_called_out(self):
        link = self.link(5, probe={"version": "2.0.0"})
        link.note_probe_gap()
        self.assertFalse(link._gap_noted)

    def test_it_waits_for_a_few_polls_before_judging(self):
        link = self.link(1)
        link.note_probe_gap()
        self.assertFalse(link._gap_noted)

    def test_it_says_it_once_not_on_every_poll(self):
        # This used to reset the flag itself and then assert it had been set -
        # it passed whatever the code did.
        link = self.link(5)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            link.note_probe_gap()
            link.polls = 50
            link.note_probe_gap()
        self.assertEqual(out.getvalue().count("has never told us"), 1)


class ShutdownHandlerTests(unittest.TestCase):
    def test_sigterm_unwinds_instead_of_killing_the_process(self):
        # systemd stops the bridge with SIGTERM, and Python's default for that
        # skips every finally and atexit. Without this handler the Firefox debug
        # session leaks on each restart, and the like quietly degrades to typing
        # at a window until Firefox is restarted as well.
        if threading.current_thread() is not threading.main_thread():
            self.skipTest("signals can only be installed from the main thread")
        before = signal.getsignal(signal.SIGTERM)
        try:
            y.install_shutdown_handler()
            handler = signal.getsignal(signal.SIGTERM)
            self.assertTrue(callable(handler))
            with self.assertRaises(SystemExit):
                handler(signal.SIGTERM, None)
        finally:
            signal.signal(signal.SIGTERM, before)


class PlayerRefollowTests(unittest.TestCase):
    """After a browser restart the bridge has to follow the new player name."""

    def backend(self, player, players, touched=0.0):
        backend = y.MprisBackend(player)
        scans = []

        def fake_list():
            scans.append(1)
            return list(players)

        backend.list = fake_list
        backend._refollow_at = touched
        return backend, scans

    def test_it_follows_the_new_firefox_after_a_restart(self):
        # Firefox registers a new MPRIS name on every start. Asking the old name
        # forever is what left the Flipper on "waiting for PC data" until the
        # bridge was restarted by hand.
        backend, _ = self.backend("firefox.instance_1_2131",
                                  ["spotify", "firefox.instance_1_999999"])
        backend._refollow()
        self.assertEqual(backend.player, "firefox.instance_1_999999")

    def test_a_living_player_is_left_alone(self):
        backend, _ = self.backend("firefox.instance_1_2131",
                                  ["firefox.instance_1_2131", "spotify"])
        backend._refollow()
        self.assertEqual(backend.player, "firefox.instance_1_2131")

    def test_it_does_not_rescan_on_every_poll(self):
        backend, scans = self.backend("a", ["b"], touched=time.monotonic())
        backend._refollow()
        self.assertEqual(scans, [])
        self.assertEqual(backend.player, "a")


class FirefoxDebugWiringTests(unittest.TestCase):
    """The Linux backend must be given Firefox's debug port.

    It never was: the flag existed and was parsed, but only the Windows backend
    was handed it. So on Linux the like state could not be read at all - no
    heart - and a like had to be typed at a focused window, which is what pulled
    YouTube Music to the front on every press.
    """

    def test_a_port_gives_the_linux_backend_a_debug_reader(self):
        backend = y.MprisBackend("firefox", None, firefox_port=9222)
        self.assertIsInstance(backend._debug, y.FirefoxDebug)
        self.assertEqual(backend._debug.port, 9222)

    def test_no_port_means_no_debug_reader(self):
        self.assertIsNone(y.MprisBackend("firefox", None)._debug)


class DeviceTextTests(unittest.TestCase):
    """Titles the Flipper cannot draw are converted on the way out, not left blank.

    The device's fonts are Latin-only bitmap faces, so non-Latin bytes had no
    glyph and the screen showed gaps where the title should be.
    """

    def test_accents_fold_to_base_letters(self):
        self.assertEqual(y.device_text("Tïtle"), "Title")
        self.assertEqual(y.device_text("naïve café"), "naive cafe")

    def test_hangul_romanises_without_a_dictionary(self):
        # Hangul decomposes mechanically, so this needs no dependency at all.
        self.assertEqual(y.device_text("알루미늄"), "alluminyum")
        self.assertEqual(y.device_text("안녕하세요"), "annyeonghaseyo")

    def test_unsupported_scripts_are_marked_not_blank(self):
        # No glyph and no dictionary: a marker beats silent empty space.
        self.assertEqual(y.device_text("عربي"), "????")

    def test_the_result_is_always_drawable_ascii(self):
        for text in ("Tïtle", "알루미늄", "日本語", "もしもし", "北京", "😀", "",
                     "   ", "plain", "ＡＢＣ"):
            out = y.device_text(text)
            self.assertTrue(all(0x20 <= ord(c) < 0x7F for c in out),
                            f"{text!r} -> {out!r} still has something the font cannot draw")

    def test_ascii_is_left_alone(self):
        self.assertEqual(y.device_text("Busker, Clown, Dancer"), "Busker, Clown, Dancer")

    def test_whitespace_is_trimmed(self):
        self.assertEqual(y.device_text("  padded  "), "padded")

    def test_the_frame_builder_stays_a_pure_formatter(self):
        # The conversion happens on the way out (device_meta), not inside
        # meta_frame: the builder must keep carrying whatever it is handed, or
        # the byte-for-byte parity with the extension's builder stops meaning
        # anything. The raw spelling also has to survive for the cache key and
        # the yt-dlp search.
        m = {"playing": True, "pos": 1, "dur": 2, "title": "알루미늄", "artist": ""}
        self.assertIn("알루미늄".encode("utf-8"), y.meta_frame(m))

    def test_the_frame_that_is_actually_sent_is_drawable(self):
        # End of the chain: what push() hands the frame builder - decoded back
        # out of the payload, so this fails if the layout or the conversion
        # moves.
        m = {"playing": True, "pos": 3, "dur": 200,
             "title": "알루미늄", "artist": "Broken Valentine"}
        payload = y.meta_frame(y.device_meta(m))
        self.assertEqual(payload[0], y.FRAME_META)
        body = payload[3:]                      # state, pos, dur, then the text
        title, artist = body[5:].split(b"\x00")[:2]
        self.assertEqual(title, b"alluminyum")
        self.assertEqual(artist, b"Broken Valentine")
        self.assertTrue(all(0x20 <= b < 0x7F for b in title + artist))


class SessionChoiceTests(unittest.TestCase):
    """Which Windows media session the bridge follows.

    This logic used to live inside `if IS_WINDOWS:`, where nothing can import it
    on Linux - so a regression here was only ever found by a Windows user, and it
    shows up as the wrong track on the device.
    """

    PLAYING = 1

    class FakeSession:
        def __init__(self, aumid, playing=True):
            self.source_app_user_model_id = aumid
            self.playing = playing

        def get_playback_info(self):
            return types.SimpleNamespace(playback_status=1 if self.playing else 0)

    def label(self, session):
        return session.source_app_user_model_id

    def is_playing(self, session):
        return session.get_playback_info().playback_status == self.PLAYING

    def test_a_pinned_player_wins_even_when_it_is_paused(self):
        firefox = self.FakeSession("firefox", playing=False)
        spotify = self.FakeSession("spotify", playing=True)
        chosen = y.choose_session([spotify, firefox], "firefox", self.label, self.is_playing)
        self.assertIs(chosen, firefox)

    def test_a_pinned_player_that_is_not_running_does_not_fall_back(self):
        chosen = y.choose_session([self.FakeSession("spotify")], "firefox",
                                  self.label, self.is_playing)
        self.assertIsNone(chosen)

    def test_without_a_hint_the_playing_session_wins(self):
        paused = self.FakeSession("vlc", playing=False)
        playing = self.FakeSession("mpv", playing=True)
        self.assertIs(y.choose_session([paused, playing], None, self.label, self.is_playing),
                      playing)

    def test_nothing_playing_still_shows_the_first(self):
        only = self.FakeSession("vlc", playing=False)
        self.assertIs(y.choose_session([only], None, self.label, self.is_playing), only)

    def test_no_sessions_is_none(self):
        self.assertIsNone(y.choose_session([], None, self.label, self.is_playing))


class TimelineTrustTests(unittest.TestCase):
    """Firefox's SMTC timeline is stale until it is seen to move.

    Believing it straight away is what put the previous track's total, at
    position zero, on the device.
    """

    def setUp(self):
        self.guard = {"key": None, "pos": 0.0, "trusted": False}

    def test_a_frozen_timeline_is_not_believed(self):
        self.assertEqual(y.trusted_timeline(self.guard, "song", True, 0.0, 200), (0, 0))
        self.assertEqual(y.trusted_timeline(self.guard, "song", True, 0.0, 200), (0, 0))

    def test_it_is_believed_once_the_position_moves(self):
        y.trusted_timeline(self.guard, "song", True, 0.0, 200)
        self.assertEqual(y.trusted_timeline(self.guard, "song", True, 1.5, 200), (1, 200))

    def test_a_new_track_drops_back_to_untrusted(self):
        y.trusted_timeline(self.guard, "song", True, 0.0, 200)
        y.trusted_timeline(self.guard, "song", True, 1.5, 200)
        self.assertEqual(y.trusted_timeline(self.guard, "next", True, 0.0, 180), (0, 0))


class CjkReadingTests(unittest.TestCase):
    """Japanese/Chinese readings, when the optional dictionaries are installed.

    They cannot be installed in CI, so a stub stands in for them: this pins the
    branch itself (text -> reading -> '?'), which shipped with no test at all.
    """

    def tearDown(self):
        y._OPTIONAL_MODULES.clear()

    def test_kana_uses_the_japanese_reading(self):
        y._OPTIONAL_MODULES["pykakasi"] = types.SimpleNamespace(
            kakasi=lambda: types.SimpleNamespace(
                convert=lambda text: [{"orig": c, "hepburn": "moshi"} for c in text]))
        self.assertEqual(y.device_text("もし"), "moshimoshi")

    def test_hanzi_uses_the_chinese_reading(self):
        y._OPTIONAL_MODULES["pypinyin"] = types.SimpleNamespace(
            lazy_pinyin=lambda text: ["bei", "jing"])
        self.assertEqual(y.device_text("北京"), "bei jing")

    def test_nothing_is_importable_says_nothing_changes(self):
        # the real state on a default install: no dictionaries, so the caller
        # marks the characters instead of guessing
        y._OPTIONAL_MODULES["pykakasi"] = None
        self.assertEqual(y.device_text("もし"), "??")


if __name__ == "__main__":
    unittest.main(verbosity=2)
