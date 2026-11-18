// SPDX-License-Identifier: GPL-3.0-or-later
// background.js - service worker with enterprise security.
// Owns ONE resilient WebSocket to the local bridge (ws://127.0.0.1:PORT).
// Keeping the socket here (not in the content script) avoids https→ws mixed
// content issues and centralises reconnect / timeout logic.
//
// Contract with content.js: every sendMessage ALWAYS gets a response object,
// even when the bridge is offline. The agentic loop must never hang waiting.

// OEP (Opaque Envelope Protocol) - see core/oep.js and PLAN_V3.md sec 4.
// When enabled, tool call args and results travel through the chat as
// AES-256-GCM envelopes. The LLM provider sees only [ENV:v1:...] and
// [RES:v1:...] ciphertext; only the local bridge can decrypt them.
importScripts("core/oep.js");

const PORT = 17613;
const HEALTH_PORT = 17614;
const URL = `ws://127.0.0.1:${PORT}`;
const HEALTH_URL = `http://127.0.0.1:${HEALTH_PORT}`;

// Chat sites where a ArteX-TNT provider content script runs. Status pushes go
// to every tab matching these. Add the new provider's URL pattern here (and in
// manifest.json content_scripts + host_permissions) when integrating another AI.
const PROVIDER_URLS = [
  "https://chat.deepseek.com/*",
  "https://chatgpt.com/*",
  "https://chat.openai.com/*",
  "https://claude.ai/*",
  "https://gemini.google.com/*",
  "https://www.kimi.ai/*",
  "https://kimi.ai/*",
  "https://chat.z.ai/*",
  "https://chat.qwen.ai/*",
  "https://arena.ai/*",
  "https://www.meta.ai/*",
  "https://meta.ai/*",
  "https://ptero.pro/*",
  "https://grok.com/*",
  "https://chat.mistral.ai/*"
];

const RECONNECT_MIN = 1000;
const RECONNECT_MAX = 30000;
const HEARTBEAT_MS = 10000;
const STALE_SOCKET_MS = 25000;
const REQUEST_TIMEOUT_DEFAULT = 130000;
const HEALTH_CHECK_INTERVAL = 30000;
// A mesh task is a WHOLE model turn in a real chat tab, so it gets its own
// deadlines: answering (typing + streaming one reply) can take minutes, while
// create/run/status are graph operations and must fail fast.
const MESH_ANSWER_TIMEOUT = 900000;
const MESH_CONTROL_TIMEOUT = 30000;

// threadId -> {threadId, provider, tabId, sentAt, attempts}
const meshInFlight = new Map();
// The watcher pushes a `mesh_task` frame; the content script answers with
// `mesh_answer`. Both sides key on threadId, and this map is the only state
// the service worker must keep across an MV3 suspension.

let ws = null;
let connected = false;
let authenticated = false;
let authToken = null;
let authError = null;
let reconnectDelay = RECONNECT_MIN;
let reconnectTimer = null;
let heartbeatTimer = null;

// OEP session state - one per live socket. Reset on close.
let oepSession = null;
let oepReady = false;
let oepEnabled = true; // set to false to force Reasoned mode
let healthCheckTimer = null;
let lastMessageAt = 0;
let nextId = 1;
const pending = new Map(); // id -> {resolve, timer}
let toolsCache = [];
let mcpAlive = false;
let serversCache = [];
let bridgeVersion = null;
let studioConnected = null;
let studioApp = null;
let studioProc = null;
let workspaceTabs = new Map(); // tabId -> workspace info
let sessionData = null; // persisted session data

function log(...a) {
  console.log("[zs-bg]", new Date().toISOString(), ...a);
}

function securityLog(event, details = {}) {
  console.log("[zs-sec]", new Date().toISOString(), event, details);
}

