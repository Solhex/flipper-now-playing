# YT Music Remote (Flipper Zero <-> PC)

Shows the track currently playing on the PC (title, artist, album art, progress)
on the Flipper Zero screen and sends playback commands back — over Bluetooth,
no cable, no custom firmware.

Transport is the Flipper's BLE **serial profile** (the same GATT service the
official mobile app uses). The PC connects as BLE central with `bleak` and the
two exchange raw byte frames.

    PC (ytm_bridge.py)  <-- BLE serial -->  Flipper (ytm_remote.fap)

## Files

| File             | Role                                                      |
|------------------|-----------------------------------------------------------|
| `ytm_remote.c`   | Flipper app: parses frames, draws screen, sends commands  |
| `application.fam`| Manifest (`requires=["bt"]`, category `Bluetooth`)        |
| `ytm_bridge.py`  | PC side: media session -> BLE, buttons -> the player       |
| `windows_startup.py` | Windows: registers the bridge to start at logon        |
| `extension/`     | browser add-on: like state -> bridge, bridge -> click  |

## Build and flash

```bash
python3 -m ufbt            # -> dist/ytm_remote.fap  (expect Target 7, API 87.1)
python3 -m ufbt launch     # build + upload + run on the Flipper
```

`ufbt launch` needs write access to `/dev/ttyACM0`. If it says *Permission
denied*, install the udev rule (kept in `~/.local/share/flipper/` so it can be
restored after upgrades):

```bash
sudo install -Dm644 ~/.local/share/flipper/99-flipper.rules /etc/udev/rules.d/99-flipper.rules
sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=tty
```

## Run the bridge

```bash
python3 ytm_bridge.py --name Gumi          # Gumi = this Flipper's advertised BLE name
python3 ytm_bridge.py --list               # list MPRIS players
python3 ytm_bridge.py --player firefox     # pin a specific player
python3 ytm_bridge.py --name Gumi --hz 15  # push metadata 15x/second (default 20, max 25)
```

Requires `bleak` and `pillow` (`pip install bleak pillow`) and `playerctl`.
On the Flipper: launch **YT Music Remote** (Apps -> Bluetooth) and make sure
Bluetooth is ON.

### Windows

The Flipper app is the same build. The PC side reads Windows' own media session —
the one behind the volume flyout and the media keys — instead of MPRIS, so
Firefox and YouTube Music behave the way they do on Linux.

```bat
py -3 -m venv .venv
.venv\Scripts\pip install -r requirements-windows.txt
.venv\Scripts\python ytm_bridge.py --list     :: what Windows is playing right now
.venv\Scripts\python ytm_bridge.py --dump     :: the exact numbers that would go to the Flipper
.venv\Scripts\python ytm_bridge.py --pair     :: pair the Flipper once (Windows needs a bond)
.venv\Scripts\python ytm_bridge.py --name Gumi --player firefox
.venv\Scripts\python ytm_bridge.py --dump --firefox-port 0   :: skip the like state
```

