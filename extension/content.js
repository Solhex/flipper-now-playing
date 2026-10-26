/* Runs in the YouTube Music page: reads the like button and presses it on demand.
 *
 * The like state lives only in the page — no desktop API exposes it — so this is
 * the honest source: the same `aria-pressed` the site's own UI uses, read from
 * the same button a user clicks.
 */
(() => {
  "use strict";

  const LIKE = "ytmusic-like-button-renderer #button-shape-like button";
  const HEARTBEAT_MS = 2000;

  const button = () => document.querySelector(LIKE);

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

  let lastSent = "";

  function report(force) {
    const info = trackInfo();
    const payload = {
      liked: likedNow(),
      title: info.title,
      artist: info.artist,
      playing: isPlaying(),
    };
    const key = JSON.stringify(payload);
    if (!force && key === lastSent) return;
    lastSent = key;

    chrome.runtime.sendMessage({ type: "state", payload }).catch(() => {});
  }

  /* The bridge asks for this when the Flipper's like button is held: click the
   * real button, then report what the page settled on (the site answers
   * asynchronously, so the immediate read is often still the old value). */
  chrome.runtime.onMessage.addListener((message) => {
    if (!message || message.type !== "toggle") return;
    const b = button();
    if (!b) return;
    b.click();
    setTimeout(() => report(true), 300);
    setTimeout(() => report(true), 1200);
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