// ── Persistence ────────────────────────────────────────────────────────────
async function loadSession() {
  try {
    const data = await chrome.storage.local.get(["sessionData", "authToken"]);
    sessionData = data.sessionData || {
      history: [],
      toolCalls: [],
      workspaceState: {},
      templates: [],
      createdAt: Date.now(),
      updatedAt: Date.now()
    };
    authToken = data.authToken || null;
    if (authToken) {
      log("Loaded auth token from storage");
    }
  } catch (e) {
    log("Failed to load session:", e);
    sessionData = { history: [], toolCalls: [], workspaceState: {}, templates: [], createdAt: Date.now(), updatedAt: Date.now() };
  }
}

async function saveSession() {
  if (!sessionData) return;
  try {
    sessionData.updatedAt = Date.now();
    await chrome.storage.local.set({ sessionData, authToken });
  } catch (e) {
    log("Failed to save session:", e);
  }
}

function addToHistory(entry) {
  if (!sessionData) return;
  sessionData.history.push({ ...entry, timestamp: Date.now() });
  if (sessionData.history.length > 1000) {
    sessionData.history = sessionData.history.slice(-1000);
  }
  saveSession();
}

function addToolCall(toolCall) {
  if (!sessionData) return;
  sessionData.toolCalls.push({ ...toolCall, timestamp: Date.now() });
  if (sessionData.toolCalls.length > 500) {
    sessionData.toolCalls = sessionData.toolCalls.slice(-500);
  }
  saveSession();
}

// ── WebSocket lifecycle ─────────────────────────────────────────────────
// The bridge protects its socket with the local handshake secret. A browser
// cannot attach an Authorization header to a WebSocket handshake, so the
// secret is read from the loopback health server (127.0.0.1 only) and passed
// as a query parameter on the socket URL.
async function ensureAuthToken() {
  try {
    const response = await fetch(`${HEALTH_URL}/auth-token`, {
      method: "GET",
      cache: "no-cache",
      credentials: "omit",
      mode: "cors",
      referrerPolicy: "no-referrer"
    });
    if (!response.ok) {
      log("auth-token endpoint returned", response.status);
      // The bridge pins one extension id. A browser that still runs another
      // build of the extension sends a different origin and is refused.
      // Surface the expected id so the panel can say what to fix.
      let detail = null;
      try {
        detail = await response.json();
      } catch (e) {
        detail = null;
      }
      authError =
        response.status === 403
          ? {
              reason: (detail && detail.reason) || "forbidden",
              expected: (detail && detail.expected_origin) || "",
              seen: (detail && detail.seen_origin) || "",
              hint:
                (detail && detail.hint) ||
                "Reload the extension (chrome://extensions -> Reload)."
            }
          : { reason: "http_" + response.status, expected: "", seen: "", hint: "" };
      broadcastStatus();
      return false;
    }
    const data = await response.json();
    if (authError) {
      authError = null;
      broadcastStatus();
    }
    if (data && data.token) {
      if (authToken !== data.token) {
        authToken = data.token;
        await saveSession();
        log("Obtained auth token from bridge");
      }
    }
    return true;
  } catch (e) {
    log("Failed to obtain auth token:", e);
    authError = { reason: "bridge_unreachable", expected: "", seen: "", hint: "" };
    broadcastStatus();
    return false;
  }
}

