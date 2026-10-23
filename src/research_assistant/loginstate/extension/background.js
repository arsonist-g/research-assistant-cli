// Research Assistant Bridge 桥扩展 service worker（移植自 Browser-Use Bridge，适配本仓库 daemon 契约）
// 职责单一：连 daemon（ws://127.0.0.1:<port>/ws，端口读 chrome.storage ra_port，默认 17890）
//   → 响应 getCookies（chrome.cookies.getAll 全量含 httpOnly）
//   → 请求带 wantStorage 时，再采集当前 http/https tab 的 localStorage / sessionStorage 单独回送
// 自愈：WS onclose 指数退避重连 + /status 探测区分「daemon 离线 / 在线未连」+ chrome.alarms 兜底
// 图标即状态：深色底 + 品牌字 + 右下状态圆点（绿=已连/黄=等待/红=离线），绘制全防御
const DEFAULT_PORT = 17890;
// 2 = cookie 与 Web Storage 分两条消息发送（旧 daemon 不认 wantStorage，保持只回 cookie）
const STORAGE_PROTO = 2;
// 单 tab / 整轮 / 冷却预算：一个卡住的 tab 只损失自己的 800ms，不拖垮 cookie 与其它 tab
const STORAGE_TAB_TIMEOUT_MS = 800;
const STORAGE_ROUND_BUDGET_MS = 3000;
const STORAGE_COOLDOWN_MS = 10 * 60 * 1000;
const STORAGE_PER_ORIGIN_BYTES = 1024 * 1024;
const STORAGE_TOTAL_BYTES = 4 * 1024 * 1024;
const stalledTabs = new Map(); // tabId -> 冷却截止时间戳
const STATE_COLORS = { connected: "#2fbf71", link: "#e0a52e", off: "#ef5a5f" };
const STATE_TITLES = {
  connected: "Research Assistant Bridge: 已连接 daemon · 登录态通道就绪（cookie + Web Storage）",
  link: "Research Assistant Bridge: daemon 在线，等待连接（自动重连中）",
  off: "Research Assistant Bridge: daemon 离线 — research-assistant fetch 会自动拉起",
};

let ws = null;
let backoffMs = 1000;
let currentPort = DEFAULT_PORT;

async function loadPort() {
  try {
    const v = await chrome.storage.local.get("ra_port");
    currentPort = Number(v.ra_port) || DEFAULT_PORT;
  } catch (e) {
    currentPort = DEFAULT_PORT;
  }
}

function setUiState(state, detail) {
  try {
    chrome.storage.local.set({ uiState: state, uiDetail: detail ?? "" });
  } catch (e) { /* */ }
}

function drawIcon(state) {
  const dot = STATE_COLORS[state] ?? STATE_COLORS.off;
  try {
    const imageData = {};
    for (const size of [16, 32]) {
      const canvas = new OffscreenCanvas(size, size);
      const ctx = canvas.getContext("2d");
      const s = size / 32;
      ctx.fillStyle = "#1f2229";
      ctx.beginPath();
      if (ctx.roundRect) ctx.roundRect(1 * s, 1 * s, 30 * s, 30 * s, 7 * s);
      else ctx.rect(1 * s, 1 * s, 30 * s, 30 * s);
      ctx.fill();
      ctx.lineWidth = 1.5 * s;
      ctx.strokeStyle = "#3a4150";
      ctx.stroke();
      ctx.fillStyle = "#e8eaee";
      ctx.font = "bold " + Math.round(19 * s) + "px sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText("R", 15 * s, 16 * s);
      ctx.beginPath();
      ctx.arc(24 * s, 24 * s, 6.5 * s, 0, Math.PI * 2);
      ctx.fillStyle = dot;
      ctx.fill();
      ctx.lineWidth = 2 * s;
      ctx.strokeStyle = "#16181d";
      ctx.stroke();
      imageData[size] = ctx.getImageData(0, 0, size, size);
    }
    chrome.action.setIcon({ imageData });
    chrome.action.setBadgeText({ text: "" });
  } catch (e) {
    try {
      chrome.action.setBadgeText({ text: state === "connected" ? "" : "!" });
      chrome.action.setBadgeBackgroundColor({ color: dot });
    } catch (e2) { /* */ }
  }
  try {
    chrome.action.setTitle({ title: STATE_TITLES[state] ?? STATE_TITLES.off });
  } catch (e) { /* */ }
  setUiState(state, STATE_TITLES[state] ?? "");
}

