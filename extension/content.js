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

  let lastActive = Date.now();
  let lastSent = "";
  let lastActivityReport = 0;
  let lastClick = null;

  const button = () => document.querySelector(LIKE);
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

  function payload() {
    const info = trackInfo();
    return {
      client: CLIENT,
      liked: likedNow(),
      title: info.title,
      artist: info.artist,
      playing: isPlaying(),
      visible: document.visibilityState === "visible",
      focused: document.hasFocus(),
      lastActive,
      /* Proof of what the last click did — "the button is not there" and "the
       * click changed nothing" are indistinguishable from the bridge otherwise. */
      diag: { button: !!button(), renderers: renderers(), click: lastClick },
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

  const sameTrack = (wanted) => {
    if (!wanted) return true;                 // no target: act, as before
    const mine = (trackInfo().title || "").trim().toLowerCase().slice(0, 24);
    const theirs = String(wanted).trim().toLowerCase().slice(0, 24);
    return !mine || !theirs || mine === theirs;
  };

  /* The bridge aims a toggle at one tab (message.client); a tab that is not that
   * one stays out of the way, so two browsers cannot fight over the same click.
   * The track name is a second guard in case ids went stale. */
  chrome.runtime.onMessage.addListener((message) => {
    if (!message || message.type !== "toggle") return;
    if (message.client && message.client !== CLIENT) return;
    if (!sameTrack(message.title)) return;

    const b = button();
    if (!b) {
      lastClick = { ok: false, reason: "no like button" };
      report(true);
      return;
    }
    const before = b.getAttribute("aria-pressed");
    b.click();
    lastClick = { ok: false, before, checked: true, at: Date.now() };
    setTimeout(() => {
      const later = b.getAttribute("aria-pressed");
      lastClick = { ok: later !== before, before, after: later, at: Date.now() };
      report(true);
    }, 700);
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