async function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return;
  }
  clearTimeout(reconnectTimer);

  // The bridge is not reachable yet -> back off instead of opening a socket
  // that can only be rejected.
  if (!(await ensureAuthToken())) {
    scheduleReconnect();
    return;
  }

  const target = authToken
    ? `${URL}?token=${encodeURIComponent(authToken)}`
    : URL;
  let socket;
  try {
    socket = new WebSocket(target);
  } catch (e) {
    log("WebSocket ctor failed", e);
    securityLog("ws_ctor_failed", { error: String(e) });
    scheduleReconnect();
    return;
  }
  ws = socket;

  // Every handler checks that it still owns the live socket: a stale socket
  // that closes after a reconnect must not reset the state of the new one.
  socket.onopen = () => {
    if (ws !== socket) return;
    connected = true;
    authenticated = false;
    reconnectDelay = RECONNECT_MIN;
    lastMessageAt = Date.now();
    log("connected to bridge");
    startHeartbeat();
    startHealthCheck();
    broadcastStatus();
    // Kick off OEP handshake in the background. Failure is non-fatal: the
    // bridge still accepts plaintext tool calls, so we fall back to Reasoned
    // mode rather than refusing to work.
    initOep().catch((e) => log("OEP init failed:", String(e)));
  };

  socket.onmessage = (ev) => {
    if (ws !== socket) return;
    lastMessageAt = Date.now();
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }
    handleBridgeMessage(msg);
  };

  socket.onclose = () => {
    if (ws !== socket) return;
    connected = false;
    authenticated = false;
    mcpAlive = false;
    studioConnected = null;
    studioApp = null;
    studioProc = null;
    serversCache = [];
    bridgeVersion = null;
    oepSession = null;
    oepReady = false;
    stopHeartbeat();
    stopHealthCheck();
    failAllPending("bridge connection closed");
    broadcastStatus();
    scheduleReconnect();
  };

  socket.onerror = () => {
    if (ws !== socket) return;
    try { socket.close(); } catch {}
  };
}

function scheduleReconnect() {
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(connect, reconnectDelay);
  reconnectDelay = Math.min(reconnectDelay * 1.5, RECONNECT_MAX);
  log(`Reconnecting in ${reconnectDelay}ms...`);
}

function startHeartbeat() {
  stopHeartbeat();
  heartbeatTimer = setInterval(() => {
    if (connected) {
      if (lastMessageAt && Date.now() - lastMessageAt > STALE_SOCKET_MS) {
        log("socket stale, forcing reconnect");
        try { ws.close(); } catch {}
        return;
      }
      send({ type: "ping" }).catch(() => {});
      refreshStudioStatus();
    }
  }, HEARTBEAT_MS);
}

function stopHeartbeat() {
  clearInterval(heartbeatTimer);
  heartbeatTimer = null;
}

function startHealthCheck() {
  stopHealthCheck();
  healthCheckTimer = setInterval(async () => {
    try {
      const response = await fetch(`${HEALTH_URL}/health`, { method: "GET", cache: "no-cache", credentials: "omit", mode: "cors", signal: AbortSignal.timeout(5000) });
      if (response.ok) {
        const health = await response.json();
        log("Health check OK:", health.status);
      }
    } catch (e) {
      log("Health check failed:", e);
    }
  }, HEALTH_CHECK_INTERVAL);
}

function stopHealthCheck() {
  clearInterval(healthCheckTimer);
  healthCheckTimer = null;
}

function waitForConnection(timeout = 8000) {
  return new Promise((resolve) => {
    if (connected && ws && ws.readyState === WebSocket.OPEN) return resolve(true);
    connect();
    const t0 = Date.now();
    const iv = setInterval(() => {
      if (connected && ws && ws.readyState === WebSocket.OPEN) {
        clearInterval(iv);
        resolve(true);
      } else if (Date.now() - t0 > timeout) {
        clearInterval(iv);
        resolve(false);
      }
    }, 100);
  });
}

// ── request/response over the socket ────────────────────────────────────
async function send(obj, timeout = REQUEST_TIMEOUT_DEFAULT) {
  if (!connected || !ws || ws.readyState !== WebSocket.OPEN) {
    await waitForConnection(8000);
  }
  return new Promise((resolve) => {
    if (!connected || !ws || ws.readyState !== WebSocket.OPEN) {
      resolve({ ok: false, kind: "disconnected", error: "bridge not connected" });
      return;
    }
    const id = nextId++;
    const payload = { ...obj, id };
    const timer = setTimeout(() => {
      if (pending.has(id)) {
        pending.delete(id);
        resolve({ ok: false, kind: "timeout", error: "bridge did not respond in time" });
      }
    }, timeout);
    pending.set(id, { resolve, timer });
    try {
      ws.send(JSON.stringify(payload));
    } catch (e) {
      clearTimeout(timer);
      pending.delete(id);
      resolve({ ok: false, kind: "disconnected", error: String(e) });
    }
  });
}

