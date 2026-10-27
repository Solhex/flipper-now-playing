/* Relays between the YouTube Music tabs, the Flipper link page (remote.html)
 * and - only when the extension page says it is not holding the Bluetooth link
 * itself - the old loopback bridge.
 *
 * The work sits here rather than in the content script so requests come from the
 * extension's own origin: page CSP and Chrome's public-to-private rules then
 * cannot block a loopback call.
 *
 *   content -> "state"   the page's like/now-playing state, whenever it changes
 *   remote  <- "state"   the same state, pushed at the Flipper link page
 *   remote  -> "cmd"     a button on the Flipper (or "like"), run on the page
 *
 * Loopback (GET /wait, POST /state) stays for Firefox, which has no Web
 * Bluetooth: there the Python bridge still drives the device.
 */

const BRIDGE = "http://127.0.0.1:8765";

/* Two extension installs (say Firefox and Brave) both poll the one bridge, and a
 * command is only delivered to whichever asks first. The id makes that visible in
 * the bridge log instead of a mystery. Sent as a query parameter, not a header,
 * so no CORS preflight is involved. */
const WORKER = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2))
  .slice(0, 6);
const BROWSER_NAME = /firefox/i.test(navigator.userAgent) ? "firefox" : "chromium";
const WHO = `worker=${WORKER}&browser=${BROWSER_NAME}`;
const RETRY_MS = 2000;

/* "bluetooth" = the extension page holds the Flipper link itself (no companion
 * app). "bridge" = fall back to the loopback poll. Only the link page knows,
 * because only a document has navigator.bluetooth. */
let mode = "bridge";

const remotePorts = new Set();

let polling = false;

