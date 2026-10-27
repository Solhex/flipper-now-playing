/* Runs in the YouTube Music page: reads the like button and presses it on demand.
 *
 * The like state lives only in the page — no desktop API exposes it — so this is
 * the honest source: the same `aria-pressed` the site's own UI uses, read from
 * the same button a user clicks.
 *
 * Every tab reports, and it also says whether it is the tab the user is looking
 * at (focused/visible, plus when they last touched it), because with YouTube
 * Music open in two browsers the bridge has to follow the one in use rather than
 * whichever spoke last.
 */
(() => {
  "use strict";

  /* The worker re-injects this file into tabs that were open before the
   * extension was reloaded (a reload does not re-inject content scripts, which
   * is why "connected but nothing works" kept happening). Running twice would
   * register two listeners and click twice, so refuse the second run. */
  if (window.__ytmBridgeLoaded) return;
  window.__ytmBridgeLoaded = true;

  const LIKE = "ytmusic-like-button-renderer #button-shape-like button";
  const HEARTBEAT_MS = 2000;
  const ACTIVITY_REPORT_MS = 500;

  /* A per-tab id, stable while the page lives, so the bridge can tell two tabs
   * apart and aim a toggle at one of them. */
  const CLIENT = (() => {
    let id = sessionStorage.getItem("ytmBridgeClient");
    if (!id) {
      id = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2))
        .slice(0, 8);
      sessionStorage.setItem("ytmBridgeClient", id);
    }
    return id;
  })();

  let seen = 0;              // toggles that actually reached this tab
  let lastReject = null;     // ...and why one was ignored, if it was
  let lastActive = Date.now();
  let lastSent = "";
  let lastActivityReport = 0;
  let lastClick = null;

  /* Prefer a button that reports a state: a renderer can exist while its button
   * is still being filled in by the site's own data. */
  const button = () => document.querySelector(LIKE) ||
    document.querySelector("ytmusic-like-button-renderer button[aria-pressed]") ||
    document.querySelector("ytmusic-like-button-renderer button");
  const renderers = () => document.querySelectorAll("ytmusic-like-button-renderer").length;

  const likedNow = () => {
    const b = button();
    if (!b || !b.hasAttribute("aria-pressed")) return null;
    return b.getAttribute("aria-pressed") === "true";
  };

  /* Title/artist come from the Media Session metadata the player publishes: it
   * names the *track*, while document.title is whatever the page feels like. */
  const trackInfo = () => {
    const meta = navigator.mediaSession && navigator.mediaSession.metadata;
    if (meta) return { title: meta.title || "", artist: meta.artist || "" };
    return { title: document.title || "", artist: "" };
  };

  const isPlaying = () => {
    const video = document.querySelector("video");
    return video ? !video.paused && !video.ended : null;
  };

  const videoEl = () => document.querySelector("video");

  /* The cover the player is showing. Media Session metadata names the track's own
   * artwork; the player-bar image is the fallback for the moments before the
   * metadata lands. The host page cannot read the pixels (cross-origin taint),
   * so only the URL travels — the extension page fetches and downsizes it. */
  const artworkUrl = () => {
    const meta = navigator.mediaSession && navigator.mediaSession.metadata;
    const art = meta && meta.artwork;
    if (art && art.length) {
      const best = art[art.length - 1];
      if (best && best.src) return best.src;
    }
    const img = document.querySelector("ytmusic-player-bar img") ||
      document.querySelector("ytmusic-player-bar .thumbnail img");
    return img ? (img.currentSrc || img.src || "") : "";
  };

  /* Position/length/volume come straight off the media element — better than any
   * desktop API, which is why this runs here rather than on the PC. */
  const progress = () => {
    const v = videoEl();
    if (!v) return { pos: 0, dur: 0, vol: 100 };
    return {
      pos: isFinite(v.currentTime) ? Math.floor(v.currentTime) : 0,
      dur: isFinite(v.duration) ? Math.floor(v.duration) : 0,
      vol: Math.round((isFinite(v.volume) ? v.volume : 1) * 100),
    };
  };

  function payload() {
    const info = trackInfo();
    return {
      client: CLIENT,
      liked: likedNow(),
      title: info.title,
      artist: info.artist,
      playing: isPlaying(),
      art: artworkUrl(),
      ...progress(),
      visible: document.visibilityState === "visible",
      focused: document.hasFocus(),
      lastActive,
      /* Proof of what the last click did — "the button is not there" and "the
       * click changed nothing" are indistinguishable from the bridge otherwise. */
      diag: { button: !!button(), renderers: renderers(), click: lastClick,
              seen, reject: lastReject },
    };
  }

  function report(force) {
    const data = payload();
    const key = JSON.stringify(data);
    if (!force && key === lastSent) return;
    lastSent = key;
    chrome.runtime.sendMessage({ type: "state", payload: data }).catch(() => {});
  }

  const noteActivity = () => {
    lastActive = Date.now();
    const now = Date.now();
    if (now - lastActivityReport > ACTIVITY_REPORT_MS) {
      lastActivityReport = now;
      report(true);          // tell the bridge this tab is the one in use
    }
  };

  for (const event of ["pointerdown", "keydown", "wheel"]) {
    window.addEventListener(event, noteActivity, { capture: true, passive: true });
  }
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") noteActivity();
    report(true);
  });
  window.addEventListener("focus", noteActivity);
  window.addEventListener("blur", () => report(true));

  /* A plain `.click()` is not enough on the real page: it lands, the button is
   * there, and aria-pressed never changes (verified on Brave — the untrusted
   * click is ignored). So send the whole pointer sequence a person would, at the
   * element actually drawn at the button's centre, and report what was in the way
   * if something else is on top. */
  function fireLike(el) {
    const box = el.getBoundingClientRect();
    const x = Math.round(box.left + box.width / 2);
    const y = Math.round(box.top + box.height / 2);
    const at = document.elementFromPoint(x, y) || el;
    const covered = !(at === el || el.contains(at));
    /* Exactly one click per target: dispatching the sequence and then calling
     * .click() as well fires it twice, which toggles the like straight back. */
    for (const target of covered && at !== el ? [at, el] : [el]) {
      press(target, { x, y });
    }
    return {
      tag: at.tagName ? `${at.tagName.toLowerCase()}${at.id ? "#" + at.id : ""}` : "?",
      disabled: el.hasAttribute("disabled") || el.getAttribute("aria-disabled") === "true",
      covered, w: Math.round(box.width), h: Math.round(box.height),
    };
  }

  const sameTrack = (wanted) => {
    if (!wanted) return true;                 // no target: act, as before
    const mine = (trackInfo().title || "").trim().toLowerCase().slice(0, 24);
    const theirs = String(wanted).trim().toLowerCase().slice(0, 24);
    return !mine || !theirs || mine === theirs;
  };

  /* ---------------------------------------------------------------- transport
   * The Flipper's own buttons land here: the extension is now self-contained, so
   * playback is driven by the site's real controls rather than a desktop hook.
   * Every control is pressed with the same pointer sequence as the like button,
   * because a bare .click() is discarded by the page (verified on Brave). */
  const PLAY_PAUSE = "#play-pause-button, .play-pause-button, tp-yt-paper-icon-button.play-pause-button";
  const NEXT_BTN = "#next-button, .next-button";
  const PREV_BTN = "#previous-button, .previous-button";

  /* One copy of the pointer sequence. `at` lets the like path aim at the point
   * it already measured (and at whatever is covering the button). */
  function press(el, at) {
    const box = el.getBoundingClientRect();
    const x = at ? at.x : Math.round(box.left + box.width / 2);
    const y = at ? at.y : Math.round(box.top + box.height / 2);
    const opts = { bubbles: true, cancelable: true, composed: true, view: window,
                   clientX: x, clientY: y, button: 0, buttons: 1, pointerId: 1,
                   pointerType: "mouse", isPrimary: true };
    const seq = [["pointerdown", PointerEvent], ["mousedown", MouseEvent],
                 ["pointerup", PointerEvent], ["mouseup", MouseEvent],
                 ["click", MouseEvent]];
    for (const [type, Kind] of seq) {
      try {
        el.dispatchEvent(new Kind(type, type === "click" ? { ...opts, buttons: 0 } : opts));
      } catch (e) { /* element may reject a synthetic event */ }
    }
  }

  function act(kind) {
    const v = videoEl();
    switch (kind) {
      case "playpause": {
        /* Media element first: the page cannot ignore a programmatic
         * play()/pause(). Fall back to the button only if play() is refused
         * (the autoplay rule), which is the case a click exists for. */
        if (v) {
          const wantPlay = v.paused;
          const r = wantPlay ? v.play() : v.pause();
          if (wantPlay && r && r.catch) {
            r.catch(() => { const b = document.querySelector(PLAY_PAUSE); if (b) press(b); });
          }
          break;
        }
        const b = document.querySelector(PLAY_PAUSE);
        if (b) press(b);
        break;
      }
      /* No element API for skip, so these are clicks — and a page may ignore a
       * synthetic click (see fireLike). Kept because there is no alternative. */
      case "next": { const n = document.querySelector(NEXT_BTN); if (n) press(n); break; }
      case "prev": { const p = document.querySelector(PREV_BTN); if (p) press(p); break; }
      /* 5 s: what YouTube Music's own seek controls do, so the buttons on the
       * device and the buttons in the player agree. */
      case "seekback": if (v && isFinite(v.currentTime)) v.currentTime = Math.max(0, v.currentTime - 5); break;
      case "seekfwd": if (v && isFinite(v.duration)) v.currentTime = Math.min(v.duration, v.currentTime + 5); break;
      case "volup": if (v) v.volume = Math.min(1, v.volume + 0.05); break;
      case "voldown": if (v) v.volume = Math.max(0, v.volume - 0.05); break;
      default: break;
    }
    setTimeout(() => report(true), 250);   // the new state goes out at once
  }

  /* Transport commands from the Flipper (the like button keeps using the
   * existing "toggle" path, which already knows how to land a click). */
  chrome.runtime.onMessage.addListener((message) => {
    if (!message || message.type !== "cmd") return;
    if (message.client && message.client !== CLIENT) return;
    act(message.cmd);
  });

  /* The bridge aims a toggle at one tab (message.client); a tab that is not that
   * one stays out of the way, so two browsers cannot fight over the same click.
   * The track name is a second guard in case ids went stale. */
  chrome.runtime.onMessage.addListener((message) => {
    if (!message || message.type !== "toggle") return;
    seen += 1;
    if (message.client && message.client !== CLIENT) {
      lastReject = `other tab (${message.client})`;
      report(true);
      return;
    }
    if (!sameTrack(message.title)) {
      lastReject = `other track (${String(message.title).slice(0, 20)})`;
      report(true);
      return;
    }
    lastReject = null;

    const b = button();
    if (!b) {
      lastClick = { ok: false, reason: "no like button" };
      report(true);
      return;
    }
    const before = b.getAttribute("aria-pressed");
    lastClick = { ok: false, before, checked: true, at: Date.now(), ...fireLike(b) };
    setTimeout(() => {
      const later = b.getAttribute("aria-pressed");
      lastClick = { ...lastClick, ok: later !== before, before, after: later,
                    at: Date.now() };
      if (!lastClick.ok) {
        /* Chromium throws away untrusted events, so the click above can land and
         * change nothing (verified on Brave). Leave the button focused and say
         * whether this page is the focused window: the bridge can then press the
         * button with a real Enter, which the site does honour. */
        try { b.focus(); } catch (error) { /* ignore */ }
        lastClick.buttonFocused = document.activeElement === b;
        lastClick.windowFocused = document.hasFocus();
      }
      report(true);
    }, 900);
  });

  /* aria-pressed flips whenever the track changes or someone likes it, so a
   * mutation observer is enough to catch everything the site does itself. */
  new MutationObserver(() => report(false)).observe(document.documentElement, {
    subtree: true,
    attributes: true,
    attributeFilter: ["aria-pressed"],
  });

  /* The interval always sends: the bridge treats a report older than a few
   * seconds as "unknown", so a heartbeat that stays quiet when nothing changed
   * would blank the heart on a paused track. */
  setInterval(() => report(true), HEARTBEAT_MS);
  report(true);
})();
