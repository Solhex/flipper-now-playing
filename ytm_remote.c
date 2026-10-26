/*
 * YT Music Remote — Flipper Zero app (Momentum / community firmware)
 *
 * Shows the track a PC is playing (YouTube Music over MPRIS) and sends playback
 * commands back. Transport is the Flipper's BLE serial profile: the PC-side
 * bridge (ytm_bridge.py, bleak) connects as BLE central and the two exchange
 * raw byte frames over the same GATT service the official mobile app uses.
 *
 * Wire format, PC -> Flipper (FRAME_SPECS below is the authoritative list):
 *   [type:1][len:2 LE][payload:len]
 *     0x01 META: [state:1][pos:2 LE][dur:2 LE][title utf8]\0[artist utf8]\0
 *                state: 0 = paused, 1 = playing; pos/dur in seconds
 *     0x02 ART : [w:1][h:1][1-bit rows, (w+7)/8 per row, LSB-first per byte]
 *     0x03 VOL : [volume:1]   0..100, shows the volume overlay
 *   Unknown types and oversized payloads are dropped, and a frame boundary
 *   lost to an overrun resyncs on the next type byte.
 *
 * Commands, Flipper -> PC: one byte each (CMD_TRANSPORT/CMD_VOLUME in ytm_bridge.py)
 *   'p' play/pause   'n' next   'b' previous   '-' seek back   '+' seek forward
 *   'u' volume up    'd' volume down
 *
 * Controls
 *   OK play/pause, Left/Right seek, Back (short) settings menu, Back (long)
 *   leave the app. Up/Down do double duty: tap = volume, hold = skip track
 *   (see SONG_COOLDOWN_MS).
 */

#include <furi.h>
#include <gui/gui.h>
#include <bt/bt_service/bt.h>
#include <profiles/serial_profile.h>
#include <notification/notification.h>
#include <notification/notification_messages.h>
#include <storage/storage.h>
#include <ytm_remote_icons.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

/* A bounded strnlen. The firmware API carries strnlen but has it disabled, and
 * the CI symbol check refuses any app that calls it — the local SDK build does
 * not catch this, it passed while CI failed. Going through memchr keeps this on
 * symbols the firmware exposes, and the compiler will not rewrite it back. */
static size_t bounded_len(const char* s, size_t max) {
    const char* end = memchr(s, '\0', max);
    return end ? (size_t)(end - s) : max;
}

#define TAG "YtmRemote"

#define TEXT_MAX       80 /* title/artist buffers */
#define META_MAX       320
#define ART_W_MAX      64
#define ART_H_MAX      64
#define ART_MAX_BYTES  (ART_H_MAX * (ART_W_MAX / 8)) /* 512 */
#define ART_PAYLOAD    (ART_MAX_BYTES + 2)
#define RX_STREAM_SIZE (META_MAX + ART_PAYLOAD + 8)
#define TX_QUEUE_LEN   8
#define TX_PACE_MS     30 /* at most one command per this many ms */

/* Up/Down are tap = volume, hold = skip track. The input service reports Long
 * ~300 ms into a press and then starts sending Repeats every 150 ms, so acting
 * on Long and ignoring Up/Down for a moment afterwards is what keeps a hold
 * from also nudging the volume. */
#define SONG_COOLDOWN_MS 1500

/* Idle screen: flowing lines while nothing is paired. Once the screen has been
 * on it for SCREENSAVER_SLEEP_MS without a keypress it lets the backlight go
 * out; any key wakes it again (and is swallowed, so it cannot act on a dark
 * screen).
 *
 * The panel ghosts when pixels change quickly, so each frame is kept almost
 * free: the curves hold their shape and only the lit part of each slides along
 * it, so just the two end columns change (~8 px per frame for all twelve lines,
 * against ~9000 when the curves also drifted). That budget is what pays for
 * having this many lines. Set WAVE_WINDOW to 128 for whole lines (still cheap:
 * the cost is in what changes, not what is lit). */
#define SCREENSAVER_SLEEP_MS 10000
#define WAVE_COUNT           12
#define WAVE_FRAME_MS        110 /* ~9 fps */
#define WAVE_WINDOW          56 /* px of each line lit at once */
#define WAVE_SPEED_MIN       0.5f /* px per frame */
#define WAVE_SPEED_STEP      0.2f /* four speed tiers across the lines */

/* Marquee: pause, scroll to the end, pause again, restart. Slow enough to read
 * and to keep the panel's ghosting down. */
#define SCROLL_MS      120 /* per pixel step, ~8 px/s */
#define SCROLL_HOLD_MS 2000 /* pause at the start and on the last portion */

#define FRAME_META  0x01
#define FRAME_ART   0x02
#define FRAME_VOL   0x03
#define FRAME_TOAST 0x04 /* a short line of text from the PC, shown briefly */

#define CMD_PLAYPAUSE 'p'
#define CMD_NEXT      'n'
#define CMD_PREV      'b'
#define CMD_SEEKBACK  '-'
#define CMD_SEEKFWD   '+'
#define CMD_VOLUP     'u'
#define CMD_VOLDOWN   'd'
#define CMD_CAPS      'k' /* sent once per connection: "I take clock-only META" */
#define CMD_LIKE      'l' /* hold OK: toggle like on the PC's YouTube Music */

#define TEXT_X     50 /* text column starts right of the cover art */
#define TEXT_COL_W 76 /* and is this wide */

#define VOL_SHOW_MS 1500 /* how long the volume overlay stays on screen */
#define TOAST_MAX     24 /* chars in a toast from the PC */
#define TOAST_SHOW_MS 1800

/* ---- display brightness ------------------------------------------------
 *
 * The public notification API cannot set LCD brightness (only backlight
 * on/off, the enforce-on/auto lock, and a forced-settings message the service
 * clears by itself), and furi_hal_light_set() is overwritten by the
 * notification service on every input event. What works — as used by
 * community clock apps — is to write the notification service's own setting
 * and ask it to re-apply. That needs its private layout: on Momentum mntm-012
 * (API 87.1) NotificationApp's `settings` member sits at offset 40.
 *
 * Re-verify after a firmware update, never guess:
 *   llvm-dwarfdump --debug-info ~/.ufbt/current/firmware.elf \
 *       | grep -B2 -A22 notification_app.h
 */