async function postState(payload) {
  try {
    await fetch(`${BRIDGE}/state?${WHO}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch (error) {
    /* bridge not running - the next heartbeat tries again */
  }
}

/* Exactly one tab gets a toggle: two YouTube Music tabs both clicking would
 * like and then unlike in the same breath, so the state would never change. */
const injectedTabs = new Set();
const clientTabs = new Map();
let restoreTab = null;

function rememberTab(client, sender) {
  if (!client || !sender || !sender.tab) return;
  clientTabs.set(client, { tabId: sender.tab.id, windowId: sender.tab.windowId });
}

async function activateClient(client) {
  const info = clientTabs.get(client) || clientTabs.values().next().value;
  if (!info) return false;
  try {
    const [active] = await chrome.tabs.query({ active: true, windowId: info.windowId });
    if (active && active.id !== info.tabId) {
      restoreTab = { tabId: active.id, windowId: info.windowId };
    }
    await chrome.tabs.update(info.tabId, { active: true });
    await chrome.windows.update(info.windowId, { focused: true });
    return true;
  } catch (error) {
    return false;
  }
}

async function restorePreviousTab() {
  const saved = restoreTab;
  restoreTab = null;
  if (!saved) return false;
  try {
    await chrome.tabs.update(saved.tabId, { active: true });
    return true;
  } catch (error) {
    return false;
  }
}

async function ensureScript(tabId) {
  if (injectedTabs.has(tabId)) return false;
  try {
    await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"] });
    injectedTabs.add(tabId);
    return true;
  } catch (error) {
    return false;      // closed tab, or a page we are not allowed to touch
  }
}

async function sweepTabs(force) {
  try {
    const tabs = await chrome.tabs.query({ url: ["https://music.youtube.com/*"] });
    for (const tab of tabs) {
      if (tab.discarded) continue;
      if (force) injectedTabs.delete(tab.id);
      ensureScript(tab.id);
    }
  } catch (error) { /* no tabs permission for a URL match: ignore */ }
}

/* A tab that navigates gets a fresh document and a fresh content script. A tab
 * that is merely reloaded while the add-on itself is being reloaded gets neither
 * - and because the background had it down as "already injected", it was never
 * repaired either: the page just stopped reporting, forever, with nothing
 * anywhere to say why. Forget a tab the moment it starts loading, so the next
 * sweep puts the script back. */
if (chrome.tabs && chrome.tabs.onUpdated) {
  chrome.tabs.onUpdated.addListener((tabId, info) => {
    if (info.status === "loading") injectedTabs.delete(tabId);
  });
}

function rankedTabs() {
  return chrome.tabs.query({}).then((tabs) =>
    [...tabs].sort((a, b) => {
      const score = (tab) => (tab.audible ? 2 : 0) + (tab.active ? 1 : 0);
      return score(b) - score(a);
    }));
}

function forwardMessage(message) {
  rankedTabs().then((ranked) => {
    const tryNext = (index) => {
      if (index >= ranked.length) return;
      const tab = ranked[index];
      let sent;
      try {
        sent = chrome.tabs.sendMessage(tab.id, message);
      } catch (error) {
        tryNext(index + 1);
        return;
      }
      if (sent && typeof sent.then === "function") {
        sent.then(() => {}).catch(async () => {
          // No listener in this tab: it probably predates the extension reload.
          if (await ensureScript(tab.id)) {
            try {
              await chrome.tabs.sendMessage(tab.id, message);
              return;
            } catch (error) { /* fall through to the next tab */ }
          }
          tryNext(index + 1);
        });
      }
    };
    tryNext(0);
  }).catch(() => {});
}

function forwardToggle(title, client) {
  forwardMessage({ type: "toggle", title, client });
}

/* A Flipper button: "like" keeps the existing toggle path (it knows how to land
 * a click), everything else is a transport action handled by the page. */
function handleRemoteCommand(cmd, title, client) {
  if (cmd === "like") forwardToggle(title, client);
  else forwardMessage({ type: "cmd", cmd, client });
}

function broadcast(payload) {
  for (const port of remotePorts) {
    try { port.postMessage({ type: "state", payload }); } catch (error) { /* gone */ }
  }
}

/* The link page connects here and stays connected: it is the only context that
 * can hold a Web Bluetooth connection. */
chrome.runtime.onConnect.addListener((port) => {
  if (!port || port.name !== "remote") return;
  remotePorts.add(port);
  port.onMessage.addListener((message) => {
    if (!message) return;
    if (message.type === "mode") {
      mode = message.mode === "bluetooth" ? "bluetooth" : "bridge";
      if (mode === "bridge") pollLoop();
    } else if (message.type === "cmd") {
      handleRemoteCommand(message.cmd, message.title, message.client);
    }
  });
  /* The link page is gone (tab closed, or it navigated away). Fall back to the
   * loopback bridge if one is running: without this the worker stays in
   * "bluetooth" mode for good, so it keeps reading the page and reports nothing -
   * no heart, and a like falls back to the keyboard path. */
  port.onDisconnect.addListener(() => {
    remotePorts.delete(port);
    if (remotePorts.size === 0 && mode === "bluetooth") {
      mode = "bridge";
      pollLoop();
    }
  });
});

/* What the extension can see about itself, sent to the bridge and printed there.
 *
 * Added after an afternoon spent guessing why a page never reported: from the
 * bridge everything looked healthy (the background was polling, even taking
 * commands) while nothing arrived from any tab. The facts that matter - site
 * access granted? which YouTube Music tabs are visible? what does the browser
 * say when asked to inject? - only exist inside the browser, and the browser
 * will not answer from the outside. So it answers itself, on a timer, into the
 * log. Injecting the content script is part of the probe, which also heals a tab
 * whose script went missing. */
async function probeOnce() {
  const site = "https://music.youtube.com/*";
  const out = { version: chrome.runtime.getManifest().version, mode, siteAccess: null,
                bridge: null, tabs: [], inject: null };
  try {
    out.siteAccess = await chrome.permissions.contains({ origins: [site] });
  } catch (error) {
    out.siteAccess = `error: ${error.message}`;
  }
  try {
    const reply = await fetch(`${BRIDGE}/state`, { cache: "no-store" });
    out.bridge = String(reply.status);
  } catch (error) {
    out.bridge = `unreachable: ${error.message}`;
  }
  try {
    const tabs = await chrome.tabs.query({ url: [site] });
    out.tabs = tabs.map((tab) => ({ id: tab.id, discarded: !!tab.discarded, status: tab.status }));
    const target = tabs.find((tab) => !tab.discarded) || tabs[0];
    if (!target) {
      out.inject = "no tab";
    } else {
      try {
        await chrome.scripting.executeScript({ target: { tabId: target.id }, files: ["content.js"] });
        injectedTabs.add(target.id);
        out.inject = "ok";
      } catch (error) {
        out.inject = `failed: ${error.message}`;
      }
    }
  } catch (error) {
    out.tabs = `query failed: ${error.message}`;
  }
  try {
    /* Sent as a GET, not a POST: until the user grants this add-on its hosts
     * (Firefox makes site access opt-in), the browser CORS-preflights every
     * POST and the bridge deliberately answers no CORS - so a POST probe is
     * blocked in exactly the state it exists to report, and the bridge is left
     * guessing "old build" while the real fault is no site access. A GET with
     * the payload in the query string is not preflighted. */
    const probe = encodeURIComponent(JSON.stringify(out));
    await fetch(`${BRIDGE}/diag?probe=${probe}`, { cache: "no-store" });
  } catch (error) {
    /* no bridge to hold it: the extension's own console is the only other reader */
  }
}

async function pollLoop() {
  if (polling) return;
  polling = true;
  sweepTabs();
  while (mode === "bridge") {
    // Cheap, and it is the only thing that repairs a page whose content script
    // went missing: ask the tabs to check themselves on every poll.
    sweepTabs();
    try {
      // A hung request must not stop the loop for good: abort it and retry.
      const controller = new AbortController();
      const abort = setTimeout(() => controller.abort(), 25000);
      const response = await fetch(`${BRIDGE}/wait?${WHO}`, {
        cache: "no-store",
        signal: controller.signal,
      });
      const data = await response.json();
      clearTimeout(abort);
      if (data && data.cmd === "toggle") forwardToggle(data.title, data.client);
      else if (data && data.cmd === "activate") await activateClient(data.client);
      else if (data && data.cmd === "restore") await restorePreviousTab();
      // Volume is the page's own slider: MPRIS's number does not match it, so
      // the Flipper's keys have to move the slider the site shows.
      else if (data && (data.cmd === "volup" || data.cmd === "voldown"))
        handleRemoteCommand(data.cmd, data.title, data.client);
    } catch (error) {
      await new Promise((resolve) => setTimeout(resolve, RETRY_MS));
    }
  }
  polling = false;
}

chrome.runtime.onMessage.addListener((message, sender) => {
  if (!message || message.type !== "state") return;
  rememberTab(message.payload && message.payload.client, sender);
  broadcast(message.payload);                    // to the Flipper link page
  if (mode === "bridge") postState(message.payload);
  pollLoop();
});

/* The toolbar button opens (or focuses) the Flipper link page. Its tab id is
 * kept in storage so no "tabs" permission is needed to find it again, and it is
 * marked non-discardable: Chromium must not throw away the tab that owns the
 * Bluetooth connection.
 *
 * The Firefox build has no action key at all (the link page cannot work there),
 * so guard the listener: an undefined namespace at top level would take the
 * whole worker down with it. */
const REMOTE_TAB_KEY = "remoteTabId";

async function openRemoteTab() {
  const url = chrome.runtime.getURL("remote.html");
  const stored = await chrome.storage.local.get(REMOTE_TAB_KEY);
  const id = stored[REMOTE_TAB_KEY];
  if (id !== undefined) {
    try {
      await chrome.tabs.update(id, { active: true, autoDiscardable: false });
      return;
    } catch (error) { /* the tab was closed: open a fresh one */ }
  }
  const tab = await chrome.tabs.create({ url, pinned: false });
  await chrome.storage.local.set({ [REMOTE_TAB_KEY]: tab.id });
  try { await chrome.tabs.update(tab.id, { autoDiscardable: false }); } catch (error) { /* older browser */ }
}

/* The toolbar button has two jobs. On Chromium it opens the link page that holds
 * the Bluetooth connection. On Firefox there is no Web Bluetooth - and Firefox
 * treats this add-on's site access as opt-in, so an add-on can be installed,
 * enabled, running, and unable to touch a single page. Granting it takes a click
 * from the user, and a click is precisely what permissions.request() requires:
 * so on Firefox the button is the grant, and it says so in its title. */
/* What the user has to grant on Firefox: every host the manifest declares, taken
 * from the manifest itself so the two can never disagree - including the
 * loopback origin the bridge listens on. */
const SITE_ORIGINS = (chrome.runtime.getManifest().host_permissions || [])
  .filter((origin) => /^(https?|wss?):\/\//.test(origin));

async function onToolbarClick() {
  const hasBluetooth = (chrome.runtime.getManifest().permissions || [])
    .includes("bluetooth");
  if (hasBluetooth) return openRemoteTab();
  try {
    const granted = await chrome.permissions.request({ origins: SITE_ORIGINS });
    console.log(`ytm-bridge: site access ${granted ? "granted" : "declined"}`);
    if (!granted) return;
  } catch (error) {
    console.log("ytm-bridge: could not ask for site access:", error.message);
    return;
  }
  sweepTabs(true);              // inject straight away rather than at next reload
  probeOnce();
}

if (chrome.action && chrome.action.onClicked) {
  chrome.action.onClicked.addListener(onToolbarClick);
}

chrome.runtime.onStartup.addListener(pollLoop);
chrome.runtime.onInstalled.addListener(pollLoop);

/* Chromium stops an idle service worker after ~30 s, and the long poll alone does
 * not reliably keep it alive - Brave in particular suspends it aggressively. The
 * alarm wakes the worker so the poll restarts; without it the bridge queues like
 * commands that nobody ever picks up. */
chrome.alarms.create("keep-polling", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(() => {
  probeOnce();
  pollLoop();
});

probeOnce();
pollLoop();
