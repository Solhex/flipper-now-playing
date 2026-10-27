"""The Linux autostart installer's unit file, without needing systemd.

install() itself cannot run in a test (it wants a real user session and would
touch the machine), but the unit it writes is what decides whether autostart
works at all, so that part is checked directly.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import linux_startup as L  # noqa: E402


class UnitTextTests(unittest.TestCase):
    def test_the_command_is_absolute(self):
        # A user unit has no shell and does not inherit the checkout's working
        # directory, so a relative ExecStart is a service that never starts.
        text = L.unit_text(Path("/venv/bin/python"), "Gumi", "")
        self.assertIn(
            f"ExecStart=/venv/bin/python {L.BRIDGE} --name Gumi --wait-for-player", text)
        self.assertIn(f"WorkingDirectory={L.HERE}", text)

    def test_the_session_bus_is_provided(self):
        # playerctl reads MPRIS off the session bus, which a user service does
        # not inherit: without this it runs and has nothing to control.
        self.assertIn("DBUS_SESSION_BUS_ADDRESS=unix:path=%t/bus",
                      L.unit_text(Path("/p"), "Gumi", ""))

    def test_extra_args_survive_quoting(self):
        text = L.unit_text(Path("/p"), "Gumi", '--toast "a b"')
        self.assertIn('--toast "a b"', text)

    def test_it_starts_at_login_and_comes_back(self):
        text = L.unit_text(Path("/p"), "Gumi", "")
        self.assertIn("WantedBy=default.target", text)
        self.assertIn("Restart=always", text)
        self.assertIn("PYTHONUNBUFFERED=1", text)   # or the log stays empty


if __name__ == "__main__":
    unittest.main()