#define NOTIF_SETTINGS_OFFSET 40

typedef struct {
    uint8_t version;
    float display_brightness;
    float led_brightness;
    float speaker_volume;
    uint32_t display_off_delay_ms;
    int8_t contrast;
    bool vibro_on;
} NotifSettings;

static NotifSettings* notif_settings(NotificationApp* app) {
    return (NotifSettings*)((uint8_t*)app + NOTIF_SETTINGS_OFFSET);
}

/* Range-check the neighbours of the field we write: if a firmware update moved
 * the struct these read as garbage and we leave the value alone. */
static bool notif_settings_sane(const NotifSettings* s) {
    return s->version <= 16 && s->led_brightness >= 0.0f && s->led_brightness <= 1.0f &&
           s->speaker_volume >= 0.0f && s->speaker_volume <= 1.0f &&
           s->display_off_delay_ms <= 600000;
}

/* ---- persisted settings ------------------------------------------------ */

#define CFG_DIR  "/ext/apps_data/ytm_remote"
#define CFG_PATH CFG_DIR "/settings.txt"

/* One format string for both directions: the field order lives in exactly one
 * place, so load and save cannot drift apart. */
#define CFG_FMT "brightness=%d\nkeep_on=%d"

#define BRIGHT_MIN  10
#define BRIGHT_MAX  100
#define BRIGHT_STEP 10

typedef struct {
    uint8_t brightness; /* percent, BRIGHT_MIN..BRIGHT_MAX */
    bool keep_on; /* hold the backlight on while the app is open */
} Cfg;

typedef struct App App;

/* One entry per frame type drives both the payload bound and the handler, so a
 * new frame type is one line here (plus the PC side). max_len must never
 * exceed the Parser buffer below. */
typedef struct {
    uint8_t type;
    uint16_t max_len;
    bool (*handler)(App* app, const uint8_t* payload, uint16_t len);
} FrameSpec;

typedef struct {
    const FrameSpec* spec; /* resolved once the length is known */
    uint16_t need;
    uint16_t got;
    uint8_t buf[ART_PAYLOAD];
    uint8_t stage;
} Parser;

typedef enum { ParserType, ParserLenLo, ParserLenHi, ParserPayload } ParserStage;

struct App {
    FuriMutex* mutex;

    char title[TEXT_MAX];
    char artist[TEXT_MAX];
    bool playing;
    bool have_meta;
    uint16_t pos;
    uint16_t dur;

    uint8_t art_w, art_h;
    uint8_t art[ART_MAX_BYTES];
    bool have_art;

    Cfg cfg;
    uint8_t sel;
    bool settings_open;
    bool tmp_hold; /* backlight held on only because the menu is open */

    uint32_t song_cooldown; /* tick until which Up/Down is ignored */

    uint8_t volume; /* last volume reported by the PC, 0..100 */
    bool have_vol; /* PC has reported a volume at least once */
    uint32_t vol_until; /* tick until which the overlay is drawn */
    bool vol_clear_pending; /* still owe one redraw to erase the overlay */

    char toast[TOAST_MAX + 1]; /* last message from the PC (e.g. the like result) */
    uint32_t toast_until;
    bool toast_clear_pending;
    bool help_open; /* the key-bind page is up, over the settings menu */
    uint8_t top; /* first visible settings row (the list scrolls) */

    Bt* bt;
    FuriHalBleProfileBase* ble;
    FuriStreamBuffer* rx;
    FuriStreamBuffer* tx; /* outbound command bytes, sent by the app thread */
    Gui* gui;
    ViewPort* vp;
    NotificationApp* notif;
    bool held; /* enforce-on lock currently taken */
    bool have_orig;
    float orig_brightness;
    volatile bool running;
    volatile bool rx_overrun; /* bytes were dropped: the parser needs resyncing */
    volatile BtStatus bt_status;
    bool announced; /* capability byte sent for this connection */

    uint32_t scroll; /* title/artist marquee offset, in pixels */
    uint32_t scroll_next; /* when the next step (or pause) is due */
    uint16_t scroll_max; /* how far the longest line has to travel */
    bool scroll_end_hold; /* the last portion has had its pause */
    volatile bool scrolling; /* a line is too wide: the loop must keep redrawing */

    float wave_head[WAVE_COUNT]; /* where each stroke currently is */
    uint32_t wave_tick;
    uint32_t idle_since; /* last keypress, for the screensaver's sleep timer */
    bool slept; /* screensaver let the backlight go out */
};

/* ---------------- backlight ---------------- */

static void notif_write_brightness(App* app, float value) {
    if(!app->notif) return;
    NotifSettings* s = notif_settings(app->notif);
    if(notif_settings_sane(s)) {
        s->display_brightness = value;
    }
}

/* Write the setting, then make the service drive the backlight from it. */
static void backlight_apply(App* app) {
    if(!app->notif) return;
    notif_write_brightness(app, (float)app->cfg.brightness / 100.0f);
    notification_message(app->notif, &sequence_display_backlight_on);
}

static void backlight_hold(App* app, bool on) {
    if(!app->notif || on == app->held) return;
    notification_message(
        app->notif,
        on ? &sequence_display_backlight_enforce_on : &sequence_display_backlight_enforce_auto);
    app->held = on;
}

/* ---- screensaver sleep ----
 *
 * The "Stay on" setting takes a backlight hold, and a hold beats a plain
 * "backlight off" message — so sleeping the screen means dropping the hold first
 * and re-taking it on wake.
 */
static void screen_sleep(App* app) {
    if(app->slept || !app->notif) return;
    app->slept = true;
    backlight_hold(app, false);
    notification_message(app->notif, &sequence_display_backlight_off);
}

static void screen_wake(App* app) {
    app->idle_since = furi_get_tick();
    if(!app->slept) return;
    app->slept = false;
    backlight_hold(app, app->cfg.keep_on);
    if(app->notif) notification_message(app->notif, &sequence_display_backlight_on);
    if(app->vp) view_port_update(app->vp);
}

/* ---------------- settings storage ---------------- */

static void cfg_save(App* app);

