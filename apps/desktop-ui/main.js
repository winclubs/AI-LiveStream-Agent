/**
 * Electron 桌面控制台：校验外部 Python、托管本地后端，并将持久数据写入 userData。
 * 安装包不内置 Python；当前验收范围为 64 位 CPython 3.12/3.13 与精确核心依赖。
 */
const { app, BrowserWindow, dialog } = require("electron");
const { spawn, spawnSync, execSync } = require("child_process");
const path = require("path");
const http = require("http");
const fs = require("fs");

const packageJson = require("./package.json");
const EXPECTED_VERSION = packageJson.version;
const EXPECTED_SERVICE = "AI-LiveStream-Agent";
const PROJECT_ROOT = app.isPackaged ? process.resourcesPath : path.resolve(__dirname, "..", "..");

let runtime = null;
let mainWindow = null;
let backendProc = null;
let quitInProgress = false;
let startupFailure = "";
let backendLogTail = "";

function buildRuntimeConfig() {
  const host = process.env.LIVE_AGENT_HOST || "127.0.0.1";
  const parsedPort = Number.parseInt(process.env.LIVE_AGENT_PORT || "18080", 10);
  const port = Number.isInteger(parsedPort) && parsedPort > 0 && parsedPort < 65536 ? parsedPort : 18080;
  const dataDir = path.join(app.getPath("userData"), "data");
  const origin = `http://${host}:${port}`;
  return {
    host,
    port,
    dataDir,
    engineUrl: `${origin}/console`,
    versionUrl: `${origin}/api/v1/system/version`,
    readyUrl: `${origin}/readyz`,
    shutdownUrl: `${origin}/api/v1/system/shutdown`,
    env: {
      ...process.env,
      PYTHONUTF8: "1",
      LIVE_AGENT_DATA_DIR: dataDir,
      LIVE_AGENT_HOST: host,
      LIVE_AGENT_PORT: String(port),
    },
  };
}

function samePath(left, right) {
  if (!left || !right) return false;
  const normalize = (value) => path.resolve(value).replace(/[\\/]+$/, "").toLowerCase();
  return normalize(left) === normalize(right);
}

function queryBackendVersion(timeout = 1000) {
  return new Promise((resolve) => {
    const req = http.get(runtime.versionUrl, { timeout }, (res) => {
      if (res.statusCode !== 200) {
        res.resume();
        return resolve(null);
      }
      let data = "";
      res.on("data", (chunk) => (data += chunk));
      res.on("end", () => {
        try { resolve(JSON.parse(data)); } catch (error) { resolve(null); }
      });
    });
    req.on("error", () => resolve(null));
    req.on("timeout", () => { req.destroy(); resolve(null); });
  });
}

function queryBackendReady(timeout = 1000) {
  return new Promise((resolve) => {
    const req = http.get(runtime.readyUrl, { timeout }, (res) => {
      res.resume();
      resolve(res.statusCode === 200);
    });
    req.on("error", () => resolve(false));
    req.on("timeout", () => { req.destroy(); resolve(false); });
  });
}

function killPidTree(pid) {
  if (!pid) return;
  if (process.platform === "win32") {
    try { execSync(`taskkill /F /T /PID ${pid} >nul 2>&1`); } catch (error) {}
  } else {
    try { process.kill(-pid, "SIGTERM"); } catch (error) {
      try { process.kill(pid, "SIGKILL"); } catch (nestedError) {}
    }
  }
}

async function shutdownRemoteInstance(pid) {
  return new Promise((resolve) => {
    const req = http.request(runtime.shutdownUrl, { method: "POST", timeout: 1000 }, () => {
      setTimeout(() => { if (pid) killPidTree(pid); resolve(); }, 500);
    });
    req.on("error", () => { if (pid) killPidTree(pid); resolve(); });
    req.end("{}");
  });
}

function pythonCandidates() {
  const embeddedCandidates = [];

  // 优先探测内置绿色版便携 Python (免去用户配置环境)
  const candidatePaths = [
    path.join(process.resourcesPath, "python", process.platform === "win32" ? "python.exe" : "bin/python3"),
    path.join(PROJECT_ROOT, "python", process.platform === "win32" ? "python.exe" : "bin/python3"),
  ];
  for (const p of candidatePaths) {
    if (fs.existsSync(p)) {
      embeddedCandidates.push({ command: p, prefix: [] });
    }
  }

  if (process.platform === "win32") {
    return [
      ...embeddedCandidates,
      { command: "py", prefix: ["-3.13"] },
      { command: "py", prefix: ["-3.12"] },
      { command: "python", prefix: [] },
    ];
  }
  return [
    ...embeddedCandidates,
    { command: "python3.13", prefix: [] },
    { command: "python3.12", prefix: [] },
    { command: "python3", prefix: [] },
  ];
}

function findValidatedPython() {
  const script = path.join(PROJECT_ROOT, "scripts", "runtime_preflight.py");
  const failures = [];
  for (const candidate of pythonCandidates()) {
    const result = spawnSync(candidate.command, [...candidate.prefix, script], {
      cwd: PROJECT_ROOT,
      env: runtime.env,
      encoding: "utf8",
      windowsHide: true,
      timeout: 30000,
      maxBuffer: 1024 * 1024,
    });
    const output = (result.stdout || result.stderr || result.error?.message || "未找到解释器").trim();
    if (result.status === 0) {
      try {
        const metadata = JSON.parse((result.stdout || "").trim().split(/\r?\n/).pop());
        if (metadata.ok) return { ...candidate, metadata };
      } catch (error) {}
    }
    failures.push(`${candidate.command} ${candidate.prefix.join(" ")}: ${output}`);
  }
  startupFailure = `外部 Python 运行时预检失败。\n${failures.join("\n")}`;
  return null;
}

