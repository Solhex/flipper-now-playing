"""The two extension builds must describe themselves honestly.

Firefox has no Web Bluetooth, so it needs the Python bridge. A Firefox build
carrying the Chromium line "no companion app to install" is not a style problem:
that is the text the browser shows in the add-ons list and the install prompt,
and it tells the user they are missing nothing when they are missing the piece
that makes the extension work. It shipped that way once, copied into both
targets, which is why this is pinned.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "extension"))

import build  # noqa: E402


class BuildManifestTests(unittest.TestCase):
    """build_manifest() is pure, so both targets can be inspected directly."""

    @classmethod
    def setUpClass(cls):
        cls.base = build.json.loads((build.HERE / "manifest.json").read_text(encoding="utf-8"))

    def firefox(self):
        return build.build_manifest(self.base, "firefox")

    def chrome(self):
        return build.build_manifest(self.base, "chrome")

    def test_firefox_does_not_claim_there_is_nothing_to_install(self):
        text = self.firefox()["description"].lower()
        self.assertNotIn("no companion app", text)
        self.assertIn("bridge", text)

    def test_chromium_keeps_that_claim_because_there_it_is_true(self):
        # There the link page holds the Bluetooth connection itself.
        self.assertIn("no companion app", self.chrome()["description"].lower())

    def test_descriptions_fit_the_store_limit(self):
        for target in ("firefox", "chrome"):
            length = len(build.build_manifest(self.base, target)["description"])
            self.assertLessEqual(length, 132, f"{target} description is {length} chars")

    def test_firefox_keeps_a_button_because_access_is_opt_in_there(self):
        # Firefox makes host permissions opt-in, and permissions.request() needs
        # a user gesture - so without a button there is nothing for a user to
        # click, and an installed add-on can silently be unable to read the page.
        manifest = self.firefox()
        self.assertIn("action", manifest)
        self.assertIn("access", manifest["action"]["default_title"].lower())

    def test_firefox_drops_what_it_cannot_use(self):
        manifest = self.firefox()
        self.assertNotIn("bluetooth", manifest.get("permissions", []))
        self.assertEqual(manifest["background"], {"scripts": ["background.js"]})
        gecko = manifest["browser_specific_settings"]["gecko"]
        self.assertEqual(gecko["id"], "ytm-bridge@solhex.local")

    def test_chromium_keeps_the_web_bluetooth_pieces(self):
        manifest = self.chrome()
        self.assertIn("bluetooth", manifest.get("permissions", []))
        self.assertIn("action", manifest)
        self.assertEqual(manifest["background"], {"service_worker": "background.js"})
        self.assertNotIn("browser_specific_settings", manifest)

    def test_the_web_bluetooth_files_ship_only_where_they_work(self):
        # remote.html/remote.js are the link page: Firefox ships neither.
        for name in build.WEB_BLUETOOTH_ONLY:
            self.assertIn(name, build.SHARED)


if __name__ == "__main__":
    unittest.main()