static void cfg_load(App* app) {
    app->cfg.brightness = 70;
    app->cfg.keep_on = true;

    Storage* storage = furi_record_open(RECORD_STORAGE);
    File* file = storage_file_alloc(storage);
    if(storage_file_open(file, CFG_PATH, FSAM_READ, FSOM_OPEN_EXISTING)) {
        char buf[64];
        size_t n = storage_file_read(file, buf, sizeof(buf) - 1);
        buf[n] = 0;
        int b = 0, k = 1;
        /* a file written by an older build may carry extra fields: extra lines
         * after the ones we ask for are simply left unread */
        if(sscanf(buf, CFG_FMT, &b, &k) >= 1) {
            if(b >= BRIGHT_MIN && b <= BRIGHT_MAX) app->cfg.brightness = (uint8_t)b;
            app->cfg.keep_on = (k != 0);
        }
    }
    storage_file_close(file);
    storage_file_free(file);
    furi_record_close(RECORD_STORAGE);

    FURI_LOG_I(TAG, "cfg: brightness=%u keep_on=%d", app->cfg.brightness, app->cfg.keep_on);
}

static void cfg_save(App* app) {
    Storage* storage = furi_record_open(RECORD_STORAGE);
    storage_common_mkdir(storage, CFG_DIR);
    File* file = storage_file_alloc(storage);
    if(storage_file_open(file, CFG_PATH, FSAM_WRITE, FSOM_CREATE_ALWAYS)) {
        char buf[64];
        int n = snprintf(
            buf,
            sizeof(buf),
            CFG_FMT "\n",
            (int)app->cfg.brightness,
            app->cfg.keep_on ? 1 : 0);
        if(storage_file_write(file, buf, (size_t)n) != (size_t)n) {
            FURI_LOG_W(TAG, "settings write failed");
        }
    }
    storage_file_close(file);
    storage_file_free(file);
    furi_record_close(RECORD_STORAGE);
}

/* ---------------- settings menu ---------------- */

typedef enum { SettingBacklight, SettingStayOn, SettingKeys, SettingExit, SettingCount } SettingRow;

typedef enum { ActStep, ActToggle, ActInfo, ActQuit } ActKind;

typedef struct {
    ActKind kind;
    const char* label;
} SettingDef;

/* Row semantics live in exactly two places: this table, and the small switches
 * below that pick which value a row edits. */
static const SettingDef SETTINGS[SettingCount] = {
    {ActStep, "Backlight"},
    {ActToggle, "Stay on"},
    {ActInfo, "Key binds"},
    {ActQuit, "Exit app"},
};

/* Three rows fit under the title with the footer still legible (glyphs are 11 px
 * tall); the rest scroll, with a bar on the right so it is obvious there is more.
 * Five rows at 11 px spacing overlapped the title and the footer. */
#define SETTINGS_ROW_Y    20 /* first visible row's baseline */
#define SETTINGS_ROW_STEP 12 /* 11 px glyphs plus a 1 px gap */
#define SETTINGS_VISIBLE  3
#define SETTINGS_BAR_X    124 /* scroll bar track x, 3 px wide */

static void settings_value(App* app, SettingRow row, char* out, size_t n) {
    switch(SETTINGS[row].kind) {
    case ActStep:
        snprintf(out, n, "< %u%% >", (unsigned)app->cfg.brightness);
        break;
    case ActToggle:
        snprintf(out, n, "[%s]", app->cfg.keep_on ? "ON" : "OFF");
        break;
    case ActInfo:
        snprintf(out, n, ">"); /* opens a page rather than changing a value */
        break;
    default:
        out[0] = 0; /* ActQuit shows no value */
        break;
    }
}

/* dir: -1 = Left, +1 = Right, 0 = OK (toggle / activate) */
static void settings_act(App* app, int dir) {
    SettingRow row = (SettingRow)app->sel;

    switch(SETTINGS[row].kind) {
    case ActStep: {
        int step = (dir < 0) ? -BRIGHT_STEP : BRIGHT_STEP;
        int v = (int)app->cfg.brightness + step;
        if(v < BRIGHT_MIN) v = BRIGHT_MIN;
        if(v > BRIGHT_MAX) v = BRIGHT_MAX;
        app->cfg.brightness = (uint8_t)v;
        backlight_apply(app);
        cfg_save(app);
        break;
    }
    case ActToggle:
        app->cfg.keep_on = (dir == 0) ? !app->cfg.keep_on : (dir > 0);
        backlight_hold(app, app->cfg.keep_on);
        cfg_save(app);
        break;
    case ActInfo:
        if(dir == 0) app->help_open = true; /* the key-bind page */
        break;
    case ActQuit:
        if(dir == 0) app->running = false;
        break;
    }
}

static void settings_open_view(App* app) {
    app->settings_open = true;
    app->sel = SettingBacklight;
    app->top = 0;
    /* the menu must stay readable even with "Stay on" turned off */
    if(!app->held) {
        backlight_hold(app, true);
        app->tmp_hold = true;
    }
}

static void settings_close_view(App* app) {
    app->settings_open = false;
    if(app->tmp_hold) {
        backlight_hold(app, app->cfg.keep_on);
        app->tmp_hold = false;
    }
}

static void settings_key(App* app, InputEvent* event) {
    switch(event->key) {
    case InputKeyUp:
        if(app->sel > 0) app->sel--;
        if(app->sel < app->top) app->top = app->sel; /* scroll to follow it */
        break;
    case InputKeyDown:
        if(app->sel < SettingCount - 1) app->sel++;
        if(app->sel >= app->top + SETTINGS_VISIBLE) app->top = app->sel - SETTINGS_VISIBLE + 1;
        break;
    case InputKeyLeft:
        settings_act(app, -1);
        break;
    case InputKeyRight:
        settings_act(app, +1);
        break;
    case InputKeyOk:
        settings_act(app, 0);
        break;
    case InputKeyBack:
        settings_close_view(app);
        break;
    default:
        break;
    }
}

/* ---------------- input ---------------- */

static void send_cmd(App* app, uint8_t c) {
    /* Nothing to send to a PC that is not there — and a stale byte would be
     * delivered the moment one connects (the app runs for days). */
    if(app->bt_status != BtStatusConnected) return;
    /* Never talk to the BLE stack straight from the input callback: each
     * indication has to be acknowledged before the next one goes out, so a
     * congested stack blocks the caller — and the input callback runs on the
     * GUI thread, which freezes the whole UI when that happens. Queue the byte
     * (non-blocking, dropped if full) and let the app thread pace it out. */
    furi_stream_buffer_send(app->tx, &c, 1, 0);
}

