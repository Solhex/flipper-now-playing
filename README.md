# YT Music Remote for Flipper Zero

What YouTube Music is playing, on your Flipper Zero: title, artist, cover art and
a progress bar, plus playback and volume from the Flipper's buttons. Bluetooth
only - no cable, no custom firmware.

```
YouTube Music -> your PC  -- BLE -->  Flipper (YT Music Remote)
                     |-- browser extension  (Brave / Chrome / Edge - nothing to run)
                     `-- ytm_bridge.py      (Firefox, Linux, Windows)
```

On the Flipper: open **YT Music Remote** (Apps -> Bluetooth) with Bluetooth ON.

## Pick your browser

| Your browser | What to install |
|---|---|
| Brave / Chrome / Edge | the extension - that's all |
| Firefox | the extension **and** the Python bridge |

Whichever you use, YouTube Music has to be playing in the *same* browser as the
extension - no browser can read another browser's tab. Only one host at a time
can hold the Bluetooth link (see [If something doesn't work](#if-something-doesnt-work)).

### Brave / Chrome / Edge

```
1. brave://extensions  (or chrome://extensions)
     -> Developer mode -> Load unpacked -> pick the  extension  folder in this repo

2. Toolbar icon -> the "Flipper YT Music Remote" tab opens
     -> Connect Flipper -> choose your Flipper -> confirm the code on its screen

3. Keep that tab open (pinning it is fine). It reconnects by itself from then on.
```

Play something on YouTube Music and the Flipper follows.

### Firefox

Firefox has no Web Bluetooth, so the extension cannot hold the Bluetooth link
itself - `ytm_bridge.py` does it on the PC. You need Python 3, `playerctl`, and:

```bash
pip install -r requirements.txt
python3 ytm_bridge.py --name Gumi      # Gumi = your Flipper's BLE name
```

Then the add-on - without it there is no heart on the screen and a like goes out
blind. Firefox only keeps signed add-ons, so pick one:

```
Quick - stops working when Firefox restarts:

    python3 extension/build.py            # -> extension/dist/firefox/
    about:debugging#/runtime/this-firefox -> Load Temporary Add-on
        -> pick extension/dist/firefox/manifest.json

Permanent - sign it with your own Mozilla API keys
(https://addons.mozilla.org/developers/addon/api/key/):

    npx web-ext sign --source-dir extension/dist/firefox \
        --api-key "$AMO_JWT_ISSUER" --api-secret "$AMO_JWT_SECRET" --channel unlisted

    then install the .xpi it writes
    (about:addons -> gear -> Install Add-on From File)
```

**After installing a signed .xpi, click the add-on's toolbar icon once.** Firefox
treats host access as opt-in, so a freshly installed add-on can look connected
while reading nothing - no heart, blind like. One click on "Grant site access
(once)" fixes it for good. A temporary add-on needs no click.

## Start it at login

Optional, but this is the only step after `pip install`:

```bash
# Linux - a systemd user service with its own venv
python3 linux_startup.py install --name Gumi
python3 linux_startup.py status      # unit path, state, log tail
python3 linux_startup.py uninstall

# Windows - a Startup shortcut that runs pythonw.exe, so no console window
py -3 -m venv .venv
.venv\Scripts\pip install -r requirements-windows.txt
.venv\Scripts\python windows_startup.py install --name Gumi
.venv\Scripts\python windows_startup.py status
.venv\Scripts\python windows_startup.py uninstall
```

Both take `--args "--player firefox --hz 15"` for extra bridge flags, and `--name`
(or `$FLIPPER_NAME`) for your Flipper's Bluetooth name.

Watch it work with `journalctl --user -u ytm-remote -f` (Linux) or
`windows_startup.py status`. Running `ytm_bridge.py` by hand is fine too - it
retries forever and reconnects on its own. On Windows, pair the Flipper once with
`python3 ytm_bridge.py --pair`.

## Buttons

| Key | Action |
|---|---|
| OK | play / pause |
| OK (hold) | like / unlike |
| Up / Down | volume +/-5 % |
| Up / Down (hold) | next / previous track |
| Left / Right | seek -5 s / +5 s (what YouTube Music's own seek controls do) |
| Back | settings menu (backlight, stay-on, key list, exit) |
| Back (hold) | exit the app |

The same list is on the device: Settings -> Keys.

**Volume** is the app's own level in the PC mixer (Core Audio on Windows, the
Firefox/PipeWire stream on Linux), not the site's slider - the number on the
screen and the Up/Down keys are that same level, so they cannot disagree, and it
works while paused. Only YouTube Music's stream is moved: a YouTube video playing
in another tab is left alone. Above 100% (PipeWire allows it) the screen shows
100; the keys still step down.

The screen shows the cover on the left, title/artist/state on the right, and a
progress bar with elapsed/total underneath. Long titles scroll. An unknown length
shows `--:--` rather than a wrong `0:00`.

## If something doesn't work

- **The bridge scans and finds nothing.** The Flipper only advertises while it is
  *not* connected. Drop stale links (`bluetoothctl disconnect <addr>`, or close
  the extension tab / phone app) and it comes back.
- **Only one host at a time.** The extension and the Python bridge both take the
  single BLE link - a second one silently kicks the first off. Stop the bridge
  (`systemctl --user stop ytm-remote`) while you use the extension.
- **Buttons do nothing, but the track still updates.** Wrong BLE characteristic:
  the receive line must show `notify=...228e61fe0000` (the indicate-only one).
- **`ufbt launch` says the app "has to be closed manually".** The firmware won't
  close a running app remotely: press Back on the Flipper, then launch again.
- **The build fails with `__pycache__/...pyc: file format not recognized`.** A
  stray `.pyc` next to the sources gets handed to the linker. Delete
  `__pycache__`.
- **The like stops working after reloading the extension.** Reload the YouTube
  Music tab once - reloading an unpacked extension strips its content script.

## Building the Flipper app

```bash
python3 -m ufbt            # -> dist/ytm_remote.fap   (expect Target 7, API 87.1)
python3 -m ufbt launch     # build, upload, run
```

Build against **your** firmware's SDK: it has to match the device or the loader
refuses the `.fap` ("too new"). `ufbt launch` needs write access to
`/dev/ttyACM0`; on *Permission denied*, install the udev rule (kept in
`~/.local/share/flipper/` so it survives upgrades):

```bash
sudo install -Dm644 ~/.local/share/flipper/99-flipper.rules /etc/udev/rules.d/99-flipper.rules
sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=tty
```

**Titles the fonts cannot draw.** The Flipper's fonts cover Latin only, so a
Korean, Japanese or Chinese title would reach the screen as blank space. The PC
side converts it first: accents fold (`naive cafe`), Korean romanises
(`alluminyum`), and anything still undrawable shows `?` instead of nothing.
Japanese and Chinese romanise too if you install the optional readings
(`pip install -r requirements-cjk.txt`). The browser extension shows `?` for
those, because it cannot carry the dictionaries.

## Developers

Repo layout, the wire format, bridge flags, extension packaging and the measured
details behind the volume and like behaviour: [docs/DEVELOPERS.md](docs/DEVELOPERS.md).