function spawnBackend(python) {
  return new Promise((resolve) => {
    startupFailure = "";
    backendLogTail = "";
    const args = [...python.prefix, "-m", "server.run"];
    const child = spawn(python.command, args, {
      cwd: PROJECT_ROOT,
      env: runtime.env,
      windowsHide: true,
      detached: process.platform !== "win32",
    });
    backendProc = child;
    let settled = false;
    const finish = (ok) => {
      if (!settled) { settled = true; resolve(ok); }
    };
    child.once("spawn", () => finish(true));
    child.once("error", (error) => {
      startupFailure = `无法启动 Python 子进程：${error.message}`;
      if (backendProc === child) backendProc = null;
      finish(false);
    });
    child.stdout.on("data", (data) => console.log("[backend]", data.toString().trim()));
    child.stderr.on("data", (data) => {
      const text = data.toString();
      backendLogTail = `${backendLogTail}${text}`.slice(-4000);
      console.error("[backend]", text.trim());
    });
    child.on("exit", (code) => {
      if (!startupFailure && code !== 0) startupFailure = `Python 后端提前退出（code=${code}）。\n${backendLogTail}`;
      if (backendProc === child) backendProc = null;
      finish(false);
    });
  });
}

async function ensureBackend() {
  startupFailure = "";
  const meta = await queryBackendVersion();
  if (meta && meta.service === EXPECTED_SERVICE) {
    if (meta.version === EXPECTED_VERSION && samePath(meta.data_dir, runtime.dataDir)) {
      if (await queryBackendReady()) return true;
      startupFailure = "同版本后端仍在初始化或尚未就绪";
      return false;
    }
    if (meta.version === EXPECTED_VERSION) {
      startupFailure = `端口 ${runtime.port} 上已有同版本服务，但数据目录不属于当前桌面用户。\n当前：${meta.data_dir || "未知"}\n预期：${runtime.dataDir}`;
      return false;
    }
    await shutdownRemoteInstance(meta.process_id);
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }

  const python = findValidatedPython();
  if (!python || !(await spawnBackend(python))) return false;

  for (let attempt = 0; attempt < 40; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 500));
    if (!backendProc && startupFailure) return false;
    const current = await queryBackendVersion();
    if (
      current && current.service === EXPECTED_SERVICE &&
      current.version === EXPECTED_VERSION && samePath(current.data_dir, runtime.dataDir) &&
      await queryBackendReady()
    ) return true;
  }
  startupFailure = startupFailure || `后端在 20 秒内未就绪。\n${backendLogTail}`;
  return false;
}

async function showEngineDownDialog() {
  const detail =
    `${startupFailure || "未获得后端启动诊断。"}\n\n` +
    `数据目录：${runtime.dataDir}\n` +
    "安装包不包含 Python。请安装 64 位 CPython 3.12/3.13，并执行：\n" +
    `python -m pip install -r "${path.join(PROJECT_ROOT, "server", "requirements.txt")}"`;
  const { response } = await dialog.showMessageBox(mainWindow, {
    type: "error",
    title: "本地执行引擎未启动",
    message: `无法启动本地核心调度引擎 (${runtime.host}:${runtime.port})。`,
    detail,
    buttons: ["重试", "退出"],
    defaultId: 0,
    cancelId: 1,
  });
  if (response !== 0) return false;
  return ensureBackend();
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1120,
    minHeight: 720,
    title: `AI-LiveStream-Agent 专业版 v${EXPECTED_VERSION}`,
    backgroundColor: "#0f172a",
    webPreferences: { contextIsolation: true, nodeIntegration: false },
  });
  mainWindow.loadURL(runtime.engineUrl);
  mainWindow.webContents.on("did-fail-load", async () => {
    if (!(await ensureBackend()) && !(await showEngineDownDialog())) app.quit();
    else if (mainWindow) mainWindow.loadURL(runtime.engineUrl);
  });
  mainWindow.on("closed", () => { mainWindow = null; });
}

async function startDesktop() {
  if (await ensureBackend()) {
    createWindow();
    return;
  }
  if (await showEngineDownDialog()) createWindow();
  else { process.exitCode = 1; app.quit(); }
}

async function shutdownOwnedBackend() {
  const child = backendProc;
  if (!child) return;
  await new Promise((resolve) => {
    let finished = false;
    const done = () => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      resolve();
    };
    child.once("exit", done);
    const req = http.request(runtime.shutdownUrl, { method: "POST", timeout: 1000 }, (res) => res.resume());
    req.on("error", () => {});
    req.end("{}");
    const timer = setTimeout(() => {
      if (backendProc === child) killPidTree(child.pid);
      done();
    }, 3000);
  });
  if (backendProc === child) backendProc = null;
}

app.whenReady().then(async () => {
  runtime = buildRuntimeConfig();
  await startDesktop();
});

app.on("window-all-closed", () => { if (process.platform !== "darwin") app.quit(); });
app.on("activate", async () => {
  if (BrowserWindow.getAllWindows().length === 0) await startDesktop();
});
app.on("before-quit", (event) => {
  if (backendProc && !quitInProgress) {
    event.preventDefault();
    quitInProgress = true;
    shutdownOwnedBackend().finally(() => app.quit());
  }
});
