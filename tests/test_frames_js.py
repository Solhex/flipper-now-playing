"""Byte parity between the extension's frames and ytm_bridge's.

Both hosts speak the same wire format to the same firmware, so any drift is a
silent corruption on the device - and the JavaScript half had no test at all.
The real file is loaded in node (see extension/test/frame_parity.mjs), never a
re-implementation, so this cannot pass while the shipped code is wrong.

Skipped when node is not installed.
"""

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ytm_bridge as y  # noqa: E402

NODE = shutil.which("node")
HARNESS = ROOT / "extension" / "test" / "frame_parity.mjs"


def _js_frames():
    assert NODE is not None          # the class is skipped without node
    out = subprocess.run(
        [NODE, str(HARNESS)], capture_output=True, text=True, timeout=60, check=True,
    )
    return json.loads(out.stdout)


@unittest.skipIf(NODE is None, "node is not installed")
class FrameParityTests(unittest.TestCase):
    """The JavaScript builder must produce the Python bytes exactly."""

    @classmethod
    def setUpClass(cls):
        cls.js = _js_frames()

    def assertParity(self, name, py_bytes):
        self.assertEqual(self.js[name], py_bytes.hex(),
                         f"{name}: the extension and the bridge disagree")

    def test_meta_with_text(self):
        meta = {"playing": True, "pos": 61, "dur": 362,
                "title": "Tïtle", "artist": "Artist"}
        self.assertParity("meta_full", y.meta_frame(meta, with_text=True))

    def test_meta_clock_only_is_five_bytes(self):
        meta = {"playing": True, "pos": 61, "dur": 362,
                "title": "Tïtle", "artist": "Artist"}
        payload = y.meta_frame(meta, with_text=False)
        self.assertEqual(len(payload), 8)          # 3 header + 5 payload
        self.assertParity("meta_clock", payload)

    def test_like_and_vol_and_toast(self):
        self.assertParity("like_yes", y.like_frame(1))
        self.assertParity("like_unknown", y.like_frame(2))
        self.assertParity("vol", y.vol_frame(100))
        self.assertParity("vol_clamped", y.vol_frame(140))
        self.assertParity("vol_unknown", y.vol_frame(None))
        self.assertParity("toast", y.toast_frame("Liked", 1))

    def test_art_envelope_packs_bits_lsb_first(self):
        expected = y.frame(y.FRAME_ART, bytes([8, 2, 0b10101010, 0b01010101]))
        self.assertParity("art", expected)

    def test_long_text_truncates_the_same_way(self):
        meta = {"playing": False, "pos": 0, "dur": 0,
                "title": "x" * 200, "artist": "y" * 200}
        self.assertParity("meta_truncated", y.meta_frame(meta, with_text=True))

    def test_positions_past_16_bits_clamp_the_same_way(self):
        # Nothing caps the clock for a stream with no length, and to_bytes raises
        # above 65535 - so both hosts clamp instead of packing a wrapped value.
        meta = {"playing": True, "pos": 70000, "dur": 999999, "title": "x", "artist": "y"}
        self.assertParity("meta_clamped", y.meta_frame(meta, with_text=False))

    def test_multibyte_text_splits_the_same_way(self):
        meta = {"playing": True, "pos": 5, "dur": 9, "title": "é" * 40, "artist": ""}
        self.assertParity("meta_split_char", y.meta_frame(meta, with_text=True))

    # Same list, same order, as DRAWABLE_CASES in extension/test/frame_parity.mjs.
    DRAWABLE_CASES = ["Tïtle", "naïve café", "알루미늄", "안녕하세요",
                      "Busker, Clown, Dancer", "", "  padded  ", "ＡＢＣ １２３",
                      "عربي", "😀 🎵"]

    def test_the_page_to_device_conversion_matches(self):
        # Both hosts convert titles for the same Latin-only fonts, so if the two
        # drift the same track shows different text depending on the browser.
        js = json.loads(self.js["drawable"])
        self.assertEqual(len(js), len(self.DRAWABLE_CASES),
                         "the case lists in the harness and here must stay the same length")
        for case, from_js in zip(self.DRAWABLE_CASES, js):
            self.assertEqual(y.device_text(case), from_js, f"the hosts disagree on {case!r}")


if __name__ == "__main__":
    unittest.main()