/* Up/Down: tap = volume, hold = skip track. */
static void up_down_key(App* app, InputKey key, InputType type) {
    if((int32_t)(app->song_cooldown - furi_get_tick()) > 0) {
        return; /* just skipped a track: swallow the trailing repeats */
    }
    bool up = (key == InputKeyUp);
    if(type == InputTypeLong) {
        send_cmd(app, up ? CMD_NEXT : CMD_PREV);
        app->song_cooldown = furi_get_tick() + SONG_COOLDOWN_MS;
    } else if(type == InputTypeShort) {
        send_cmd(app, up ? CMD_VOLUP : CMD_VOLDOWN);
    }
}

static void input_callback(InputEvent* event, void* context) {
    App* app = context;

    bool was_asleep = app->slept;
    screen_wake(app); /* any key wakes the screen... */
    if(was_asleep) return; /* ...and is swallowed, so it cannot act blind */

    if(event->key == InputKeyBack && event->type == InputTypeLong) {
        app->running = false; /* hold Back always leaves the app */
        return;
    }
    if(event->type != InputTypeShort && event->type != InputTypeLong &&
       event->type != InputTypeRepeat) {
        return; /* Press/Release carry no action of their own */
    }

    if(app->help_open) {
        /* the key-bind page: any of Back/OK returns to the menu */
        if(event->key == InputKeyBack || event->key == InputKeyOk) app->help_open = false;
        view_port_update(app->vp);
        return;
    }

    if(app->settings_open) {
        settings_key(app, event);
        view_port_update(app->vp);
        return;
    }

    switch(event->key) {
    case InputKeyOk:
        /* tap plays/pauses, holding it toggles like on the PC. Repeats must be
         * ignored: a hold sends Long once and then Repeat every 150 ms, so acting
         * on those flipped the like on and off for as long as the key was held. */
        if(event->type == InputTypeRepeat) break;
        send_cmd(app, event->type == InputTypeLong ? CMD_LIKE : CMD_PLAYPAUSE);
        break;
    case InputKeyUp:
    case InputKeyDown:
        up_down_key(app, event->key, event->type);
        break;
    case InputKeyLeft:
        send_cmd(app, CMD_SEEKBACK);
        break;
    case InputKeyRight:
        send_cmd(app, CMD_SEEKFWD);
        break;
    case InputKeyBack:
        settings_open_view(app);
        break;
    default:
        break;
    }
}

/* ---------------- drawing ---------------- */

static void fmt_time(char* out, size_t n, uint16_t secs) {
    snprintf(out, n, "%u:%02u", (unsigned)(secs / 60), (unsigned)(secs % 60));
}

/* Draw text at x, scrolling it leftwards when it is wider than maxw.
 *
 * The canvas has no clip rectangle, so characters are drawn one at a time and
 * any that reach outside the column are wiped afterwards (see the gutter in
 * draw_now_playing) rather than being dropped, which would make a glyph pop in
 * and out at the edge instead of sliding. */
/* Advance width of one glyph, the same way the drawing loop below advances. */
static int glyph_w(Canvas* canvas, char c) {
    char glyph[2] = {c, 0};
    return canvas_string_width(canvas, glyph);
}

/* Sum of the glyph advances — what the drawing below actually lays out.
 * canvas_string_width() on the whole string can disagree with this (trailing
 * advance, wide glyphs), and when it does the marquee stops in the wrong place:
 * it scrolled past the end of the title, leaving blank space beside the last
 * word. Measure the way you draw. */
static int text_w(Canvas* canvas, const char* text) {
    int sum = 0;
    for(const char* p = text; *p; p++) {
        sum += glyph_w(canvas, *p);
    }
    return sum;
}

static void
    draw_scrolling(Canvas* canvas, int x, int y, int maxw, const char* text, uint32_t scroll) {
    int width = text_w(canvas, text);
    if(width <= maxw) {
        canvas_draw_str(canvas, x, y, text);
        return;
    }

    /* Each line stops at its own end, so a short artist holds on its last words
     * while a longer title keeps travelling. */
    uint32_t limit = (uint32_t)(width - maxw);
    int cx = x - (int)(scroll > limit ? limit : scroll);
    for(const char* p = text; *p; p++) {
        char glyph[2] = {*p, 0};
        int cw = canvas_string_width(canvas, glyph);
        if(cx + cw > x && cx < x + maxw) canvas_draw_str(canvas, cx, y, glyph);
        cx += cw;
        if(cx > x + maxw) break;
    }
}

/* How far a line has to travel to show its end, 0 if it fits already. Measured
 * with text_w() so the driver and the drawing agree on where the end is. */
static int scroll_overflow(Canvas* canvas, const char* text, int maxw) {
    int over = text_w(canvas, text) - maxw;
    return over > 0 ? over : 0;
}

/* Outlined bar with an inset fill — used for progress and volume alike. */
static void
    draw_bar(Canvas* canvas, int x, int y, int w, int h, uint32_t done, uint32_t total) {
    canvas_draw_frame(canvas, x, y, w, h);
    if(total == 0 || done > total) return;
    /* Fill flush inside the 1 px frame. A 2 px inset ate a quarter of an 8 px bar,
     * which made the progress itself look thin. */
    int fill = (int)((uint64_t)done * (w - 2) / total);
    if(fill > 0) canvas_draw_box(canvas, x + 1, y + 1, fill, h - 2);
}