let studioProbing = false;
async function refreshStudioStatus() {
  if (studioProbing || !connected) return;
  studioProbing = true;
  try {
    const r = await send({ type: "studio_status" }, 12000);
    const v = r && r.ok && typeof r.studio === "boolean" ? r.studio : null;
    if (v !== studioConnected) {
      studioConnected = v;
      broadcastStatus();
    }
  } finally {
    studioProbing = false;
  }
}

// â”€â”€ OEP handshake â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
// Sends our X25519 public key, waits for the bridge to answer with its own,
// derives the shared AES-256-GCM session key. The provider never sees any
// of this - only the resulting [ENV:v1:...] ciphertext ever enters the chat.
async function initOep() {
  if (!oepEnabled) return;
  if (!globalThis.SnapgentOEP) {
    log("OEP not available; staying in Reasoned mode");
    oepEnabled = false;
    return;
  }
  const supported = await globalThis.SnapgentOEP.OEP.supported();
  if (!supported) {
    log("OEP unsupported in this browser; staying in Reasoned mode");
    oepEnabled = false;
    return;
  }
  oepSession = new globalThis.SnapgentOEP.OEP();
  const pub = await oepSession.publicKeyB64();
  const r = await send({ type: "oep_hello", pub }, 10000);
  if (!r || !r.ok) {
    log("OEP hello failed:", r && r.error);
    oepSession = null;
    return;
  }
  await oepSession.establish(r.pub, r.session_id);
  oepReady = true;
  log("OEP session established");
}

// Routes a tool call through OEP when the session is live; falls back to the
// plaintext path when OEP is disabled or the handshake has not completed yet.
async function callToolOep(name, args, timeout, server) {
  if (!oepReady || !oepSession) return null;
  const env = await oepSession.wrapEnvelope({
    name,
    arguments: args || {},
    timeout_ms: timeout || 120000,
    server: server || undefined,
  });
  const r = await send(
    { type: "oep_call_tool", env },
    (timeout || 120000) + 15000
  );
  if (!r || !r.ok) return r;
  if (!r.env) {
    return { ok: false, kind: "oep_error", error: "bridge sent no envelope" };
  }
  try {
    const plain = await oepSession.unwrapResult(r.env);
    return plain;
  } catch (e) {
    return { ok: false, kind: "oep_error", error: String(e) };
  }
}

