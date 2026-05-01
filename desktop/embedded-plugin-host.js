const path = require("path");
const { spawn } = require("child_process");

const embeddedHosts = new Map();

function buildError(message) {
  return new Error(String(message || "Embedded plugin host failed."));
}

function parseHostLine(line, host) {
  const trimmed = String(line || "").trim();
  if (!trimmed) return;
  
  if (trimmed === "READY") {
    host.ready = true;
    host.readyResolve?.({ 
      ready: true, 
      trackId: host.trackId,
      width: host.preferredWidth,
      height: host.preferredHeight
    });
    host.readyResolve = null;
    host.readyReject = null;
    return;
  }
  
  if (trimmed.startsWith("SIZE ")) {
    const parts = trimmed.split(" ");
    if (parts.length >= 3) {
      host.preferredWidth = parseInt(parts[1], 10) || 800;
      host.preferredHeight = parseInt(parts[2], 10) || 600;
    }
    return;
  }
  
  if (trimmed === "AUDIO_ENGINE_STARTED") {
    host.audioEngineReady = true;
    return;
  }
  
  if (trimmed.startsWith("ERROR ")) {
    const error = buildError(trimmed.slice("ERROR ".length));
    if (!host.ready) {
      host.readyReject?.(error);
      host.readyResolve = null;
      host.readyReject = null;
    }
    host.lastError = error.message;
  }
}

function wireProcessOutput(host) {
  let stdoutBuffer = "";
  let stderrBuffer = "";
  
  host.child.stdout?.on("data", (chunk) => {
    stdoutBuffer += chunk.toString("utf8");
    const lines = stdoutBuffer.split(/\r?\n/);
    stdoutBuffer = lines.pop() || "";
    lines.forEach((line) => parseHostLine(line, host));
  });
  
  host.child.stderr?.on("data", (chunk) => {
    stderrBuffer += chunk.toString("utf8");
    const lines = stderrBuffer.split(/\r?\n/);
    stderrBuffer = lines.pop() || "";
    lines.forEach((line) => {
      if (line.trim()) host.lastError = line.trim();
    });
  });
}

function writeCommand(host, command) {
  if (!host?.child || host.child.killed || host.child.exitCode !== null) {
    throw buildError("The embedded plugin host is not running.");
  }
  host.child.stdin.write(`${command}\n`);
}

async function destroyEmbeddedPluginHost(trackId) {
  const host = embeddedHosts.get(trackId);
  if (!host) return { closed: false };
  
  embeddedHosts.delete(trackId);
  try {
    if (host.child.exitCode === null) {
      writeCommand(host, "QUIT");
    }
  } catch {}
  
  if (host.child.exitCode === null) {
    host.child.kill();
  }
  return { closed: true };
}

async function ensureEmbeddedPluginHost({
  trackId,
  pluginPath,
  pluginName,
  rootDir,
  auHostBinary,
  windowHandle
}) {
  const existing = embeddedHosts.get(trackId);
  if (existing && existing.pluginPath === pluginPath && existing.child.exitCode === null) {
    return existing.readyPromise;
  }
  
  await destroyEmbeddedPluginHost(trackId);
  
  const windowId = windowHandle || "embedded";
  const args = ["--embed", windowId, pluginPath];
  
  const child = spawn(auHostBinary, args, {
    cwd: rootDir,
    stdio: ["pipe", "pipe", "pipe"]
  });
  
  const host = {
    trackId,
    child,
    pluginPath,
    pluginName,
    windowId,
    ready: false,
    preferredWidth: 800,
    preferredHeight: 600,
    lastError: "",
    readyPromise: null,
    readyResolve: null,
    readyReject: null
  };
  
  host.readyPromise = new Promise((resolve, reject) => {
    host.readyResolve = resolve;
    host.readyReject = reject;
  });
  
  child.once("error", (error) => {
    host.lastError = error.message;
    host.readyReject?.(error);
    embeddedHosts.delete(trackId);
  });
  
  child.once("exit", (code) => {
    if (!host.ready) {
      host.readyReject?.(buildError(host.lastError || `Plugin host exited (code ${code ?? "unknown"}).`));
    }
    embeddedHosts.delete(trackId);
  });
  
  wireProcessOutput(host);
  embeddedHosts.set(trackId, host);
  
  return host.readyPromise;
}

function getEmbeddedPluginHostInfo(trackId) {
  const host = embeddedHosts.get(trackId);
  if (!host) return null;
  return {
    ready: host.ready,
    preferredWidth: host.preferredWidth,
    preferredHeight: host.preferredHeight,
    pluginName: host.pluginName,
    lastError: host.lastError
  };
}

async function shutdownAllEmbeddedHosts() {
  await Promise.all([...embeddedHosts.keys()].map((trackId) => destroyEmbeddedPluginHost(trackId)));
}

module.exports = {
  destroyEmbeddedPluginHost,
  ensureEmbeddedPluginHost,
  getEmbeddedPluginHostInfo,
  shutdownAllEmbeddedHosts
};
