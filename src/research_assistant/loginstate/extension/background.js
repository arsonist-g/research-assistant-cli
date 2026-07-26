// Research Assistant Bridge — background service worker (MV3)
// 架构(搬自 cdt)：扩展主动连本地 daemon 保持持久长连接，被动响应 daemon 的 getCookies/ping。
// 保活：daemon 25s ping → onmessage 重置 30s 不活动计时器；chrome.alarms 30s 兜底重连。
// 去噪：重连指数退避(1→60s 封顶)，只在状态变化时记日志。
// 端口：从 chrome.storage.local 读 ra_port（默认 17890），可在 popup 改。

const DEFAULT_PORT = 17890;
const ALARM = "ra-keepalive";
const MAX_LOGS = 30;

const state = {
  status: "disconnected", // disconnected|connecting|connected
  port: DEFAULT_PORT,
  cookieCount: 0,
  served: 0,
  lastEvent: null,
  logs: [],
};

let wsRef = null;
let backoff = 1000; // 重连退避 ms

function wsUrl() {
  return `ws://127.0.0.1:${state.port}/ws`;
}

function persist() {
  try {
    chrome.storage.session.set({ state }).catch(() => {});
  } catch {}
}

function log(text) {
  const time = new Date().toLocaleTimeString();
  state.logs.push(`${time} ${text}`);
  if (state.logs.length > MAX_LOGS) state.logs.shift();
  state.lastEvent = { time, text };
  console.log("[ra-bridge]", text);
  persist();
}

function setStatus(s) {
  if (state.status !== s) {
    state.status = s;
    log("状态 → " + s);
  }
  persist();
}

async function loadPort() {
  try {
    const v = await chrome.storage.local.get("ra_port");
    state.port = Number(v.ra_port) || DEFAULT_PORT;
  } catch {
    state.port = DEFAULT_PORT;
  }
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type === "getStatus") {
    sendResponse({ ...state });
    return false;
  }
  if (msg?.type === "setPort") {
    const p = Number(msg.port);
    if (p > 0 && p < 65536) {
      chrome.storage.local.set({ ra_port: p });
      state.port = p;
      log("端口改为 " + p);
      backoff = 1000;
      if (wsRef) {
        try { wsRef.close(); } catch {}
      } else {
        connect();
      }
      sendResponse({ ok: true, port: p });
    } else {
      sendResponse({ ok: false });
    }
    return false;
  }
  if (msg?.type === "reset") {
    backoff = 1000;
    log("手动重连");
    if (!wsRef) connect();
    sendResponse({ ok: true });
    return false;
  }
  return false;
});

function connect() {
  if (wsRef) return;
  setStatus("connecting");
  let ws;
  try {
    ws = new WebSocket(wsUrl());
    wsRef = ws;
  } catch (e) {
    log("WebSocket 构造失败: " + (e?.message || e));
    scheduleReconnect();
    return;
  }

  ws.onopen = () => {
    backoff = 1000;
    setStatus("connected");
    ws.send(JSON.stringify({ type: "hello" }));
  };

  ws.onmessage = async (event) => {
    let m;
    try {
      m = JSON.parse(event.data);
    } catch {
      return;
    }
    if (m.type === "ping") {
      ws.send(JSON.stringify({ type: "pong" }));
      return;
    }
    if (m.type === "getCookies") {
      try {
        const cookies = await chrome.cookies.getAll({});
        state.cookieCount = cookies.length;
        state.served += 1;
        persist();
        ws.send(JSON.stringify({ type: "cookies", count: cookies.length, data: cookies }));
        log(`响应 getCookies #${state.served}: ${cookies.length} cookie`);
      } catch (e) {
        ws.send(JSON.stringify({ type: "error", message: String(e) }));
        log("读 cookie 报错: " + (e?.message || e));
      }
    }
  };

  ws.onerror = () => {};

  ws.onclose = () => {
    if (wsRef === ws) wsRef = null;
    setStatus("disconnected");
    scheduleReconnect();
  };
}

function scheduleReconnect() {
  if (wsRef) return;
  const wait = backoff;
  backoff = Math.min(backoff * 2, 60000);
  if (wait <= 2000) log(`${wait}ms 后重连…`);
  setTimeout(connect, wait);
}

chrome.alarms.onAlarm.addListener((a) => {
  if (a.name !== ALARM) return;
  if (!wsRef || wsRef.readyState !== WebSocket.OPEN) {
    if (!wsRef) connect();
  }
});

chrome.alarms.create(ALARM, { periodInMinutes: 0.5 });

(async () => {
  await loadPort();
  log("Research Assistant Bridge 启动 (端口 " + state.port + ")");
  connect();
})();