function handleBridgeMessage(msg) {
  if ("studio" in msg && (typeof msg.studio === "boolean" || msg.studio === null)) {
    studioConnected = msg.studio;
  }
  if ("studio_app" in msg && (typeof msg.studio_app === "boolean" || msg.studio_app === null)) {
    studioApp = msg.studio_app;
  }
  if ("studio_proc" in msg && (typeof msg.studio_proc === "boolean" || msg.studio_proc === null)) {
    studioProc = msg.studio_proc;
  }
  if (msg.type === "studio_status") {
    resolvePending(msg.id, { ok: true, studio: studioConnected });
    broadcastStatus();
    return;
  }
  if (msg.type === "connected") {
    bridgeVersion = typeof msg.bridge_version === "string" ? msg.bridge_version : null;
    mcpAlive = !!msg.mcp_alive;
    // The bridge only reaches this frame after the handshake secret was
    // accepted, so the socket is authenticated.
    authenticated = true;
    if (Array.isArray(msg.tools)) toolsCache = msg.tools;
    if (Array.isArray(msg.servers)) serversCache = msg.servers;
    broadcastStatus();
    return;
  }
  if (msg.type === "pong") {
    resolvePending(msg.id, { ok: true });
    return;
  }
  if (msg.type === "tools") {
    if (Array.isArray(msg.tools)) toolsCache = msg.tools;
    if (Array.isArray(msg.servers)) serversCache = msg.servers;
    mcpAlive = !!msg.mcp_alive;
    resolvePending(msg.id, { ok: true, tools: toolsCache });
    broadcastStatus();
    return;
  }
  if (msg.type === "oep_ready") {
    resolvePending(msg.id, { ok: true, pub: msg.pub, session_id: msg.session_id });
    return;
  }
  if (msg.type === "oep_result") {
    resolvePending(msg.id, { ok: true, env: msg.env });
    return;
  }
  if (msg.type === "oep_error") {
    resolvePending(msg.id, { ok: false, kind: "oep_error", error: msg.error });
    return;
  }
  if (msg.type === "tool_result") {
    const result = msg.ok
      ? { ok: true, text: msg.text, images: msg.images || [] }
      : { ok: false, kind: msg.kind, error: msg.error };
    addToolCall({ name: msg.name, arguments: msg.arguments, result, timestamp: Date.now() });
    resolvePending(msg.id, result);
    return;
  }
  if (msg.type === "mcp_status") {
    mcpAlive = !!msg.alive;
    if (Array.isArray(msg.tools)) toolsCache = msg.tools;
    if (Array.isArray(msg.servers)) serversCache = msg.servers;
    resolvePending(msg.id, { ok: !!msg.ok, alive: msg.alive, error: msg.error });
    broadcastStatus();
    return;
  }
  if (msg.type === "server_changed") {
    resolvePending(msg.id, { ok: !!msg.ok, error: msg.error, restarting: !!msg.restarting });
    return;
  }
  if (msg.type === "error") {
    resolvePending(msg.id, { ok: false, error: msg.error });
    return;
  }
  if (msg.type === "auth_token") {
    // The bridge may still hand the secret over the socket for non-browser
    // clients. The extension already holds it (fetched from /auth-token), so
    // there is nothing to reconnect: store it and answer the caller.
    if (msg.ok && msg.token) {
      authToken = msg.token;
      saveSession();
      log("Received auth token from bridge");
    }
    resolvePending(msg.id, { ok: !!msg.ok, token: msg.token });
    return;
  }
  if (msg.type === "mesh_task") {
    routeMeshTask(msg.task || {});
    return;
  }
  if (msg.type === "mesh_run_finished" || msg.type === "mesh_run_started") {
    broadcastMeshEvent(msg);
    return;
  }
  if (msg.type === "a2a_ack") {
    resolvePending(msg.id, { ok: true, ack: msg });
    return;
  }
  if (msg.type === "a2a_agents") {
    resolvePending(msg.id, { ok: true, agents: msg.agents });
    return;
  }
  if (msg.type === "a2a_inbox") {
    resolvePending(msg.id, { ok: true, agent: msg.agent, messages: msg.messages });
    return;
  }
  if (msg.type === "a2a_error") {
    resolvePending(msg.id, { ok: false, error: msg.error });
    return;
  }
}

// ── project mesh routing ─────────────────────────────────────────────────
// A browser worker can only type into the ACTIVE tab of its window, so this
// half is serialized: one task dispatched at a time, and the target tab is
// raised (tabs.active + window focus) instead of being refused. Raising the
// tab is what revives a worker parked in core/main.js:332 waitVisible, which
// resolves on visibilitychange — no code inside the tab has to change.
const meshQueue = [];
let meshDispatching = false;

async function routeMeshTask(task) {
  const threadId = task.threadId;
  if (typeof threadId !== "string" || !threadId) return;
  if (meshInFlight.has(threadId)) return; // pushed twice; the first wins
  meshInFlight.set(threadId, {
    threadId,
    provider: task.provider,
    tabId: null,
    sentAt: Date.now(),
    attempts: 0,
  });
  meshQueue.push(task);
  drainMeshQueue();
}

async function drainMeshQueue() {
  if (meshDispatching) return;
  meshDispatching = true;
  try {
    while (meshQueue.length) {
      const task = meshQueue.shift();
      await meshDispatch(task);
    }
  } finally {
    meshDispatching = false;
  }
}

