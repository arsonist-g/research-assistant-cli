// postinstall: 建 venv + 安装 Python 包（ADR-0003：uv 优先，回退 python -m venv + pip）。
// PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 —— 用本地浏览器 channel，不下载 Chromium。
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const packageRoot = path.resolve(__dirname, "..", "..");
const venvDir = path.join(packageRoot, ".research-assistant-python");

function run(command, args, options = {}) {
  const env = {
    ...process.env,
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD: "1",
    ...(options.env || {}),
  };
  const result = spawnSync(command, args, {
    cwd: packageRoot,
    stdio: options.stdio || "inherit",
    encoding: "utf8",
    env,
    windowsHide: true,
  });
  if (result.error) {
    return { ok: false, error: result.error };
  }
  return { ok: result.status === 0, status: result.status, stdout: result.stdout || "" };
}

function venvPython() {
  return process.platform === "win32"
    ? path.join(venvDir, "Scripts", "python.exe")
    : path.join(venvDir, "bin", "python");
}

function hasCommand(cmd) {
  const finder = process.platform === "win32" ? "where" : "which";
  const r = run(finder, [cmd], { stdio: "pipe" });
  return r.ok;
}

function pythonCandidates() {
  if (process.platform === "win32") {
    return [
      { command: "py", args: ["-3"] },
      { command: "python", args: [] },
      { command: "python3", args: [] },
    ];
  }
  return [
    { command: "python3", args: [] },
    { command: "python", args: [] },
  ];
}

function findPython() {
  const probe = ["-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"];
  for (const c of pythonCandidates()) {
    const r = run(c.command, [...c.args, ...probe], { stdio: "pipe" });
    if (r.ok) return c;
  }
  return null;
}

// ---- 主流程 ----
let useUv = hasCommand("uv");

// 1) 建 venv
if (!fs.existsSync(venvPython())) {
  console.log("Creating research-assistant Python runtime...");
  if (useUv) {
    const created = run("uv", ["venv", venvDir, "--python", "3.10"]);
    if (!created.ok) {
      console.error("uv venv 创建失败，回退到 python -m venv");
      useUv = false;
    }
  }
  if (!fs.existsSync(venvPython())) {
    const py = findPython();
    if (!py) {
      console.error("research-assistant 需要 Python 3.10+。请安装 Python 后重试：");
      console.error("  npm install -g research-assistant");
      process.exit(1);
    }
    // python -m venv 自带 pip；uv venv 不带（uv 自己管包），故回退时重建带 pip 的 venv
    const created = run(py.command, [...py.args, "-m", "venv", "--clear", venvDir]);
    if (!created.ok) {
      console.error("无法创建 Python 虚拟环境。");
      process.exit(created.status || 1);
    }
    useUv = false;
  }
}

// 2) 安装包（uv venv 用 uv pip；python venv 用 pip）
console.log("Installing research-assistant Python package" + (useUv ? " (via uv)" : "") + "...");
let installOk = false;
if (useUv) {
  const r = run("uv", ["pip", "install", "--python", venvPython(), packageRoot]);
  installOk = r.ok;
}
if (!installOk) {
  // 非 uv 路径（python -m venv 自带 pip）
  const r = run(venvPython(), [
    "-m", "pip", "install", "--disable-pip-version-check", packageRoot,
  ]);
  if (!r.ok) {
    console.error("安装 research-assistant Python 包失败。");
    process.exit(r.status || 1);
  }
}

console.log("research-assistant ready.");