function wsSend(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    try { ws.send(JSON.stringify(obj)); } catch (e) { /* */ }
  }
}

// 在页面主框架里读取 localStorage / sessionStorage。函数体会被 chrome.scripting 序列化，
// 不能引用外部变量（常量、闭包都不行）。
function readStorageInPage() {
  const dump = (s) => {
    const out = {};
    try {
      for (let i = 0; i < s.length; i++) {
        const k = s.key(i);
        if (k === null) continue;
        const v = s.getItem(k);
        if (typeof v === "string") out[k] = v;
      }
    } catch (e) { /* 某些页面禁用 storage */ }
    return out;
  };
  try {
    return { origin: location.origin, l: dump(localStorage), s: dump(sessionStorage) };
  } catch (e) {
    return null;
  }
}

/** 给 promise 加超时；超时返回 null（不 reject，调用方统一按「没答」处理）。 */
function withTimeout(promise, ms) {
  return new Promise((resolve) => {
    let done = false;
    const timer = setTimeout(() => {
      if (!done) { done = true; resolve(null); }
    }, Math.max(0, ms));
    promise.then(
      (v) => { if (!done) { done = true; clearTimeout(timer); resolve(v); } },
      () => { if (!done) { done = true; clearTimeout(timer); resolve(null); } },
    );
  });
}

/**
 * 采集当前打开的 http/https tab 的 Web Storage。
 * 返回 {origins: {origin: {l:{},s:{}}}, truncated, visited, stalled}。
 * 预算：单 tab 800ms、整轮 3s；超时的 tab 冷却 10 分钟；按最近访问排序，同 origin 取第一个。
 */
async function harvestStorage() {
  const started = Date.now();
  const out = { origins: {}, truncated: false, visited: 0, stalled: 0 };
  let totalBytes = 0;

  let tabs = [];
  try {
    tabs = await chrome.tabs.query({ url: ["http://*/*", "https://*/*"] });
  } catch (e) {
    return out;
  }
  // 最近访问的 tab 优先（同 origin 时它的值更新）
  tabs.sort((a, b) => (b.lastAccessed || 0) - (a.lastAccessed || 0));
  const now = Date.now();
  tabs = tabs.filter((t) => t.id != null && !(stalledTabs.get(t.id) > now));

  for (let i = 0; i < tabs.length; i += 4) {
    if (Date.now() - started >= STORAGE_ROUND_BUDGET_MS) break;
    const wave = tabs.slice(i, i + 4);
    const results = await Promise.all(wave.map(async (tab) => {
      const remaining = STORAGE_ROUND_BUDGET_MS - (Date.now() - started);
      if (remaining <= 0) return null;
      const res = await withTimeout(
        chrome.scripting.executeScript({ target: { tabId: tab.id }, func: readStorageInPage }),
        Math.min(STORAGE_TAB_TIMEOUT_MS, remaining),
      );
      if (!res) {
        stalledTabs.set(tab.id, Date.now() + STORAGE_COOLDOWN_MS);
        return null;
      }
      return (res[0] && res[0].result) || null;
    }));

    for (const rec of results) {
      if (!rec || !rec.origin || rec.origin === "null") continue;
      if (out.origins[rec.origin]) continue; // 同 origin 取最近访问的 tab
      const size = JSON.stringify(rec).length;
      if (size > STORAGE_PER_ORIGIN_BYTES) { out.truncated = true; continue; }
      if (totalBytes + size > STORAGE_TOTAL_BYTES) { out.truncated = true; break; }
      totalBytes += size;
      out.origins[rec.origin] = { l: rec.l || {}, s: rec.s || {} };
      out.visited += 1;
    }
  }
  out.stalled = stalledTabs.size;
  return out;
}

