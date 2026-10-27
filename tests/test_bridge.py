"""Tests for the platform-neutral half of the bridge.

The Linux path cannot be executed on a Windows machine and the wire format is
consumed by firmware that is awkward to run in a test, so both are pinned here
against stubs: MPRIS is fed canned `playerctl` output, and the frames are parsed
back byte by byte the way the device does.

    python -m unittest discover -s tests -v
"""

import asyncio
import io
import json
import socket
import sys
import threading
import time
import unittest
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

    def test_like_frame_clamps_state(self):
        for state, expected in ((0, 0), (1, 1), (2, 2), (7, 2), (-5, 0)):
            kind, payload = parse_frame(y.like_frame(state))
            self.assertEqual(kind, y.FRAME_LIKE)
            self.assertEqual(payload, bytes([expected]))

    def test_vol_frame(self):
        kind, payload = parse_frame(y.vol_frame(140))
        self.assertEqual((kind, payload), (y.FRAME_VOL, b"\x64"))

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
        return backend

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
