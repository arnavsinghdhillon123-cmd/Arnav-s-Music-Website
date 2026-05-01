const path = require("path");
const { spawn } = require("child_process");
const {
  compileAuHost,
  readAudioUnitMetadata,
  resolveAudioUnitPluginPath
} = require("./plugin-host");

const hosts = new Map();

function buildError(message) {
  return new Error(String(message || "Plugin instrument host failed."));
}

function parseHostLine(line, host) {
  const trimmed = String(line || "").trim();
  if (!trimmed) {
    return;
  }
  if (trimmed === "READY") {
    host.ready = true;
    host.readyResolve?.({
      ready: true,
      trackId: host.trackId,
      pluginPath: host.pluginPath,
      pluginName: host.pluginName,
      width: host.preferredWidth || 800,
      height: host.preferredHeight || 600
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
      if (line.trim()) {
        host.lastError = line.trim();
      }
    });
  });
}

function writeCommand(host, command) {
  if (!host?.child || host.child.killed || host.child.exitCode !== null) {
    throw buildError("The plugin host is not running.");
  }
  host.child.stdin.write(`${command}\n`);
}

async function destroyInstrumentPluginHost(trackId) {
  const host = hosts.get(trackId);
  if (!host) {
    return { closed: false };
  }
  hosts.delete(trackId);
  try {
    if (host.child.exitCode === null) {
      writeCommand(host, "QUIT");
    }
  } catch {
    // no-op
  }
  if (host.child.exitCode === null) {
    host.child.kill();
  }
  return { closed: true };
}

async function ensureInstrumentPluginHost({
  trackId,
  pluginPath,
  pluginName,
  rootDir,
  platform,
  auHostSource,
  auHostBinary,
  autoOpen = false
}) {
  if (platform !== "darwin") {
    throw buildError("Instrument plugin hosting is currently implemented for macOS Audio Units.");
  }

  const componentPath = resolveAudioUnitPluginPath(pluginPath);
  if (!componentPath) {
    throw buildError("WaveForge could not resolve an Audio Unit version of this plugin for instrument hosting.");
  }
  const metadata = readAudioUnitMetadata(componentPath);
  if (!metadata.isInstrument) {
    throw buildError("That plugin does not appear to be an instrument plugin, so it cannot be loaded as an instrument track.");
  }

  const existing = hosts.get(trackId);
  if (existing && existing.componentPath === componentPath && existing.child.exitCode === null) {
    if (autoOpen) {
      writeCommand(existing, "OPEN");
    }
    return existing.readyPromise;
  }

  await destroyInstrumentPluginHost(trackId);
  compileAuHost({
    rootDir,
    sourcePath: auHostSource,
    binaryPath: auHostBinary
  });

  const args = ["--serve"];
  if (autoOpen) {
    args.push("--show");
  }
  args.push(componentPath);

  const child = spawn(auHostBinary, args, {
    cwd: rootDir,
    stdio: ["pipe", "pipe", "pipe"]
  });

  const host = {
    trackId,
    child,
    ready: false,
    componentPath,
    pluginPath,
    pluginName: pluginName || metadata.name || path.parse(componentPath).name,
    metadata,
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
    hosts.delete(trackId);
  });
  child.once("exit", (code) => {
    if (!host.ready) {
      host.readyReject?.(buildError(host.lastError || `Plugin host exited before becoming ready (code ${code ?? "unknown"}).`));
    }
    hosts.delete(trackId);
  });

  wireProcessOutput(host);
  hosts.set(trackId, host);
  return host.readyPromise;
}

async function openInstrumentPluginEditor(trackId) {
  const host = hosts.get(trackId);
  if (!host) {
    throw buildError("The plugin instrument host is not ready yet.");
  }
  await host.readyPromise;
  writeCommand(host, "OPEN");
  return {
    opened: true,
    trackId,
    pluginPath: host.pluginPath,
    pluginName: host.pluginName,
    width: host.preferredWidth || 800,
    height: host.preferredHeight || 600
  };
}

async function focusInstrumentPluginEditor(trackId) {
  const host = hosts.get(trackId);
  if (!host) {
    throw buildError("The plugin instrument host is not ready yet.");
  }
  await host.readyPromise;
  writeCommand(host, "FOCUS");
  return {
    focused: true,
    trackId,
    pluginPath: host.pluginPath,
    pluginName: host.pluginName,
    width: host.preferredWidth || 800,
    height: host.preferredHeight || 600
  };
}

async function noteOn(trackId, midi, velocity = 100) {
  const host = hosts.get(trackId);
  if (!host) {
    throw buildError("The plugin instrument host is not ready yet.");
  }
  await host.readyPromise;
  writeCommand(host, `NOTE_ON ${Math.max(0, Math.min(127, Number(midi) || 0))} ${Math.max(1, Math.min(127, Number(velocity) || 100))}`);
  return { ok: true };
}

async function noteOff(trackId, midi) {
  const host = hosts.get(trackId);
  if (!host) {
    return { ok: false };
  }
  await host.readyPromise;
  writeCommand(host, `NOTE_OFF ${Math.max(0, Math.min(127, Number(midi) || 0))}`);
  return { ok: true };
}

async function shutdownAllPluginInstrumentHosts() {
  await Promise.all([...hosts.keys()].map((trackId) => destroyInstrumentPluginHost(trackId)));
}

module.exports = {
  destroyInstrumentPluginHost,
  ensureInstrumentPluginHost,
  focusInstrumentPluginEditor,
  noteOff,
  noteOn,
  openInstrumentPluginEditor,
  shutdownAllPluginInstrumentHosts
};
