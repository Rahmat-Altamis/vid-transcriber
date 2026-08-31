const { app, BrowserWindow, shell } = require("electron");
const path = require("path");
const http = require("http");
const { spawn } = require("child_process");

const HOST = "127.0.0.1";
const PORT = 8000;
const HEALTH_URL = `http://${HOST}:${PORT}/health`;
const READY_POLL_MS = 500;
const READY_TIMEOUT_MS = 20000;

let backendProcess = null;
let mainWindow = null;

function backendExecutablePath() {
  if (!app.isPackaged) return null;

  const exeName =
    process.platform === "win32"
      ? "CaptionerBackend.exe"
      : "CaptionerBackend";

  return path.join(process.resourcesPath, "backend", exeName);
}

function pingHealth() {
  return new Promise((resolve) => {
    const req = http.get(HEALTH_URL, (res) => {
      res.resume();
      resolve(res.statusCode === 200);
    });

    req.on("error", () => resolve(false));

    req.setTimeout(2000, () => {
      req.destroy();
      resolve(false);
    });
  });
}

async function waitForBackend() {
  const deadline = Date.now() + READY_TIMEOUT_MS;

  while (Date.now() < deadline) {
    if (await pingHealth()) return;

    await new Promise((resolve) =>
      setTimeout(resolve, READY_POLL_MS)
    );
  }

  throw new Error(
    `Backend did not become healthy within ${READY_TIMEOUT_MS}ms`
  );
}

function startBackend() {
  const exe = backendExecutablePath();

  if (exe) {
    backendProcess = spawn(exe, [], {
      cwd: path.dirname(exe),
    });
  } else {
    const repoRoot = path.join(__dirname, "..");

    backendProcess = spawn(
      "python",
      ["server.py"],
      {
        cwd: repoRoot,
      }
    );
  }

  backendProcess.stdout.on("data", (data) => {
    process.stdout.write(`[backend] ${data}`);
  });

  backendProcess.stderr.on("data", (data) => {
    process.stderr.write(`[backend] ${data}`);
  });

  backendProcess.on("exit", (code) => {
    console.log(`[backend] exited (${code})`);
    backendProcess = null;
  });

  return waitForBackend();
}

function stopBackend() {
  if (backendProcess) {
    backendProcess.kill();
    backendProcess = null;
  }
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 900,
    minHeight: 600,
    title: "Video Captioning Agent",

    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  // Load the NEW UI
  mainWindow.loadFile(
    path.join(__dirname, "index.html")
  );

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return {
      action: "deny",
    };
  });

  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

app.whenReady().then(async () => {
  try {
    await startBackend();
    createWindow();
  } catch (err) {
    console.error(
      "[main] backend failed to start:",
      err
    );

    app.quit();
    return;
  }

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on("window-all-closed", () => {
  stopBackend();

  if (process.platform !== "darwin") {
    app.quit();
  }
});

app.on("before-quit", stopBackend);