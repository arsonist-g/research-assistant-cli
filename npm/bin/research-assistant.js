#!/usr/bin/env node
// bin shim：找到 venv python 并转发到 `python -m research_assistant.cli <args>`。
// venv 缺失时自动修复（跑一次 postinstall）。
const { spawn, spawnSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const packageRoot = path.resolve(__dirname, "..", "..");
const callerCwd = process.env.INIT_CWD || process.cwd();
const venvDir = path.join(packageRoot, ".research-assistant-python");
const pythonPath =
  process.platform === "win32"
    ? path.join(venvDir, "Scripts", "python.exe")
    : path.join(venvDir, "bin", "python");

function reinstallHint() {
  console.error("修复：重装 npm 包");
  console.error("  npm install -g research-assistant");
}

if (!fs.existsSync(pythonPath)) {
  const postinstall = path.join(packageRoot, "npm", "scripts", "postinstall.js");
  console.error("research-assistant Python 运行时缺失，尝试自动修复...");
  const repaired = spawnSync(process.execPath, [postinstall], {
    cwd: packageRoot,
    stdio: "inherit",
    windowsHide: true,
    env: { ...process.env, PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD: "1" },
  });
  if (repaired.error) {
    console.error(`运行时修复失败：${repaired.error.message}`);
    reinstallHint();
    process.exit(5);
  }
  if (repaired.status !== 0 || !fs.existsSync(pythonPath)) {
    console.error("research-assistant 找不到 Python 运行时。");
    console.error(`期望位置：${pythonPath}`);
    reinstallHint();
    process.exit(repaired.status || 5);
  }
}

const child = spawn(
  pythonPath,
  ["-m", "research_assistant.cli", ...process.argv.slice(2)],
  {
    cwd: callerCwd,
    stdio: "inherit",
    env: {
      ...process.env,
      RESEARCH_ASSISTANT_PACKAGE_ROOT: packageRoot,
      PYTHONIOENCODING: process.env.PYTHONIOENCODING || "utf-8",
      PYTHONUTF8: process.env.PYTHONUTF8 || "1",
    },
    windowsHide: true,
  }
);

child.on("error", (error) => {
  console.error(`启动 research-assistant 失败：${error.message}`);
  process.exit(5);
});

child.on("close", (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal);
    return;
  }
  process.exit(code ?? 5);
});