static void draw_settings(Canvas* canvas, App* app) {
    canvas_set_font(canvas, FontPrimary);
    canvas_draw_str(canvas, 2, 9, "Settings");
    canvas_set_font(canvas, FontSecondary);

    for(uint8_t j = 0; j < SETTINGS_VISIBLE; j++) {
        uint8_t i = app->top + j;
        if(i >= SettingCount) break;
        int y = SETTINGS_ROW_Y + j * SETTINGS_ROW_STEP;
        char value[24];
        settings_value(app, i, value, sizeof(value));

        if(i == app->sel) {
            canvas_draw_box(canvas, 0, y - 10, SETTINGS_BAR_X - 2, SETTINGS_ROW_STEP);
            canvas_set_color(canvas, ColorWhite);
        }
        canvas_draw_str(canvas, 3, y, SETTINGS[i].label);
        if(value[0]) {
            canvas_draw_str_aligned(canvas, SETTINGS_BAR_X - 6, y, AlignRight, AlignBottom, value);
        }
        canvas_set_color(canvas, ColorBlack);
    }

    /* Scroll bar, only when the list does not fit — a full-height bar would
     * suggest scrolling where there is none. */
    if(SettingCount > SETTINGS_VISIBLE) {
        int track_y = SETTINGS_ROW_Y - 9;
        int track_h = SETTINGS_VISIBLE * SETTINGS_ROW_STEP - 2;
        canvas_draw_frame(canvas, SETTINGS_BAR_X, track_y, 3, track_h);
        int thumb_h = track_h * SETTINGS_VISIBLE / SettingCount;
        if(thumb_h < 6) thumb_h = 6;
        int thumb_y = track_y +
                      (track_h - thumb_h) * app->top / (SettingCount - SETTINGS_VISIBLE);
        canvas_draw_box(canvas, SETTINGS_BAR_X + 1, thumb_y + 1, 1, thumb_h - 2);
    }

    canvas_draw_str(canvas, 2, 63, "Back: close   Hold: exit");
}

static void draw_waiting(Canvas* canvas, App* app) {
    bool connected = (app->bt_status == BtStatusConnected);

    canvas_set_font(canvas, FontPrimary);
    canvas_draw_str_aligned(canvas, 64, 22, AlignCenter, AlignBottom, "YT Music Remote");
    canvas_set_font(canvas, FontSecondary);
    canvas_draw_str_aligned(
        canvas,
        64,
        38,
        AlignCenter,
        AlignBottom,
        connected ? "Waiting for PC data..." : "Connect from ytm_bridge");
    canvas_draw_str_aligned(
        canvas,
        64,
        54,
        AlignCenter,
        AlignBottom,
        connected ? "BT: connected" : "BT: advertising");
}

/* The now-playing screen: cover, progress bar, times, and the marquee. */
static void draw_now_playing(Canvas* canvas, App* app) {
    if(!app->have_meta && !app->have_art) {
        draw_waiting(canvas, app);
        return;
    }

    if(app->have_art && app->art_w > 0 && app->art_h > 0) {
        canvas_draw_xbm(canvas, 2, 2, app->art_w, app->art_h, app->art);
    } else {
        canvas_draw_frame(canvas, 2, 2, 44, 44);
        canvas_set_font(canvas, FontSecondary);
        canvas_draw_str_aligned(canvas, 24, 26, AlignCenter, AlignBottom, "no art");
    }

    int tx = TEXT_X;
    int tw = TEXT_COL_W;
    canvas_set_font(canvas, FontSecondary);

    const char* title = app->title[0] ? app->title : "(untitled)";
    draw_scrolling(canvas, tx, 12, tw, title, app->scroll);
    draw_scrolling(canvas, tx, 24, tw, app->artist, app->scroll);
    /* Only the marquee needs repeated redraws — and redrawing costs pixels the
     * panel then ghosts, so the loop is told to animate only when a line really
     * does not fit. */
    int over_title = scroll_overflow(canvas, title, tw);
    int over_artist = scroll_overflow(canvas, app->artist, tw);
    app->scroll_max = (uint16_t)(over_title > over_artist ? over_title : over_artist);
    app->scrolling = (app->scroll_max > 0);

    if(app->scrolling) {
        /* No clip rectangle on this canvas: blank the gap between the art and
         * the text column so a partly visible glyph cannot be drawn over the
         * cover. That gap is empty background, so this erases nothing else. */
        int gutter_x = 2 + (app->art_w > 0 ? app->art_w : 44);
        canvas_set_color(canvas, ColorWhite);
        canvas_draw_box(canvas, gutter_x, 2, tx - gutter_x, 26);
        canvas_set_color(canvas, ColorBlack);
    }

    canvas_draw_str(canvas, tx, 36, app->playing ? "Playing" : "Paused");

    if(app->have_vol) {
        char vol[8];
        snprintf(vol, sizeof(vol), "%u%%", (unsigned)app->volume);
        canvas_draw_str_aligned(canvas, 126, 36, AlignRight, AlignBottom, vol);
    }

    /* The bar sits under the cover art and takes whatever height is left before
     * the time text. Both gaps are rows and are the knobs to turn; the text gap
     * never goes below 1, because 0 puts the bar against the digits. The cover is
     * measured rather than assumed (it starts at y 2 and is art_h tall), as is the
     * font, since the text baseline is 63. */
    const int art_gap = 2;  /* blank rows between the cover art and the bar */
    const int text_gap = 1; /* blank rows between the bar and the time text */
    int text_top = 63 - (int)canvas_current_font_height(canvas) + 1;
    int art_bottom = 2 + (app->art_h > 0 ? app->art_h : 44) - 1;
    int bar_y = art_bottom + 1 + art_gap;
    int h = text_top - text_gap - bar_y; /* the bar fills what is left */
    if(h > 0) draw_bar(canvas, 2, bar_y, 124, h, app->pos, app->dur);

    char elapsed[12], total[12];
    fmt_time(elapsed, sizeof(elapsed), app->pos);
    /* An unknown length is not 0:00 — say so rather than display a wrong time. */
    if(app->dur) {
        fmt_time(total, sizeof(total), app->dur);
    } else {
        snprintf(total, sizeof(total), "--:--");
    }
    canvas_draw_str(canvas, 2, 63, elapsed);
    canvas_draw_str_aligned(canvas, 126, 63, AlignRight, AlignBottom, total);
}

/* The key-bind reference, opened from the settings menu. Terse on purpose: the
 * whole page has to fit above the bottom edge. */
static void draw_help(Canvas* canvas, App* app) {
    (void)app;
    static const char* const LINES[] = {
        "OK         play / pause",
        "OK hold    like / unlike",
        "Up/Down    volume / skip",
        "Left/Right seek 5 s",
        "Back       this menu",
        "Back hold  exit app",
    };

    canvas_set_font(canvas, FontPrimary);
    canvas_draw_str(canvas, 3, 8, "Keys");
    canvas_set_font(canvas, FontSecondary);
    for(size_t i = 0; i < sizeof(LINES) / sizeof(LINES[0]); i++) {
        canvas_draw_str(canvas, 3, 17 + (int)i * 9, LINES[i]);
    }
}

