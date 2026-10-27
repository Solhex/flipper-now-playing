# YT Music Remote - Flipper Zero <-> PC

Shows what YouTube Music is playing (title, artist, cover art, progress bar) on
your Flipper Zero, and controls playback from its buttons - over Bluetooth, no
cable, no custom firmware.

```
YouTube Music  -->  host  -- BLE serial -->  Flipper (ytm_remote.fap)
                     |-- browser extension   (Brave / Chrome / Edge - no companion app)
                     `-- ytm_bridge.py       (Linux, Windows, and Firefox)
```

## Which setup do you want?

| Your browser | What you install | Companion app? |
|---|---|---|
| **Brave / Chrome / Edge** | the extension - that's all | **none, nothing running** |
| **Firefox** | the extension **and** the Python bridge | yes (the bridge) |

On Chromium the extension *is* the whole thing: it holds the Bluetooth link
itself with Web Bluetooth and reads title, artist, cover, progress and the like
state straight out of the page. The Flipper runs the same `.fap` either way -
the wire format is identical, so the device doesn't care what's on the other end.

**Why Firefox can't be extension-only:** it has no Web Bluetooth (Mozilla closed
that as WONTFIX). It does have Web Serial, but that only reaches serial ports
over a **USB cable**, and the Flipper's USB port belongs to its firmware, not to
an app - so it can't carry this data. Nothing you can install in Firefox changes
this; it's a browser decision.

**One consequence either way:** the extension reads the page, so YouTube Music
has to be playing in the *same* browser as the extension. No browser can read a
tab in another browser.

## Quick start - Brave / Chrome / Edge (no companion app)

```
1. brave://extensions  (or chrome://extensions)
     -> turn on Developer mode -> Load unpacked -> pick the  extension  folder in
       this repo (it already carries the Chromium manifest)

2. Click the extension's toolbar icon -> the "Flipper YT Music Remote" tab opens
     -> Connect Flipper -> choose your Flipper -> confirm the code on its screen

3. Keep that tab open (pinning it is fine).
```

`extension/dist/` is generated (`python extension/build.py`) and is not in the
repo - only the Firefox side needs it, because its `.xpi` has to be signed.

The Bluetooth link lives in that tab - `navigator.bluetooth` only exists in a
document, so a background worker can't hold it. Chromium remembers the
permission, so it reconnects on its own from then on.

Play something on YouTube Music and the Flipper follows.

## Quick start - Firefox, or any OS with the bridge

The bridge reads the desktop's media session (MPRIS on Linux, Windows media
sessions on Windows) and drives the Flipper over BLE.

```bash
python3 ytm_bridge.py --name Gumi          # Gumi = your Flipper's BLE name
python3 ytm_bridge.py --list               # what's playing, and the player names
python3 ytm_bridge.py --dump               # the exact numbers sent to the Flipper
python3 ytm_bridge.py --cmd p              # fire one button, without a Flipper
```

Needs `bleak` + `pillow` (`pip install -r requirements.txt`), and `playerctl` on
Linux. On the Flipper: open **YT Music Remote** (Apps -> Bluetooth) with
Bluetooth ON.

### The Firefox add-on (worth it: the heart + a real like)

Firefox refuses to keep an unsigned add-on, so there are two routes and neither
is a couple of clicks:

```
1. Temporary - works immediately, gone after a Firefox restart:

     python3 extension/build.py          # -> extension/dist/firefox/

   then  about:debugging#/runtime/this-firefox  ->  Load Temporary Add-on  ->
   pick  extension/dist/firefox/manifest.json