`--firefox-port` (default 9222, `0` turns it off) is where the like state comes
from — see [Liked state](#liked-state-the-heart).

`--dump` and `--cmd` (one button, e.g. `--cmd p`) need no Flipper, which is the
quickest way to tell a bridge problem from a device problem. Load the browser
extension (below) to get the like state on any browser.
launch **YT Music Remote** (Apps -> Bluetooth), Bluetooth ON.

The pieces differ underneath:

| | Linux | Windows |
|---|---|---|
| media source | MPRIS via `playerctl` | Windows media sessions (WinRT / SMTC) |
| extra dependencies | `playerctl`, `yt-dlp` | `winrt-Windows.Media.Control`, `pycaw`, `yt-dlp` |
| play / pause / skip | `playerctl` | `WM_APPCOMMAND` posted at the browser window |
| volume | the player's own volume | that app's slider in the Windows mixer |
| pairing | BlueZ bond | `--pair` once, or Settings -> Bluetooth |

Firefox is why two of those rows exist. It publishes a Windows media session, so
title, artist and cover art arrive for free — but it ignores the session's
play/pause/next methods (they return "ok" and do nothing), so the bridge posts the
same `WM_APPCOMMAND` messages the media keys produce directly to the browser
window, which it does act on. It also reports no playback position for YouTube
Music, and its timeline can still carry the *previous* track's length, so the
bridge times the track itself (as on Linux) and only trusts a timeline once it has
seen the position move. Length still comes from `yt-dlp`, so without it the total
and the progress bar stay empty.

Two Windows-only consequences worth knowing:

- **Seek is ±10 s per press**, not 5 s: Firefox has no seek API, so the bridge
  types YouTube Music's own `l`/`h` shortcut at the tab.
- **Volume keys move that app's slider**, not a player-internal volume, and need
  `pycaw`; without it they are ignored (`Volume keys disabled` on startup).

## Track screen

Cover art on the left, then title, artist, play state, a progress bar and the
elapsed/total time. A title or artist wider than its column scrolls instead of
being cut off — one pixel per `SCROLL_MS` (70 ms, ~14 px/s), looping with a
`SCROLL_GAP` of 16 px, and restarting at the start of each new track. Lines that
fit are drawn statically, and the loop only redraws while something is actually
scrolling, since redrawing costs pixels the panel then ghosts.

An unknown track length shows as `--:--` rather than a wrong `0:00`.

## Buttons

| Key           | Action                     |
|---------------|----------------------------|
| OK            | play / pause               |
| OK (hold)     | like / unlike the track    |
| Up            | tap: volume up · hold (~0.3 s): next track      |
| Down          | tap: volume down · hold (~0.3 s): previous track |
| Left          | seek back 5 s              |
| Right         | seek forward 5 s           |
| Back (short)  | settings menu              |
| Back (long)   | exit the app               |

## Idle screen (screensaver)

While nothing is paired the app strokes twelve flowing lines instead of a static
waiting message. The curves hold their shape and a lit window of each slides along
it at a constant speed, so it reads as lines being drawn — and each frame only
changes the two end columns of each stroke (~8 px in total), which is what keeps
the panel from ghosting at 12 lines. The wavelengths divide the screen exactly, so
a stroke leaving the right edge carries straight on from the left rather than
disappearing (that is worth getting right: clamping instead of wrapping made the
lines vanish at the edge).

The knobs are at the top of `ytm_remote.c`:

| Constant        | Meaning                                              |
|-----------------|------------------------------------------------------|
| `WAVE_COUNT`    | how many lines                                       |
| `WAVE_FRAME_MS` | frame period (110 ms ≈ 9 fps; raise to calm down)    |
| `WAVE_WINDOW`   | pixels of each line lit at once (128 = whole lines)  |
| `WAVE_SPEED_MIN`| px of travel per frame (0.5)                         |
| `WAVE_SPEED_STEP`| spread of speeds across the lines                   |

Two things worth not re-learning: strokes move at a *constant* speed (a sine makes
each stroke stall at the ends of its swing, which looks like a frozen line), and
they are drawn *solid* (a dotted fading tail has to blink its dots on and off as
it travels, re-lighting ~30 px per line per frame — that ghosted).

After 10 s without a keypress it lets the backlight go out
(`SCREENSAVER_SLEEP_MS`); any key lights it again — and that key is swallowed, so
it can't act on a dark screen. The BLE profile keeps advertising the whole time,
so the PC can connect whenever it likes; when it does, the screen wakes with the
track.

## Always on

The app never exits by itself — only Settings → Exit app or a held Back leaves it.
On the PC side the bridge is a **user service**, so opening the PC is all it takes:

```bash
systemctl --user status ytm-remote.service      # is it up?
journalctl --user -u ytm-remote.service -f      # watch it connect
systemctl --user restart ytm-remote.service
```

It waits for a media player before doing anything (`--wait-for-player`), retries
forever, and recovers from the usual stuck states on its own: if the Flipper isn't
advertising it drops a stale BlueZ link first (a peripheral that BlueZ still thinks
is connected stops advertising) and then reconnects to the known address.

The unit lives at `~/.config/systemd/user/ytm-remote.service` and runs the bridge
from a dedicated venv at `~/.local/share/ytm-remote/venv`, so it doesn't depend on
any other Python install on the machine.

On Windows the same idea is one command — it writes a shortcut into your Startup
folder, so the bridge comes up at logon with no console window and no
administrator rights:

```bat
.venv\Scripts\python windows_startup.py install --name Gumi
.venv\Scripts\python windows_startup.py status        :: what it runs, and the log tail
.venv\Scripts\python windows_startup.py uninstall
```

The shortcut runs `pythonw.exe`, so nothing appears on screen; the bridge appends
its usual lines to `%LOCALAPPDATA%\ytm-remote\bridge.log` (that is what
`--log` is for — `pythonw.exe` has no stdout at all), and keeps
`status.json` beside it. Extra bridge options go through
`--args "--player firefox --hz 15"`.

There is no stale-link step on Windows — the BLE stack releases the link with the
process — so the bridge just rescans, then retries the cached address. If it
starts before the Bluetooth radio is up it says so and retries, rather than
exiting.

## Settings menu (Back)

| Row       | Controls                                              |
|-----------|-------------------------------------------------------|
| Backlight | Left/Right (or OK) step the LCD brightness 10–100 %   |
| Stay on   | Left = OFF, Right/OK = toggle (keeps the screen lit)  |
| Key binds | OK opens a page listing every button                  |
| Exit app  | OK quits                                              |

Up/Down are volume on a tap (5 % per press, with a `Volume NN%` overlay and bar
for 1.5 s) and skip track on a hold. The firmware reports a long press ~300 ms in
and then repeats every 150 ms, so after a hold the app ignores Up/Down for 1.5 s
(`SONG_COOLDOWN_MS`) — otherwise the trailing repeats would nudge the volume
right after the track changed.

## Liked state (the heart)

The track screen draws a small heart in the middle of the progress bar when the
current track is liked, and a cracked heart when it is not; an unknown state draws
nothing. Holding OK shows `Liked` / `Unliked` with the matching icon beside it.
Nothing in MPRIS or the Windows media session exposes that — only
the page knows — so the bridge asks the page. There are two ways to ask it, and
the bridge uses whichever is available:

### 1. The browser extension (Chrome, Edge, Firefox)

`extension/` is a Manifest V3 add-on. It reads the like button the site itself
draws and reports it to the bridge on `127.0.0.1:8765`; when the Flipper's like
button is held, the bridge answers the extension's long poll and the extension
**clicks the real button**. No browser launch flags, no debug port, and the same
folder runs in every Chromium browser.

- **Chrome / Edge**: `chrome://extensions` -> Developer mode -> *Load unpacked* ->
  pick the `extension` folder.
- **Firefox**: `about:debugging#/runtime/this-firefox` -> *Load Temporary
  Add-on* -> pick `extension/manifest.json`. Firefox only keeps temporary add-ons
  for the session unless the file is signed, so for a permanent setup either sign
  it or use option 2 below.

Check it with the bridge's own endpoints:

```bash
curl -s 127.0.0.1:8765/state     # liked / title / reports / polls / fresh
curl -s -X POST 127.0.0.1:8765/toggle    # what holding OK sends the page
```

`--dump` and `--cmd` work while the bridge is running: when port 8765 is already
owned they read the state from the running bridge instead of starting a second
server, and say so (`Using the running bridge on port 8765 for the like state`).
The state is labelled `(browser extension)` in `--dump` when that is where it came
from.

`extension/test/` holds the harness used to verify this: a mock page with the
site's like-button DOM, and a script that loads the extension into a headless
Chromium browser and drives the bridge
(`python extension/test/run_extension_test.py 50`).

### 2. Firefox's own debug port

Without the extension, Firefox started with `--remote-debugging-port` serves a
WebDriver BiDi agent, and the same state is read (and the button clicked) over
that:

```bat
.venv\Scripts\python windows_startup.py firefox   :: Desktop shortcut with the debug port
```

Firefox must be **started** with `--remote-debugging-port=9222` (the port belongs
to whichever instance of a profile starts first, so quit Firefox fully before
using that shortcut). The bridge then reads the like button's `aria-pressed` once
a second and presses the same button when you hold OK, which is why the heart
follows your own clicks in the browser too. `--ext-port 0` and
`--firefox-port 0` switch each source off.

Things worth knowing:

- **No port, no heart.** Without the debug port the bridge says
  `like state: unavailable (...)` and sends "unknown", which draws nothing —
  better a blank than a wrong heart.
- Firefox allows **one** debug session at a time. The running bridge holds it, so
  `--dump`/`--cmd` cannot open a second one; they say so and fall back to the
  keystroke path for like. A client that dies without ending its session leaves
  the port unusable until Firefox restarts (the bridge keeps retrying).
- Firefox speaks **WebDriver BiDi**, not Chrome's CDP: `/json/list` answers 404
  and the websocket *is* the `/session` handshake endpoint. `websocket-client` is
  the only extra dependency.

## Like (hold OK)

Neither MPRIS nor the Windows media session has a like action, so this presses
YouTube Music's own like button in the page — or, when the debug port is not
open, types its shortcut (`shift+=`) at the browser. That has consequences worth
knowing:

- **The YouTube Music tab has to be the one on screen.** A window's title is its
  active tab's title, so the bridge looks for a window named "… - YouTube Music",
  focuses it if needed, types the shortcut and gives focus back. If no window
  matches it does *nothing* and says so — firing blind would type a `+` into
  whatever else you have focused. Either way the device shows a toast with the
  result (`Like toggled` / `Open the YT Music tab`).
- On Windows the same keystroke is sent with `SendInput` after raising the
  browser window, and focus goes back to what you were using afterwards.
- On Linux it needs `swaymsg` to check the focused window, and **`vkkey`** to type: a small
  Wayland virtual-keyboard client at `~/.local/share/ytm-remote/bin/vkkey`, built
  from `src/vkkey.c` in that directory against `wayland-client` + `xkbcommon`
  (rebuild with `wayland-scanner` + `gcc`, no root needed). It uses
  `zwp_virtual_keyboard_manager_v1` — what sway advertises here — and sets the
  shift modifier through the protocol's `modifiers` request, because pressing the
  shift keycode does not work.
- The browser extension described under [Liked state](#liked-state-the-heart)
  works the same on Linux: it is the only route that does not depend on the
  window manager (`swaymsg`) or on typing at a window.
- Check the toast path without touching the buttons:
  `python3 ytm_bridge.py --name Gumi --toast "hello"` (needs the service stopped,
  since only one connection can hold the device).

Both settings persist in `/ext/apps_data/ytm_remote/settings.txt`, and the app
restores the Flipper's own brightness when it exits. While "Stay on" is ON the
app takes a backlight hold for as long as it is open; the hold is released on
exit. The brightness write goes through the notification service's private
settings struct (the public API cannot set LCD brightness) and is range-checked
against that struct's neighbours before every write — see the
`flipper-zero-app-dev` skill, `references/display-backlight.md`.

## BLE characteristics (the transport)

Service `8fe5b3d5-2e7f-4a98-2a48-7acc60fe0000`, characteristics `19ed82ae-ed21-4c9d-4145-…`:

| UUID tail | Role              | Properties                        |
|-----------|-------------------|-----------------------------------|
| `…228e62fe0000` | RX, PC → Flipper  | read, write, write-without-response |
| `…228e61fe0000` | TX, Flipper → PC  | read, **indicate**                |
| `…228e63fe0000` | flow control      | read, notify (peripheral RX credit) |
| `…228e64fe0000` | RPC status        | read, write, notify               |

The TX character *indicates*, and playback commands come back on it. The bridge
subscribes there (the `Connected. write=… notify=` line must show `…228e61…`);
subscribing to the flow-control or RPC-status character instead — both also
advertise notify — yields a link where metadata flows but buttons appear dead.

## Frame format (PC -> Flipper)

    [type:1][len:2 LE][payload:len]
      0x01 META: [state:1][pos:2 LE][dur:2 LE][title utf8]0[artist utf8]0
               the text is optional: a payload of exactly 5 bytes is a clock
               tick and the device keeps the title/artist it already has. The
               bridge only sends those short frames once the device has said it
               understands them (command byte 'k', sent once per connection),
               which is what keeps the per-tick frame inside one BLE write.
      0x02 ART : [w:1][h:1][1-bit rows, (w+7)/8 per row, LSB-first per byte]
      0x03 VOL : [volume:1]  percent, 0..100 — shows the volume overlay
      0x04 TOAST: [icon:1][text utf8] — a moment-long line; icon 0 none,
               1 heart, 2 broken heart (the LCD font has no heart glyph)
      0x05 LIKE: [state:1]   0 = not liked, 1 = liked, 2 = unknown

Flipper -> PC: a single command byte (`p` `n` `b` `-` `+` `u` `d`; `u`/`d` are
the volume keys). Unknown frame types and oversized payloads are dropped and the
parser resyncs on the next type byte, so the PC side can add frames without
breaking a build already on the device.

Adding a frame type is one entry in `FRAME_SPECS` (ytm_remote.c): it carries both
the payload bound and the handler, so the two cannot drift. Each handler reports
whether anything changed, which is what keeps a repeated 10 Hz frame from
redrawing the screen.

## Troubleshooting

- **Bridge scans and finds nothing.** The Flipper only advertises while it is
  *not* connected. A stale connection from an earlier run or a phone blocks it:
  `bluetoothctl disconnect <addr>`. The device is paired/bonded, so it
  reconnects on its own afterwards.
- **Only one bridge at a time.** A second central silently takes the serial
  link away from the first.
- **First write fails with `0x0E Unlikely Error`.** The serial profile is still
  coming up; the bridge retries with backoff and settles.
- **Wrong BLE name.** Pass `--name <substring>` or set `FLIPPER_NAME`.
- **`Bluetooth unavailable: Bluetooth radio is not powered on`.** Windows has the
  radio switched off (or a bridge started before the adapter came up). The bridge
  retries every few seconds; nothing else to do.
- **The Flipper is listed but connecting fails on Windows.** The serial profile
  expects a bonded device: run `ytm_bridge.py --pair` once, or pair it in
  Settings -> Bluetooth. `--pair` also works on Linux.
- **Windows: volume keys do nothing.** `pycaw` is not installed, or that app has
  no audio session yet (nothing has played since it started).
- **Windows: a terminal window flashes while music plays.** That was the
  `yt-dlp` length lookup: `pythonw.exe` has no console, so a console child gets a
  new one, i.e. a window. The bridge now starts it with `CREATE_NO_WINDOW`; if a
  terminal still flashes, the child holding it is whatever replaced `yt-dlp`.
- **Windows: nothing happens after logging in.** `windows_startup.py status`
  prints the exact command and the end of `bridge.log`; a bridge that was started
  hidden is otherwise invisible. If the log is empty, the shortcut's `pythonw.exe`
  path moved (reinstall after recreating the venv).
- **Buttons do nothing while metadata streams fine.** Check the
  `Connected. write=… notify=…` line: `notify` must be `…228e61fe0000`
  (the indicate-only TX char). Pointing it at the flow-control char `…228e63…`
  is silent but harmless-looking, which is exactly how it hid for a while.
- **Build fails with `__pycache__/...pyc: file format not recognized`.** `scons`
  links every file it finds beside the sources, so a stray `.pyc` (say, from
  running `python -m py_compile` in the project folder) is handed to the linker.
  Delete `__pycache__` and build again.
- **`ufbt launch` says `Application "YT Music Remote" has to be closed
  manually`.** The firmware refuses to close a running app over RPC or the CLI:
  press Back on the Flipper (long-press with this build), then launch again.
- **Wrong elapsed time.** Firefox reports neither `Position` nor `mpris:length`
  for YouTube Music, so the bridge estimates elapsed time itself. When it first
  sees a track it dates it from the artwork file Firefox writes at track start
  (`track_start_age()`), so joining a song mid-way still shows the true time; a
  player that *does* report a position (mpv, VLC, local files) is used verbatim.
  Track length comes from `yt-dlp` — without it the elapsed time still ticks but
  the total and the progress bar stay empty. Run `check_time.py` (below) to see
  whether the timer matches.

## Checking the timer

The elapsed time is an *estimate* (see above), so it needs a way to be wrong
loudly. The bridge publishes what it is feeding the device to
`~/.cache/ytm-remote/status.json` (once a second), and the checker compares that
against an independent estimate — the age of Firefox's per-track artwork file,
which is written when a track starts:

```bash
python3 ~/.local/share/ytm-remote/check_time.py            # one-shot report
python3 ~/.local/share/ytm-remote/check_time.py --watch 20 # watch both clocks move
```

It prints the track, the art age, the length, what the Flipper is being fed and
the drift, then a verdict: `MATCH` within 2s, otherwise which way it is off (and
that only means anything while playing — the art file keeps ageing while paused).

## Testing without real music

`~/.hermes/cache/scratch/mpris-fake/fake_mpris.py` (also shipped as
`templates/fake-mpris-player.py` in the `flipper-zero-app-dev` skill) serves a
fake MPRIS player named `faketest`, so the whole path can be exercised with no
media app playing:

```bash
export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
python3 fake_mpris.py /path/to/art.png &
python3 ytm_bridge.py --player faketest --name Gumi
```
