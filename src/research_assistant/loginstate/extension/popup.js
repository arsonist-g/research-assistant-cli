// popup：展示状态 + 改端口 + 强制重连
const $ = (id) => document.getElementById(id);

function clsFor(status) {
  if (status === "connected") return "ok";
  if (status === "connecting") return "warn";
  return "bad";
}

async function refresh() {
  const bg = chrome.runtime.getBackgroundPage
    ? null
    : await chrome.runtime.sendMessage({ type: "getStatus" });
  const s = bg || { status: "unknown", cookieCount: 0, port: 17890, logs: [] };
  $("status").textContent = s.status || "unknown";
  $("status").className = "status " + clsFor(s.status);
  $("count").textContent = s.cookieCount ?? 0;
  $("port").value = s.port || 17890;
  $("logs").textContent = (s.logs || []).join("\n");
}

$("save").addEventListener("click", async () => {
  const port = Number($("port").value);
  await chrome.runtime.sendMessage({ type: "setPort", port });
  setTimeout(refresh, 300);
});

$("reset").addEventListener("click", async () => {
  await chrome.runtime.sendMessage({ type: "reset" });
  setTimeout(refresh, 300);
});

refresh();