2. Permanent - sign it with your own Mozilla API keys
   (https://addons.mozilla.org/developers/addon/api/key/):

     npx web-ext sign --source-dir extension/dist/firefox \
         --api-key "$AMO_JWT_ISSUER" --api-secret "$AMO_JWT_SECRET" --channel unlisted

   then install the signed .xpi it produces. It is not on AMO, so this is a
   per-user step, not a link.
```

Skip both and the bridge still runs - you lose the heart and a like becomes a
blind keystroke, as the browser table below spells out.

**After route 2, click the add-on's toolbar icon once.** Firefox makes an
add-on's host access opt-in, so a freshly installed .xpi can poll the bridge and
look healthy while it cannot read the page at all - no heart, and a like stays a
blind keystroke. One click on the icon ("Grant site access (once)"), or the same
sites switched on in `about:addons` -> the add-on -> Permissions, fixes it for
good. Route 1 needs no click: a temporary add-on holds that access while it is
loaded, which is why the two behave differently.

**Start it at login.** Both platforms have an installer that sets the autostart
up and starts it now, so this is the only step after `pip install`:

```bash
# Linux - systemd user service (creates its own venv, so it does not depend on
# your desktop's Python)
python3 linux_startup.py install --name Gumi
python3 linux_startup.py status          # unit path, state, log tail
python3 linux_startup.py uninstall

# Windows - a Startup shortcut that runs pythonw.exe, so no console window
py -3 -m venv .venv
.venv\Scripts\pip install -r requirements-windows.txt
.venv\Scripts\python windows_startup.py install --name Gumi
.venv\Scripts\python windows_startup.py status
.venv\Scripts\python windows_startup.py uninstall
```

Both take `--args "--player firefox --hz 15"` for extra bridge flags, and
`--name` (or `$FLIPPER_NAME`) for your Flipper's BLE name.

Watch it work: `journalctl --user -u ytm-remote -f` on Linux, or
`windows_startup.py status` on Windows. Running `ytm_bridge.py` by hand instead
is fine too - it retries forever and reconnects by itself, and needs
`playerctl` on Linux plus a one-off `ytm_bridge.py --pair` on Windows to bond the
Flipper.

## Flipper app

```bash
python3 -m ufbt            # -> dist/ytm_remote.fap   (expect Target 7, API 87.1)
python3 -m ufbt launch     # build, upload, run
```

`ufbt launch` needs write access to `/dev/ttyACM0`. On *Permission denied*,
install the udev rule (kept in `~/.local/share/flipper/` so it survives
upgrades):

```bash
sudo install -Dm644 ~/.local/share/flipper/99-flipper.rules /etc/udev/rules.d/99-flipper.rules
sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=tty
```

**Titles the fonts cannot draw.** The Flipper's fonts cover Latin only, so a
Korean, Japanese or Chinese title would reach the screen as blank space. The PC
side converts it before it goes out: accents fold (`naive cafe`), Korean
romanises (`alluminyum`), and anything still undrawable shows `?` instead of
nothing. The bridge romanises Japanese and Chinese as well if you install the
optional readings (`pip install -r requirements-cjk.txt`); the browser extension shows
`?` for those, because it cannot carry the dictionaries. The device still gets
the same frame either way.

Build the app against **your** firmware's SDK: it must match the device, or the
loader refuses it ("too new"). Momentum's SDK is what's used above.

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

One row per key and action, so a hold is its own line rather than sharing a cell
with the tap. The same list is on the device: Settings -> Keys.

**Which volume that is.** The app's own level in the mixer (Core Audio on
Windows, the Firefox/PipeWire stream on Linux), not the site's slider: measured
here, YouTube Music reported 10% while Firefox's stream sat at 110%, so the site
slider cannot make the music quieter. The number on the screen and the Up/Down
keys are that same mixer level, so they cannot disagree - and it works while
paused. Above 100% (PipeWire allows it) the screen shows 100; the keys still step
down.

The screen shows the cover on the left, title/artist/state on the right, and a
progress bar with elapsed/total underneath. Long titles scroll. An unknown
length shows `--:--` rather than a wrong `0:00`.

## The heart - and what each browser actually gives you

Nothing in MPRIS or the Windows media session exposes *whether a track is liked*
- only the page knows - so the host has to ask the page:

| Browser | Heart on the Flipper | Like (hold OK) | Needs the bridge? |
|---|---|---|---|
| Brave / Chrome / Edge | yes | real click on the real button | no |
| Firefox + extension | yes | real click on the real button | yes |
| Firefox, no extension | **no - blank** | blind keystroke, tab must be in front | yes |

Without the extension, like is a fallback: on Linux the bridge types YouTube
Music's `shift+=` at the focused window with `vkkey` + `swaymsg`, which means the
YouTube Music tab has to be the visible one, and it can't read the state before
or after - so it fires blind and the Flipper draws no heart at all. (The other
no-extension route is starting Firefox with `--remote-debugging-port=9222`,
which does give the heart, but only if you launch Firefox that way every time.)

With the extension: the heart reflects the site's own button, and a like is a
click on that button - no launch flags, no focus requirement, and it can tell
you the result.

## Troubleshooting

- **Bridge scans and finds nothing.** The Flipper only advertises while it is
  *not* connected. Drop a stale link (`bluetoothctl disconnect <addr>`, or
  close the extension tab/phone app) and it comes back.
- **Only one host at a time.** The extension and the Python bridge both take the
  single BLE link - a second one silently kicks the first off. Stop the bridge
  (`systemctl --user stop ytm-remote`) when you're using the extension.
- **Buttons do nothing but the track updates.** Wrong BLE characteristic: the
  receive line must show `notify=...228e61fe0000` (the indicate-only one).
- **`ufbt launch` says the app "has to be closed manually".** The firmware won't
  close a running app remotely: press Back on the Flipper, then launch again.
- **Build fails with `__pycache__/...pyc: file format not recognized`.** A stray
  `.pyc` next to the sources gets handed to the linker. Delete `__pycache__`.
- **Extension reloaded and the like stops working.** Reload the YouTube Music
  tab once - reloading an unpacked extension strips its content script.

## For developers

**Repo layout**

| Path | Role |
|---|---|
| `ytm_remote.c`, `application.fam` | the Flipper app (frames in, screen out, buttons back) |
| `ytm_bridge.py` | PC host: media session -> BLE, buttons -> the player |
| `extension/` | the add-on: page state -> BLE, buttons -> the page |
| `extension/build.py` | packages it for Chromium and Firefox |
| `windows_startup.py` | Windows: start the bridge at logon |
| `linux_startup.py` | Linux: install the systemd user service (autostart) |
| `tests/` | unit tests - no Flipper, browser or media player needed |

**Tests**

```bash
python -m unittest discover -s tests -v
```

**Extension packaging**

```bash
python extension/build.py
#  extension/dist/ytm-bridge-2.0.0-chrome.zip    (+ unpacked in dist/chrome)
#  extension/dist/ytm-bridge-2.0.0-firefox.xpi   (+ unpacked in dist/firefox)
```

Chromium: load `extension/dist/chrome` unpacked. Firefox: load
`extension/dist/firefox/manifest.json` via `about:debugging` (temporary), or
sign the `.xpi` for a permanent install:

```bash
export AMO_JWT_ISSUER=... AMO_JWT_SECRET=...     # from addons.mozilla.org, keep private
npx web-ext sign --source-dir extension/dist/firefox --channel unlisted
```

then `about:addons` -> gear -> *Install Add-on From File...*.

**Transport**

The Flipper's BLE **serial profile** - service
`8fe5b3d5-2e7f-4a98-2a48-7acc60fe0000`, chars `19ed82ae-ed21-4c9d-4145-...`:
`...228e62...` = RX (PC->Flipper, write), `...228e61...` = TX (Flipper->PC, **indicate**),
`...228e63...` = flow control, `...228e64...` = RPC status.

**Wire format** - `[type:1][len:2 LE][payload]`:

| Type | Payload |
|---|---|
| `0x01` META | `[state:1][pos:2 LE][dur:2 LE][title]0[artist]0` - exactly 5 bytes = a clock tick, keep the text (only after the device sends `k`) |
| `0x02` ART | `[w:1][h:1][1-bit rows, (w+7)/8 per row, LSB-first]` |
| `0x03` VOL | `[percent:1]` |
| `0x04` TOAST | `[icon:1][text]` - icon 0 none, 1 heart, 2 broken heart |
| `0x05` LIKE | `[state:1]` - 0 no, 1 yes, 2 unknown |

Flipper -> host: one byte - `p` `n` `b` `-` `+` `u` `d`, plus `k` ("I take
clock-only META") and `l` (like). Unknown types are dropped and the parser
resyncs, so either host can be updated without breaking a build already on the
device. Each frame type is one entry in `FRAME_SPECS` in `ytm_remote.c`, carrying
both its length bound and its handler.

**Seek distance** is one constant (`SEEK_STEP`, 5 s) on every path - the Flipper
sends `-`/`+` and the host decides how far. The exception that looks like one but
isn't: Firefox on Windows has no seek API, so that path types at the tab, and it
taps YouTube Music's *1-second* shortcut (`Shift+H`/`Shift+L`) `SEEK_STEP` times
rather than carrying a distance of its own. Plain `h`/`l` would move 10 s and
disagree with the device.

**Useful bridge flags:** `--name`, `--player`, `--hz` (default 20), `--dump`,
`--cmd`, `--pair`, `--wait-for-player`, `--toast "text"`, `--ext-port 0` /
`--firefox-port 0` (turn the like sources off).

**Handy extras**

- Test with no music playing: run a fake MPRIS player, then
  `ytm_bridge.py --player faketest --name Gumi`
  (`templates/fake-mpris-player.py` in the `flipper-zero-app-dev` skill).
- Extension end-to-end test:
  `YTM_TEST_BROWSER=/usr/bin/brave python3 extension/test/run_extension_test.py`
  - builds its own bridge on a free port, so your setup is untouched.

**While no PC is connected** the device shows a static "Waiting for PC" screen
(`draw_waiting` in `ytm_remote.c`): what the app is waiting for, in words. There
is no screensaver and the backlight is left to the firmware's own setting. BLE
keeps advertising, so the host can connect at any time.