/* A line of text from the PC (how the like went, say), over everything. */
static void draw_toast(Canvas* canvas, App* app) {
    const int x = 3;
    const int y = 20;
    const int w = 122;

    canvas_draw_box(canvas, x, y, w, 24);
    canvas_set_color(canvas, ColorWhite);
    canvas_draw_frame(canvas, x, y, w, 24);
    canvas_set_font(canvas, FontSecondary);
    canvas_draw_str_aligned(canvas, 64, y + 15, AlignCenter, AlignBottom, app->toast);
    canvas_set_color(canvas, ColorBlack);
}

static void draw_volume_overlay(Canvas* canvas, App* app) {
    const int y0 = 18;
    char text[20];

    canvas_draw_box(canvas, 12, y0, 104, 28);
    canvas_set_color(canvas, ColorWhite);
    canvas_draw_frame(canvas, 12, y0, 104, 28);

    canvas_set_font(canvas, FontPrimary);
    snprintf(text, sizeof(text), "Volume %u%%", (unsigned)app->volume);
    canvas_draw_str_aligned(canvas, 64, y0 + 14, AlignCenter, AlignBottom, text);

    canvas_set_font(canvas, FontSecondary);
    draw_bar(canvas, 22, y0 + 18, 84, 7, app->volume, 100);

    canvas_set_color(canvas, ColorBlack);
}

/* Height of one line at x. Fixed low-frequency sines: long lazy curves, no two
 * alike — four amplitudes crossed with three whole wavelengths. The wavelengths
 * divide the screen exactly, so a stroke can run off one edge and carry straight
 * on from the other instead of vanishing at the edge. */
static int wave_y(uint8_t wave, int x) {
    float amplitude = 7.0f + (float)(wave % 4) * 5.0f;
    float cycles = 1.0f + (float)(wave / 4);
    float frequency = 6.2831853f * cycles / 128.0f;
    return 32 + (int)(amplitude * sinf(frequency * (float)x + (float)wave * 0.55f));
}

/* Px per frame for one stroke: four speed tiers, so the lines never move in
 * lockstep. Constant speed, deliberately not a sine — a sinusoidal position
 * stalls at the ends of its swing and the line looks frozen. */
static float wave_speed(uint8_t wave) {
    return WAVE_SPEED_MIN + (float)(wave % 4) * WAVE_SPEED_STEP;
}

/* Idle screen: lines being stroked along, advanced by the app loop.
 *
 * Only WAVE_WINDOW pixels of each line are lit and they slide along it: that is
 * what reads as a line being drawn, and it keeps each frame nearly free, so the
 * panel does not ghost. A dotted tail would look better still but re-lights ~30
 * px per line per frame — the dots have to blink on and off as they travel. */
static void draw_screensaver(Canvas* canvas, App* app) {
    for(uint8_t wave = 0; wave < WAVE_COUNT; wave++) {
        int head = (int)app->wave_head[wave];

        for(int i = 0; i < WAVE_WINDOW; i++) {
            int x = head - i;
            if(x < 0) x += 128; /* wraps around: the stroke stays continuous */
            canvas_draw_dot(canvas, x, wave_y(wave, x));
        }
    }
}

static void draw_callback(Canvas* canvas, void* context) {
    App* app = context;
    furi_mutex_acquire(app->mutex, FuriWaitForever);

    canvas_clear(canvas);
    if(app->help_open) {
        draw_help(canvas, app);
    } else if(app->settings_open) {
        draw_settings(canvas, app);
    } else if(app->bt_status != BtStatusConnected) {
        draw_screensaver(canvas, app);
    } else {
        draw_now_playing(canvas, app);
        if((int32_t)(app->vol_until - furi_get_tick()) > 0) {
            draw_volume_overlay(canvas, app);
        }
        if((int32_t)(app->toast_until - furi_get_tick()) > 0) {
            draw_toast(canvas, app);
        }
    }

    furi_mutex_release(app->mutex);
}

/* ---------------- frame parsing ---------------- */

/* Each handler updates the shared state and reports whether anything changed:
 * the PC pushes metadata ~10x/second, so an unchanged frame must not redraw. */

static bool handle_meta(App* app, const uint8_t* b, uint16_t len) {
    if(len < 5) return false;

    bool playing = b[0] != 0;
    uint16_t pos = (uint16_t)b[1] | ((uint16_t)b[2] << 8);
    uint16_t dur = (uint16_t)b[3] | ((uint16_t)b[4] << 8);

    /* A frame with no text (len == 5) is a clock tick: the PC resends the
     * title/artist only when they change, which keeps the per-tick frame small
     * enough to travel in a single BLE write. Keep the text we already have. */
    bool have_text = (len > 5);
    char title[TEXT_MAX] = {0};
    char artist[TEXT_MAX] = {0};

    if(have_text) {
        const char* p = (const char*)(b + 5);
        uint16_t rem = len - 5;

        size_t tl = bounded_len(p, rem);
        if(tl >= sizeof(title)) tl = sizeof(title) - 1;
        memcpy(title, p, tl);

        if(tl + 1 < rem) {
            const char* q = p + tl + 1;
            size_t rl = rem - tl - 1;
            size_t al = bounded_len(q, rl);
            if(al >= sizeof(artist)) al = sizeof(artist) - 1;
            memcpy(artist, q, al);
        }
    }

    furi_mutex_acquire(app->mutex, FuriWaitForever);
    /* Note the position changes on every tick, so a new track is detected on the
     * text alone — resetting the marquee on `changed` would pin it to column 0. */
    bool text_changed =
        have_text && (strcmp(app->title, title) != 0 || strcmp(app->artist, artist) != 0);
    bool changed = !app->have_meta || app->playing != playing || app->pos != pos ||
                   app->dur != dur || text_changed;
    if(changed) {
        app->playing = playing;
        app->pos = pos;
        app->dur = dur;
        if(have_text) {
            memcpy(app->title, title, sizeof(title));
            memcpy(app->artist, artist, sizeof(artist));
        }
        if(text_changed) {
            /* new track: show the start of it for a moment before scrolling */
            app->scroll = 0;
            app->scroll_end_hold = false;
            app->scroll_next = furi_get_tick() + SCROLL_HOLD_MS;
        }
        app->have_meta = true;
    }
    furi_mutex_release(app->mutex);
    return changed;
}

