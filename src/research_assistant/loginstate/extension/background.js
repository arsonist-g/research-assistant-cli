// Research Assistant Bridge 桥扩展 service worker（移植自 Browser-Use Bridge，适配本仓库 daemon 契约）
// 职责单一：连 daemon（ws://127.0.0.1:<port>/ws，端口读 chrome.storage ra_port，默认 17890）
//   → 响应 getCookies（chrome.cookies.getAll 全量含 httpOnly）
// 自愈：WS onclose 指数退避重连 + /status 探测区分「daemon 离线 / 在线未连」+ chrome.alarms 兜底
// 图标即状态：深色底 + 品牌字 + 右下状态圆点（绿=已连/黄=等待/红=离线），绘制全防御
const DEFAULT_PORT = 17890;
const STATE_COLORS = { connected: "#2fbf71", link: "#e0a52e", off: "#ef5a5f" };
const STATE_TITLES = {
  connected: "Research Assistant Bridge: 已连接 daemon · cookie 通道就绪",
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
    wsSend({ type: "hello" });
  };
  socket.onmessage = async (ev) => {
    let m;
    try { m = JSON.parse(ev.data); } catch { return; }
    if (m.type === "getCookies") {
      try {
        const data = await chrome.cookies.getAll({});
        // daemon 不要求 reqId；有则原样回带（无 reqId 的键不落 JSON，与 JS 序列化行为一致）
        const reply = { type: "cookies", data };
        if (m.reqId !== undefined) reply.reqId = m.reqId;
        wsSend(reply);
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