async function meshDispatch(task) {
  const threadId = task.threadId;
  const entry = meshInFlight.get(threadId);
  if (!entry) return;
  const provider = task.provider;
  let tab = null;
  try {
    tab = await meshTabFor(provider);
  } catch (e) {
    // No tab at all (or unreachable): fail the task now with the reason, so the
    // mesh reports it instead of waiting out the broker timeout.
    log(`mesh: no usable ${provider} tab for ${threadId}: ${e && e.message}`);
    await send(
      { type: "mesh_task_result", threadId, text: `ERROR: ${e && e.message}` },
      MESH_CONTROL_TIMEOUT
    ).catch(() => {});
    meshInFlight.delete(threadId);
    return;
  }
  entry.tabId = tab.id;
  try {
    await meshRaise(tab);
    await chrome.tabs.sendMessage(tab.id, { type: "zs-mesh-task", task });
    entry.attempts = 1;
    securityLog("mesh_task_dispatched", { threadId, provider, tabId: tab.id });
  } catch (e) {
    log(`mesh: dispatch failed for ${threadId}: ${e && e.message}`);
    await send(
      {
        type: "mesh_task_result",
        threadId,
        text: `ERROR: could not reach the ${provider} tab: ${e && e.message}`,
      },
      MESH_CONTROL_TIMEOUT
    ).catch(() => {});
    meshInFlight.delete(threadId);
  }
}

// Raise the tab and focus its window, then wait for the tab to actually be
// visible. A hidden tab has no layout and throttled timers, which is exactly
// why core/main.js refuses to type into one — so the raise has to land before
// the worker starts, not in parallel with it.
async function meshRaise(tab) {
  if (!tab.active) {
    await chrome.tabs.update(tab.id, { active: true });
  }
  await chrome.windows.update(tab.windowId, { focused: true }).catch(() => {});
  const deadline = Date.now() + 5000;
  while (Date.now() < deadline) {
    const now = await chrome.tabs.get(tab.id).catch(() => null);
    if (!now || now.discarded) return;
    const state = await chrome.tabs
      .sendMessage(tab.id, { type: "zs-visibility" })
      .catch(() => null);
    if (state && state.hidden === false) return;
    await new Promise((r) => setTimeout(r, 120));
  }
}

async function meshTabFor(provider) {
  const tabs = await chrome.tabs.query({ url: PROVIDER_URLS });
  let discarded = null;
  for (const t of tabs) {
    if (detectProvider(t.url || "") !== provider) continue;
    if (t.discarded) {
      discarded = discarded || t;
      continue; // sleeping tab; keep looking for a live one
    }
    return t;
  }
  if (discarded) {
    throw new Error(`${provider} tab is suspended (discarded); reload it once`);
  }
  return null;
}

function broadcastMeshEvent(msg) {
  const out = {
    type: msg.type,
    projectId: msg.projectId,
    summary: msg.summary,
    providers: msg.providers,
    alreadyRunning: msg.alreadyRunning,
  };
  chrome.runtime.sendMessage(out).catch(() => {});
  chrome.tabs.query({ url: PROVIDER_URLS }, (tabs) => {
    for (const t of tabs) chrome.tabs.sendMessage(t.id, out).catch(() => {});
  });
}

function resolvePending(id, value) {
  const p = pending.get(id);
  if (!p) return;
  clearTimeout(p.timer);
  pending.delete(id);
  p.resolve(value);
}

function failAllPending(reason) {
  for (const [, p] of pending) {
    clearTimeout(p.timer);
    p.resolve({ ok: false, kind: "disconnected", error: reason });
  }
  pending.clear();
}

// ── status push to any open provider tab + popup ─────────────────────────
function statusObj() {
  return {
    type: "zs-status",
    connected,
    authenticated,
    mcpAlive,
    bridgeVersion,
    studio: studioConnected,
    studioApp,
    studioProc,
    tools: toolsCache.length,
    servers: serversCache,
    authError,
    workspaces: Array.from(workspaceTabs.values())
  };
}

