// popup 逻辑：daemon /status 是真信号（extConnected）；端口可配（chrome.storage ra_port）
const dot = document.getElementById("dot");
const st = document.getElementById("statusText");
const msg = document.getElementById("msg");
const portInput = document.getElementById("port");
document.getElementById("extV").textContent = chrome.runtime.getManifest().version;

function setLight(cls, text, msgText) {
  dot.className = "dot " + cls;
  st.textContent = text;
  if (msgText !== undefined) msg.textContent = msgText;
}

async function currentPort() {
  try {
    const { ra_port } = await chrome.storage.local.get("ra_port");
    return Number(ra_port) || 17890;
  } catch (e) {
    return 17890;
  }
}

async function refresh() {
  const port = await currentPort();
  // 用户正在输入时不回写输入框
  if (document.activeElement !== portInput) portInput.value = port;
  try {
    const r = await fetch("http://127.0.0.1:" + port + "/status", { cache: "no-store" });
    const s = await r.json();
    document.getElementById("cacheV").textContent = s.cachedCookieCount ?? "—";
    if (s.extConnected) {
      setLight("ok", "已连接 daemon · cookie 通道就绪");
      return;
    }
    setLight("warn", "daemon 在线，等待连接", "扩展自动重连中；若长时间等待，点「立即重连」。");
    return;
  } catch (e) { /* daemon 不在线 */ }
  document.getElementById("cacheV").textContent = "—";
  setLight("err", "daemon 离线 · 自动重连中",
    "daemon 离线 — research-assistant fetch/doctor 调用时会自动拉起，无需手动操作。");
}

document.getElementById("reconnect").addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "reconnect" }, () => setTimeout(refresh, 1200));
});

document.getElementById("applyPort").addEventListener("click", () => {
  const p = Number(portInput.value);
  chrome.runtime.sendMessage({ type: "setPort", port: p }, (res) => {
    if (!res?.ok) {
      msg.textContent = "端口无效（1-65535）";
      return;
    }
    setTimeout(refresh, 1200);
  });
});

refresh();
setInterval(refresh, 5000);