static bool handle_art(App* app, const uint8_t* b, uint16_t len) {
    if(len < 2) return false;

    uint8_t w = b[0];
    uint8_t h = b[1];
    if(w == 0 || h == 0 || w > ART_W_MAX || h > ART_H_MAX) return false;

    uint16_t row = (w + 7) / 8;
    uint32_t need = (uint32_t)row * h;
    if((uint32_t)(len - 2) < need) return false;

    furi_mutex_acquire(app->mutex, FuriWaitForever);
    bool changed = !app->have_art || app->art_w != w || app->art_h != h ||
                   memcmp(app->art, b + 2, need) != 0;
    if(changed) {
        /* store tightly packed: canvas_draw_xbm expects (w+7)/8 bytes per row */
        memcpy(app->art, b + 2, need);
        app->art_w = w;
        app->art_h = h;
        app->have_art = true;
    }
    furi_mutex_release(app->mutex);
    return changed;
}

/* A short message from the PC — how the like went, say. Shown for a moment;
 * the loop clears it, like the volume overlay. */
static bool handle_toast(App* app, const uint8_t* b, uint16_t len) {
    if(len == 0 || len > TOAST_MAX) return false;

    furi_mutex_acquire(app->mutex, FuriWaitForever);
    memcpy(app->toast, b, len);
    app->toast[len] = 0;
    app->toast_until = furi_get_tick() + TOAST_SHOW_MS;
    app->toast_clear_pending = true;
    furi_mutex_release(app->mutex);
    return true;
}

/* Always redraws: a volume frame means someone is adjusting, and refreshing the
 * overlay deadline is what keeps it on screen. */
static bool handle_vol(App* app, const uint8_t* b, uint16_t len) {
    if(len < 1) return false;
    uint8_t v = b[0] > 100 ? 100 : b[0];

    furi_mutex_acquire(app->mutex, FuriWaitForever);
    app->volume = v;
    app->have_vol = true;
    app->vol_until = furi_get_tick() + VOL_SHOW_MS;
    app->vol_clear_pending = true;
    furi_mutex_release(app->mutex);
    return true;
}

static const FrameSpec FRAME_SPECS[] = {
    {FRAME_META, META_MAX, handle_meta},
    {FRAME_ART, ART_PAYLOAD, handle_art},
    {FRAME_VOL, 1, handle_vol},
    {FRAME_TOAST, TOAST_MAX, handle_toast},
};

static const FrameSpec* frame_spec(uint8_t type) {
    for(size_t i = 0; i < COUNT_OF(FRAME_SPECS); i++) {
        if(FRAME_SPECS[i].type == type) return &FRAME_SPECS[i];
    }
    return NULL;
}

static void parser_byte(App* app, Parser* p, uint8_t b) {
    switch(p->stage) {
    case ParserType:
        p->spec = frame_spec(b);
        p->stage = ParserLenLo;
        break;
    case ParserLenLo:
        p->need = b;
        p->stage = ParserLenHi;
        break;
    case ParserLenHi:
        p->need |= (uint16_t)b << 8;
        p->got = 0;
        /* unknown type, empty payload, or longer than this type accepts: drop
         * the frame and resync on the next type byte */
        if(!p->spec || p->need == 0 || p->need > p->spec->max_len) {
            p->stage = ParserType;
            break;
        }
        p->stage = ParserPayload;
        break;
    case ParserPayload:
        p->buf[p->got++] = b;
        if(p->got >= p->need) {
            if(p->spec->handler(app, p->buf, p->need)) {
                view_port_update(app->vp);
            }
            p->stage = ParserType;
        }
        break;
    default:
        p->stage = ParserType;
        break;
    }
}

/* ---------------- BLE plumbing ---------------- */

static uint16_t ble_rx_callback(SerialServiceEvent event, void* context) {
    App* app = context;
    if(event.event == SerialServiceEventTypeDataReceived && event.data.size > 0) {
        size_t stored = furi_stream_buffer_send(app->rx, event.data.buffer, event.data.size, 0);
        if(stored != event.data.size) {
            /* a dropped chunk loses the frame boundary: resync next loop */
            app->rx_overrun = true;
        }
    }
    /* the serial service reads this as our free buffer size */
    size_t free_space = furi_stream_buffer_spaces_available(app->rx);
    return free_space > 0xFFFF ? 0xFFFF : (uint16_t)free_space;
}

static void bt_status_changed(BtStatus status, void* context) {
    App* app = context;
    app->bt_status = status;
    /* announce once per connection (the PC may be another instance entirely) */
    if(status != BtStatusConnected) {
        app->announced = false;
        /* drop anything queued for the PC that just left */
        if(app->tx) furi_stream_buffer_reset(app->tx);
    }
    /* The BT service overrides our serial callback on every connect, so reclaim it. */
    if(app->ble) {
        ble_profile_serial_set_event_callback(app->ble, RX_STREAM_SIZE, ble_rx_callback, app);
    }
    view_port_update(app->vp);
}

static void ble_start(App* app) {
    app->bt = furi_record_open(RECORD_BT);
    for(int i = 0; i < 30 && app->running; i++) {
        app->ble = bt_profile_start(app->bt, ble_profile_serial, NULL);
        if(app->ble) break;
        furi_delay_ms(200);
    }
    if(!app->ble) {
        FURI_LOG_E(TAG, "bt_profile_start failed");
        return;
    }
    ble_profile_serial_set_event_callback(app->ble, RX_STREAM_SIZE, ble_rx_callback, app);
    bt_set_status_changed_callback(app->bt, bt_status_changed, app);
    FURI_LOG_I(TAG, "BLE serial profile started");
}

static void ble_stop(App* app) {
    view_port_enabled_set(app->vp, false);
    if(app->ble) {
        ble_profile_serial_set_event_callback(app->ble, 0, NULL, NULL);
        bt_disconnect(app->bt);
        furi_delay_ms(1500); /* let the disconnect land before restoring the profile */
        bt_profile_restore_default(app->bt);
    }
    bt_set_status_changed_callback(app->bt, NULL, NULL);
    furi_record_close(RECORD_BT);
}

/* ---------------- app entry ---------------- */

