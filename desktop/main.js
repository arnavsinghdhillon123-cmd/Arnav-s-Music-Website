const { app, BrowserWindow, dialog, ipcMain, session, systemPreferences } = require("electron");
const fs = require("fs");
const http = require("http");
const path = require("path");
const { spawn } = require("child_process");
const { scanInstalledPlugins, DEFAULT_SCAN_SECONDS } = require("./plugin-discovery");
const { openPluginUi } = require("./plugin-host");
const {
  destroyInstrumentPluginHost,
  ensureInstrumentPluginHost,
  focusInstrumentPluginEditor,
  noteOff: pluginInstrumentNoteOff,
  noteOn: pluginInstrumentNoteOn,
  openInstrumentPluginEditor,
  shutdownAllPluginInstrumentHosts
} = require("./plugin-instrument-manager");
const {
  destroyEmbeddedPluginHost,
  ensureEmbeddedPluginHost,
  getEmbeddedPluginHostInfo,
  shutdownAllEmbeddedHosts
} = require("./embedded-plugin-host");

const ROOT = path.resolve(__dirname, "..");
const SERVER_URL = "http://127.0.0.1:8000";
const HEALTH_URL = `${SERVER_URL}/api/stem-health`;
const BACKEND_TIMEOUT_MS = 30000;
const AU_HOST_SOURCE = path.join(__dirname, "au-host.swift");
const AU_HOST_BINARY = path.join(ROOT, "desktop", "bin", "au-host");

let mainWindow = null;
let backendProcess = null;
let backendStartedByApp = false;

