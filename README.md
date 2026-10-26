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
```

`--dump` and `--cmd` (one button, e.g. `--cmd p`) need no Flipper, which is the
quickest way to tell a bridge problem from a device problem. On the Flipper:
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

On Windows put a shortcut in `shell:startup` (`Win+R` -> `shell:startup`) or a
Task Scheduler job that runs at log on:

```bat
C:\path\to\.venv\Scripts\python.exe C:\path\to\ytm_bridge.py --name Gumi --player firefox --wait-for-player
```

A plain `python.exe` keeps a console window (and the log) around; swap in
`pythonw.exe` to hide it, and read `%LOCALAPPDATA%\ytm-remote\status.json`
instead. There is no stale-link step on Windows — the BLE stack releases the link
with the process — so the bridge just rescans, then retries the cached address.

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

## Like (hold OK)

Neither MPRIS nor the Windows media session has a like action, so this types
YouTube Music's own shortcut (`shift+=`) at the browser. That has consequences
worth knowing:

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
- **Buttons do nothing while metadata streams fine.** Check the
  `Connected. write=… notify=…` line: `notify` must be `…228e61fe0000`
  (the indicate-only TX char). Pointing it at the flow-control char `…228e63…`
  is silent but harmless-looking, which is exactly how it hid for a while.
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