function broadcastStatus() {
  chrome.runtime.sendMessage(statusObj()).catch(() => {});
  chrome.tabs.query({ url: PROVIDER_URLS }, (tabs) => {
    for (const t of tabs) chrome.tabs.sendMessage(t.id, statusObj()).catch(() => {});
  });
}

// ── messages from content.js / popup.js ─────────────────────────────────
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    switch (msg.type) {
      case "status": {
        if (!connected) connect();
        sendResponse(statusObj());
        break;
      }
      case "list_tools": {
        const r = await send({ type: "list_tools" }, 10000);
        if (r.ok) sendResponse({ ok: true, tools: r.tools });
        else sendResponse({ ok: toolsCache.length > 0, tools: toolsCache, error: r.error });
        break;
      }
      case "call_tool": {
        const timeout = (msg.timeout || 120000) + 10000;
        let r = null;
        // Prefer OEP (opaque to the provider). Fall back to plaintext only
        // when the session isn't up yet - never silently downgrade mid-session.
        if (oepReady) {
          try {
            r = await callToolOep(msg.name, msg.arguments, msg.timeout, msg.server);
          } catch (e) {
            r = { ok: false, kind: "oep_error", error: String(e) };
          }
        }
        if (!r) {
          r = await send(
            { type: "call_tool", name: msg.name, arguments: msg.arguments, timeout: msg.timeout, server: msg.server },
            timeout
          );
        }
        addToHistory({ type: "tool_call", name: msg.name, arguments: msg.arguments, result: r, timestamp: Date.now() });
        sendResponse(r);
        break;
      }
      case "restart_mcp": {
        const r = await send({ type: "restart_mcp" }, 30000);
        sendResponse(r);
        break;
      }
      case "add_server": {
        const r = await send({
          type: "add_server", server_id: msg.server_id,
          command: msg.command, args: msg.args, env: msg.env, sandbox: msg.sandbox
        }, 15000);
        sendResponse(r);
        break;
      }
      case "remove_server": {
        const r = await send({ type: "remove_server", server_id: msg.server_id }, 15000);
        sendResponse(r);
        break;
      }
      case "reconnect": {
        reconnectDelay = RECONNECT_MIN;
        connect();
        sendResponse({ ok: true });
        break;
      }
      // ── project mesh: the extension is a WORKER, not a participant ────────
      // The bridge owns the task graph and the parked set; it PUSHES a
      // `mesh_task` frame (see handleBridgeMessage). This half answers it.
      // Every case has a timeout so a dead tab never wedges a mesh task.
      case "mesh_answer": {
        const threadId = msg.threadId;
        const text = msg.text;
        if (typeof threadId !== "string" || typeof text !== "string") {
          sendResponse({ ok: false, error: "mesh_answer needs threadId and text" });
          break;
        }
        const r = await send(
          { type: "mesh_task_result", threadId, text },
          MESH_ANSWER_TIMEOUT
        );
        meshInFlight.delete(threadId);
        addToHistory({ type: "mesh_answer", threadId, ok: !!r.resolved, timestamp: Date.now() });
        // The serialized worker just freed up: take the next parked task.
        drainMeshQueue();
        sendResponse(r);
        break;
      }
      case "mesh_pending": {
        sendResponse({
          ok: true,
          pending: Array.from(meshInFlight.values()),
          queued: meshQueue.map((t) => ({
            threadId: t.threadId,
            provider: t.provider,
          })),
        });
        break;
      }
      case "mesh_control": {
        const r = await send(
          {
            type: msg.action,
            title: msg.title,
            goal: msg.goal,
            projectId: msg.projectId,
            tasks: msg.tasks,
            maxConcurrent: msg.maxConcurrent,
            maxAttempts: msg.maxAttempts,
            sender: msg.sender,
            recipient: msg.recipient,
            note: msg.note,
          },
          MESH_CONTROL_TIMEOUT
        );
        sendResponse(r);
        break;
      }
      case "get_session": {
        sendResponse({ ok: true, session: sessionData });
        break;
      }
      case "save_session": {
        sessionData = msg.session;
        await saveSession();
        sendResponse({ ok: true });
        break;
      }
      case "clear_session": {
        sessionData = { history: [], toolCalls: [], workspaceState: {}, templates: [], createdAt: Date.now(), updatedAt: Date.now() };
        await saveSession();
        sendResponse({ ok: true });
        break;
      }
      case "register_workspace": {
        if (sender.tab) {
          workspaceTabs.set(sender.tab.id, {
            id: sender.tab.id,
            url: sender.tab.url,
            title: sender.tab.title,
            provider: detectProvider(sender.tab.url),
            registeredAt: Date.now()
          });
          broadcastStatus();
        }
        sendResponse({ ok: true });
        break;
      }
      case "unregister_workspace": {
        if (sender.tab) {
          workspaceTabs.delete(sender.tab.id);
          broadcastStatus();
        }
        sendResponse({ ok: true });
        break;
      }
      case "get_workspaces": {
        sendResponse({ ok: true, workspaces: Array.from(workspaceTabs.values()) });
        break;
      }
      case "switch_workspace": {
        if (sender.tab) {
          workspaceTabs.set(sender.tab.id, {
            ...workspaceTabs.get(sender.tab.id),
            ...msg.workspace,
            switchedAt: Date.now()
          });
          broadcastStatus();
        }
        sendResponse({ ok: true });
        break;
      }
      case "add_template": {
        if (sessionData) {
          sessionData.templates.push({ ...msg.template, id: Date.now().toString(), createdAt: Date.now() });
          await saveSession();
        }
        sendResponse({ ok: true });
        break;
      }
      case "remove_template": {
        if (sessionData) {
          sessionData.templates = sessionData.templates.filter(t => t.id !== msg.id);
          await saveSession();
        }
        sendResponse({ ok: true });
        break;
      }
      case "a2a_send": {
        try {
          const frame = {
            type: "a2a_send",
            messageType: typeof msg.messageType === "string" ? msg.messageType : "note",
            sender: typeof msg.sender === "string" && msg.sender ? msg.sender : "snapgent-browser",
            payload: msg.payload !== undefined ? msg.payload : {},
            recipient: typeof msg.recipient === "string" && msg.recipient ? msg.recipient : "zed"
          };
          const r = await send(frame, 15000);
          sendResponse(r && r.ok !== false ? { ok: true, ack: r } : { ok: false, error: (r && r.error) || "a2a_send failed" });
        } catch (e) {
          sendResponse({ ok: false, error: String(e && e.message ? e.message : e) });
        }
        break;
      }
      default:
        sendResponse({ ok: false, error: "unknown message" });
    }
  })();
  return true; // async sendResponse
});

function detectProvider(url) {
  if (url.includes("chat.deepseek.com")) return "deepseek";
  if (url.includes("chatgpt.com") || url.includes("chat.openai.com")) return "chatgpt";
  if (url.includes("claude.ai")) return "claude";
  if (url.includes("gemini.google.com")) return "gemini";
  if (url.includes("kimi.ai")) return "kimi";
  if (url.includes("chat.z.ai")) return "glm";
  if (url.includes("chat.qwen.ai")) return "qwen";
  if (url.includes("arena.ai")) return "arena";
  if (url.includes("meta.ai")) return "meta";
  if (url.includes("ptero.pro")) return "ptero";
  if (url.includes("grok.com")) return "grok";
  if (url.includes("chat.mistral.ai")) return "mistral";
  return "unknown";
}

// Wake/keepalive hooks.
chrome.runtime.onStartup.addListener(async () => {
  await loadSession();
  connect();
});

chrome.runtime.onInstalled.addListener(async () => {
  await loadSession();
  connect();
});

// Load session on startup
loadSession().then(connect);

log("Background service worker initialized");