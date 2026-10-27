/* Relays between the YouTube Music tabs, the Flipper link page (remote.html)
 * and — only when the extension page says it is not holding the Bluetooth link
 * itself — the old loopback bridge.
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
    /* bridge not running — the next heartbeat tries again */
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

async function sweepTabs() {
  try {
    const tabs = await chrome.tabs.query({ url: ["https://music.youtube.com/*"] });
    for (const tab of tabs) {
      if (!tab.discarded) ensureScript(tab.id);
    }
  } catch (error) { /* no tabs permission for a URL match: ignore */ }
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
  port.onDisconnect.addListener(() => remotePorts.delete(port));
});

async function pollLoop() {
  if (polling) return;
  polling = true;
  sweepTabs();
  while (mode === "bridge") {
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

if (chrome.action && chrome.action.onClicked) {
  chrome.action.onClicked.addListener(openRemoteTab);
}

chrome.runtime.onStartup.addListener(pollLoop);
chrome.runtime.onInstalled.addListener(pollLoop);

/* Chromium stops an idle service worker after ~30 s, and the long poll alone does
 * not reliably keep it alive — Brave in particular suspends it aggressively. The
 * alarm wakes the worker so the poll restarts; without it the bridge queues like
 * commands that nobody ever picks up. */
chrome.alarms.create("keep-polling", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(() => pollLoop());

pollLoop();