function sendStorage(reqId) {
  harvestStorage().then((data) => {
    const msg = { type: "storage", data };
    if (reqId !== undefined) msg.reqId = reqId;
    wsSend(msg);
  }).catch((e) => {
    const msg = { type: "storage", data: { origins: {}, error: String(e) } };
    if (reqId !== undefined) msg.reqId = reqId;
    wsSend(msg);
  });
}

async function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  await loadPort();
  let socket;
  try {
    socket = new WebSocket("ws://127.0.0.1:" + currentPort + "/ws");
  } catch (e) {
    drawIcon("off");
    scheduleReconnect();
    return;
  }
  ws = socket;
  socket.onopen = () => {
    backoffMs = 1000;
    drawIcon("connected");
    wsSend({ type: "hello", storageProto: STORAGE_PROTO });
  };
  socket.onmessage = async (ev) => {
    let m;
    try { m = JSON.parse(ev.data); } catch { return; }
    if (m.type === "getCookies") {
      try {
        const data = await chrome.cookies.getAll({});
        // 先回 cookie：Web Storage 采集可能被卡住的 tab 拖慢，不能让它等
        const reply = { type: "cookies", data, storageProto: STORAGE_PROTO };
        if (m.reqId !== undefined) reply.reqId = m.reqId;
        wsSend(reply);
        if (m.wantStorage) sendStorage(m.reqId);
      } catch (e) {
        wsSend({ type: "error", reqId: m.reqId, message: String(e) });
      }
    } else if (m.type === "ping") {
      wsSend({ type: "pong" });
    }
  };
  socket.onclose = () => {
    if (ws === socket) ws = null;
    // 与 Browser-Use 桥同策略：断开大概率是 daemon 重启/暂离，先按「在线未连」标黄，
    // 下一轮 /status 探测会把真离线校准成红
    drawIcon("link");
    scheduleReconnect();
  };
  socket.onerror = () => {};
}

function scheduleReconnect() {
  setTimeout(connect, backoffMs);
  backoffMs = Math.min(backoffMs * 2, 30000);
}

/** 状态校准：ws 断开时区分「daemon 离线(off)」与「daemon 在线未连(link)」 */
async function probeDaemon() {
  if (ws && ws.readyState === WebSocket.OPEN) return;
  try {
    const r = await fetch("http://127.0.0.1:" + currentPort + "/status", { cache: "no-store" });
    if (r.ok) drawIcon("link");
    else drawIcon("off");
  } catch (e) {
    drawIcon("off");
  }
  connect();
}

chrome.runtime.onMessage.addListener((msg, _s, sendResponse) => {
  if (msg?.type === "reconnect") {
    if (ws) { try { ws.close(); } catch (e) { /* */ } ws = null; }
    probeDaemon();
    sendResponse({ ok: true });
  }
  if (msg?.type === "getStatus") {
    sendResponse({ connected: !!(ws && ws.readyState === WebSocket.OPEN), port: currentPort });
  }
  if (msg?.type === "setPort") {
    const p = Number(msg.port);
    if (p > 0 && p < 65536) {
      currentPort = p;
      try { chrome.storage.local.set({ ra_port: p }); } catch (e) { /* */ }
      if (ws) { try { ws.close(); } catch (e) { /* */ } ws = null; }
      backoffMs = 1000;
      probeDaemon(); // connect() 会用新端口
      sendResponse({ ok: true, port: p });
    } else {
      sendResponse({ ok: false });
    }
  }
  return false;
});

try {
  chrome.alarms.onAlarm.addListener(() => probeDaemon());
  chrome.alarms.create("reconnect", { periodInMinutes: 1 });
} catch (e) { /* */ }

(async () => {
  await loadPort();
  drawIcon("off");
  connect();
})();
try {
  setInterval(probeDaemon, 5000);
} catch (e) { /* */ }