int32_t ytm_remote_app(void* p) {
    UNUSED(p);
    App* app = malloc(sizeof(App));
    memset(app, 0, sizeof(App));
    app->mutex = furi_mutex_alloc(FuriMutexTypeNormal);
    app->running = true;
    app->idle_since = furi_get_tick();
    app->scroll_next = furi_get_tick() + SCROLL_HOLD_MS; /* read before it moves */
    /* start the strokes spread across the screen rather than stacked on the left */
    for(uint8_t w = 0; w < WAVE_COUNT; w++) {
        app->wave_head[w] = (float)(w * 127 / WAVE_COUNT);
    }

    cfg_load(app);

    app->notif = furi_record_open(RECORD_NOTIFICATION);
    NotifSettings* ns = notif_settings(app->notif);
    if(notif_settings_sane(ns)) {
        app->orig_brightness = ns->display_brightness;
        app->have_orig = true;
    } else {
        FURI_LOG_W(TAG, "notification settings layout unexpected; brightness untouched");
    }
    if(app->cfg.keep_on) backlight_hold(app, true);
    backlight_apply(app);

    app->gui = furi_record_open(RECORD_GUI);
    app->vp = view_port_alloc();
    view_port_draw_callback_set(app->vp, draw_callback, app);
    view_port_input_callback_set(app->vp, input_callback, app);
    gui_add_view_port(app->gui, app->vp, GuiLayerFullscreen);

    app->rx = furi_stream_buffer_alloc(RX_STREAM_SIZE, 1);
    app->tx = furi_stream_buffer_alloc(TX_QUEUE_LEN, 1);

    ble_start(app);

    Parser parser;
    memset(&parser, 0, sizeof(parser));
    uint8_t chunk[160];
    uint32_t last_tx = 0;

    while(app->running) {
        size_t got = furi_stream_buffer_receive(app->rx, chunk, sizeof(chunk), 20);
        for(size_t i = 0; i < got; i++) {
            parser_byte(app, &parser, chunk[i]);
        }

        if(app->rx_overrun) {
            app->rx_overrun = false;
            memset(&parser, 0, sizeof(parser));
            FURI_LOG_W(TAG, "rx overrun: parser resynced");
        }

        /* Nothing paired: run the idle screensaver, and let the screen go dark
         * after a while. The BLE profile keeps advertising throughout, so the PC
         * can still connect — the app is meant to sit here for days. */
        if(app->bt_status != BtStatusConnected) {
            uint32_t now = furi_get_tick();
            if(!app->slept) {
                if(now - app->wave_tick >= WAVE_FRAME_MS) {
                    app->wave_tick = now;
                    for(uint8_t w = 0; w < WAVE_COUNT; w++) {
                        app->wave_head[w] += wave_speed(w);
                        if(app->wave_head[w] >= 128.0f) app->wave_head[w] -= 128.0f;
                    }
                    view_port_update(app->vp);
                }
                if(now - app->idle_since >= SCREENSAVER_SLEEP_MS) screen_sleep(app);
            }
        } else {
            if(app->slept) screen_wake(app); /* the PC turned up: light the screen */
            /* Marquee: step to the end, pause on the last portion, then start
             * over — pausing at the start as well, so a new title can be read
             * before it moves. */
            if(app->scrolling && !app->settings_open) {
                uint32_t now = furi_get_tick();
                if((int32_t)(now - app->scroll_next) >= 0) {
                    if(app->scroll < app->scroll_max) {
                        app->scroll++;
                        app->scroll_next = now + SCROLL_MS;
                        view_port_update(app->vp);
                    } else if(app->scroll_end_hold) {
                        app->scroll = 0; /* the end has had its pause: start over */
                        app->scroll_end_hold = false;
                        app->scroll_next = now + SCROLL_HOLD_MS;
                        view_port_update(app->vp);
                    } else {
                        app->scroll_end_hold = true; /* hold on the last portion */
                        app->scroll_next = now + SCROLL_HOLD_MS;
                    }
                }
            }
        }

        if(app->ble && app->bt_status == BtStatusConnected && !app->announced) {
            app->announced = true;
            send_cmd(app, CMD_CAPS);
        }

        /* Pace queued commands out from this thread, so a congested BLE stack
         * stalls the app loop instead of the input callback. */
        if(app->ble && app->bt_status == BtStatusConnected) {
            uint32_t now = furi_get_tick();
            if(now - last_tx >= TX_PACE_MS && furi_stream_buffer_bytes_available(app->tx) > 0) {
                uint8_t cmd = 0;
                if(furi_stream_buffer_receive(app->tx, &cmd, 1, 0) == 1) {
                    last_tx = now;
                    if(!ble_profile_serial_tx(app->ble, &cmd, 1)) {
                        FURI_LOG_W(TAG, "BLE tx rejected command %c", cmd);
                    }
                }
            }
        }

        /* The overlay is kept on screen by the deadline checked in the draw
         * callback; one redraw after it expires is what clears it. */
        if(app->vol_clear_pending && (int32_t)(app->vol_until - furi_get_tick()) <= 0) {
            app->vol_clear_pending = false;
            view_port_update(app->vp);
        }
        if(app->toast_clear_pending && (int32_t)(app->toast_until - furi_get_tick()) <= 0) {
            app->toast_clear_pending = false;
            view_port_update(app->vp);
        }

        /* The serial service keeps a receive-credit counter that it only refills
         * when we say the buffer is drained. Without this the credit runs out
         * and the firmware starts logging buffer-overflow warnings. */
        if(got > 0 && app->ble && furi_stream_buffer_is_empty(app->rx)) {
            ble_profile_serial_notify_buffer_is_empty(app->ble);
        }
    }

    /* hand the display back: original brightness, and no backlight lock */
    if(app->have_orig) notif_write_brightness(app, app->orig_brightness);
    backlight_hold(app, false);
    notification_message(app->notif, &sequence_display_backlight_on);
    furi_record_close(RECORD_NOTIFICATION);

    ble_stop(app);
    furi_stream_buffer_free(app->rx);
    furi_stream_buffer_free(app->tx);
    gui_remove_view_port(app->gui, app->vp);
    view_port_free(app->vp);
    furi_record_close(RECORD_GUI);
    furi_mutex_free(app->mutex);
    free(app);
    return 0;
}
