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
    """[type:1][len:2 LE][payload] — the device's own layout."""
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

    def test_seek_step_is_five_seconds_on_mpris(self):
        self.assertEqual(y.MprisBackend.seek_step, 5)
        self.assertEqual(y.SEEK_STEP, 5)


class TrackUrlTests(unittest.TestCase):
    def test_watch_and_short_links_are_tracks(self):
        self.assertTrue(y.is_track_url("https://music.youtube.com/watch?v=abc"))
        self.assertTrue(y.is_track_url("https://youtu.be/abc"))

    def test_home_page_is_not(self):
        self.assertFalse(y.is_track_url("https://music.youtube.com/"))
        self.assertFalse(y.is_track_url(""))


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

    def test_state_goes_stale(self):
        self.post_state({"liked": True, "title": "Song"})
        self.link.updated -= 10
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