function sanitizeProjectFileName(input) {
  return String(input || "Untitled Project")
    .replace(/[\/:*?"<>|]/g, " ")
    .replace(/\s+/g, " ")
    .trim() || "Untitled Project";
}

async function saveProjectFileToDocuments(payload = {}) {
  const projectName = sanitizeProjectFileName(payload?.fileName);
  const autosaveSuffix = payload?.autosave ? ".autosave" : "";
  const fileName = `${projectName}${autosaveSuffix}.waveforge.json`;
  const targetPath = path.join(app.getPath("documents"), fileName);
  fs.writeFileSync(targetPath, String(payload?.content || ""), "utf8");
  return {
    ok: true,
    path: targetPath,
    fileName,
    projectName
  };
}

function nextAvailableFilePath(directory, fileName) {
  const parsed = path.parse(fileName);
  let candidate = path.join(directory, fileName);
  let index = 1;
  while (fs.existsSync(candidate)) {
    candidate = path.join(directory, `${parsed.name} ${index}${parsed.ext}`);
    index += 1;
  }
  return candidate;
}

async function saveAudioExportFileToDownloads(payload = {}) {
  const safeFileName = sanitizeProjectFileName(payload?.fileName || "track.wav");
  const downloadsDir = app.getPath("downloads");
  const targetPath = nextAvailableFilePath(downloadsDir, safeFileName);
  const rawData = payload?.data;
  const buffer = Buffer.from(
    rawData instanceof ArrayBuffer ? rawData : new Uint8Array(rawData || [])
  );
  fs.writeFileSync(targetPath, buffer);
  return {
    ok: true,
    path: targetPath,
    fileName: path.basename(targetPath)
  };
}

async function showUnsavedChangesDialog() {
  const result = await dialog.showMessageBox(mainWindow || undefined, {
    type: "question",
    buttons: ["Save", "Don't Save", "Cancel"],
    defaultId: 0,
    cancelId: 2,
    title: "WaveForge",
    message: "Do you want to save the current project before creating a new one?",
    detail: "You have unsaved changes in this project."
  });
  return { choice: ["save", "discard", "cancel"][result.response] || "cancel" };
}

function configureMediaPermissions() {
  const defaultSession = session.defaultSession;
  defaultSession.setPermissionRequestHandler((_webContents, permission, callback) => {
    if (permission === "media" || permission === "microphone") {
      callback(true);
      return;
    }
    callback(false);
  });
  defaultSession.setPermissionCheckHandler((_webContents, permission) => {
    if (permission === "media" || permission === "microphone") {
      return true;
    }
    return false;
  });
}

async function requestMacMicrophoneAccess() {
  if (process.platform !== "darwin" || !systemPreferences?.askForMediaAccess) {
    return { ok: true, status: "granted" };
  }
  const currentStatus = systemPreferences.getMediaAccessStatus?.("microphone") || "unknown";
  if (currentStatus === "granted") {
    return { ok: true, status: currentStatus };
  }
  if (currentStatus === "denied" || currentStatus === "restricted") {
    return { ok: false, status: currentStatus };
  }
  try {
    const granted = await systemPreferences.askForMediaAccess("microphone");
    const nextStatus = systemPreferences.getMediaAccessStatus?.("microphone") || (granted ? "granted" : "denied");
    return { ok: Boolean(granted), status: nextStatus };
  } catch (error) {
    return { ok: false, status: "error", message: String(error?.message || error) };
  }
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function requestUrl(url) {
  return new Promise((resolve) => {
    const request = http.get(url, (response) => {
      response.resume();
      resolve(response.statusCode >= 200 && response.statusCode < 500);
    });
    request.on("error", () => resolve(false));
    request.setTimeout(2000, () => {
      request.destroy();
      resolve(false);
    });
  });
}

async function isBackendReady() {
  return requestUrl(HEALTH_URL);
}

function getPythonLaunchOptions() {
  if (process.platform === "win32") {
    return [
      { command: "py", args: ["-3", "server.py"] },
      { command: "python", args: ["server.py"] },
      { command: "python3", args: ["server.py"] }
    ];
  }
  return [
    { command: "python3", args: ["server.py"] },
    { command: "python", args: ["server.py"] }
  ];
}

async function ensureBackendRunning() {
  if (await isBackendReady()) {
    return;
  }

  const launchOptions = getPythonLaunchOptions();
  let lastError = null;

  for (const option of launchOptions) {
    try {
      backendProcess = spawn(option.command, option.args, {
        cwd: ROOT,
        stdio: "ignore",
        windowsHide: true
      });
      backendStartedByApp = true;

      backendProcess.on("error", (error) => {
        lastError = error;
      });

      const startedAt = Date.now();
      while (Date.now() - startedAt < BACKEND_TIMEOUT_MS) {
        if (await isBackendReady()) {
          return;
        }
        if (backendProcess.exitCode !== null) {
          break;
        }
        await delay(500);
      }

      if (backendProcess.exitCode === null) {
        backendProcess.kill();
      }
      backendProcess = null;
      backendStartedByApp = false;
    } catch (error) {
      lastError = error;
    }
  }

  throw new Error(
    lastError?.message ||
      "The desktop app could not start the local Python backend. Make sure Python 3 is installed."
  );
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1500,
    height: 930,
    minWidth: 1180,
    minHeight: 760,
    backgroundColor: "#0a0d12",
    title: "Online DAW",
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false
    }
  });

  mainWindow.loadURL(SERVER_URL);

  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

async function bootDesktopApp() {
  try {
    configureMediaPermissions();
    await ensureBackendRunning();
    createWindow();
  } catch (error) {
    await dialog.showMessageBox({
      type: "error",
      title: "Online DAW",
      message: "The desktop app could not start the local backend.",
      detail: String(error?.message || error)
    });
    app.quit();
  }
}

app.whenReady().then(bootDesktopApp);

ipcMain.handle("waveforge:scan-plugins", async (_event, options = {}) => scanInstalledPlugins({
  app,
  platform: process.platform,
  forceRescan: Boolean(options?.force),
  maxSeconds: Number(options?.maxSeconds) || DEFAULT_SCAN_SECONDS
}));
ipcMain.handle("waveforge:open-plugin-ui", async (_event, pluginPath) => openPluginUi({
  pluginPath,
  rootDir: ROOT,
  platform: process.platform,
  auHostSource: AU_HOST_SOURCE,
  auHostBinary: AU_HOST_BINARY
}));
ipcMain.handle("waveforge:get-backend-info", async () => ({
  serverUrl: SERVER_URL,
  healthUrl: HEALTH_URL,
  desktopPlatform: process.platform
}));
ipcMain.handle("waveforge:request-microphone-access", async () => requestMacMicrophoneAccess());
ipcMain.handle("waveforge:ensure-plugin-instrument", async (_event, payload) => ensureInstrumentPluginHost({
  ...payload,
  rootDir: ROOT,
  platform: process.platform,
  auHostSource: AU_HOST_SOURCE,
  auHostBinary: AU_HOST_BINARY
}));
ipcMain.handle("waveforge:open-plugin-instrument-editor", async (_event, trackId) => openInstrumentPluginEditor(trackId));
ipcMain.handle("waveforge:focus-plugin-instrument-editor", async (_event, trackId) => focusInstrumentPluginEditor(trackId));
ipcMain.handle("waveforge:plugin-instrument-note-on", async (_event, payload) => pluginInstrumentNoteOn(payload?.trackId, payload?.midi, payload?.velocity));
ipcMain.handle("waveforge:plugin-instrument-note-off", async (_event, payload) => pluginInstrumentNoteOff(payload?.trackId, payload?.midi));
ipcMain.handle("waveforge:destroy-plugin-instrument", async (_event, trackId) => destroyInstrumentPluginHost(trackId));

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});

app.on("activate", async () => {
  if (BrowserWindow.getAllWindows().length === 0) {
    await bootDesktopApp();
  }
});

ipcMain.handle("waveforge:ensure-embedded-plugin", async (_event, payload) => ensureEmbeddedPluginHost({
  ...payload,
  rootDir: ROOT,
  auHostBinary: AU_HOST_BINARY
}));
ipcMain.handle("waveforge:destroy-embedded-plugin", async (_event, trackId) => destroyEmbeddedPluginHost(trackId));
ipcMain.handle("waveforge:get-embedded-plugin-info", async (_event, trackId) => getEmbeddedPluginHostInfo(trackId));

ipcMain.handle("waveforge:save-project-file", async (_event, payload) => saveProjectFileToDocuments(payload));
ipcMain.handle("waveforge:save-audio-export-file", async (_event, payload) => saveAudioExportFileToDownloads(payload));
ipcMain.handle("waveforge:show-unsaved-changes-dialog", async () => showUnsavedChangesDialog());

app.on("before-quit", () => {
  shutdownAllPluginInstrumentHosts().catch(() => {});
  shutdownAllEmbeddedHosts().catch(() => {});
  if (backendStartedByApp && backendProcess && backendProcess.exitCode === null) {
    backendProcess.kill();
  }
});
