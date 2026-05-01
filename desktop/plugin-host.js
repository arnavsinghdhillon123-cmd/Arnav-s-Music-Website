const fs = require("fs");
const path = require("path");
const { shell } = require("electron");
const { spawn, spawnSync } = require("child_process");

const MACOS_COMPONENT_DIRS = [
  "/Library/Audio/Plug-Ins/Components"
];
const INSTRUMENT_AUDIO_COMPONENT_TYPES = new Set(["aumu", "aumi", "aumv"]);

function getCarlaSinglePath() {
  const commonPaths = [
    "/Applications/Carla.app/Contents/MacOS/carla-single",
    "/usr/local/bin/carla-single",
    "/opt/homebrew/bin/carla-single",
    "/usr/bin/carla-single"
  ];
  for (const carlaPath of commonPaths) {
    if (fs.existsSync(carlaPath)) {
      return carlaPath;
    }
  }
  try {
    const result = spawnSync("which", ["carla-single"], { encoding: "utf8" });
    if (result.status === 0) {
      return result.stdout.trim();
    }
  } catch {
    // fall through
  }
  return null;
}

function openDetached(command, args, cwd) {
  const child = spawn(command, args, {
    cwd,
    detached: true,
    stdio: "ignore"
  });
  child.unref();
}

function compileAuHost({ rootDir, sourcePath, binaryPath }) {
  const binaryDir = path.dirname(binaryPath);
  fs.mkdirSync(binaryDir, { recursive: true });
  const shouldCompile = !fs.existsSync(binaryPath)
    || fs.statSync(sourcePath).mtimeMs > fs.statSync(binaryPath).mtimeMs;
  if (!shouldCompile) {
    return;
  }
  const compile = spawnSync("xcrun", [
        "swiftc",
        "-framework", "Cocoa",
        "-framework", "AVFoundation",
        "-framework", "AudioToolbox",
        "-framework", "CoreAudioKit",
        sourcePath,
        "-o",
        binaryPath
      ], {
        cwd: rootDir,
        encoding: "utf8",
        env: {
          ...process.env,
          CLANG_MODULE_CACHE_PATH: process.env.CLANG_MODULE_CACHE_PATH || "/tmp/waveforge-clang-cache"
        }
      });
  if (compile.status !== 0) {
    throw new Error((compile.stderr || compile.stdout || "Could not compile the built-in AU host.").trim());
  }
}

function getMacAudioUnitDirs() {
  const homeDir = process.env.HOME || "";
  return [
    ...MACOS_COMPONENT_DIRS,
    homeDir ? path.join(homeDir, "Library", "Audio", "Plug-Ins", "Components") : ""
  ].filter(Boolean);
}

function findMatchingAudioUnit(pluginPath) {
  const pluginName = path.parse(pluginPath).name;
  if (!pluginName) {
    return null;
  }
  for (const componentDir of getMacAudioUnitDirs()) {
    const candidate = path.join(componentDir, `${pluginName}.component`);
    if (fs.existsSync(candidate)) {
      return candidate;
    }
  }
  return null;
}

function resolveAudioUnitPluginPath(pluginPath) {
  const resolved = path.resolve(pluginPath);
  const ext = path.extname(resolved).toLowerCase();
  if (ext === ".component") {
    return fs.existsSync(resolved) ? resolved : null;
  }
  if (ext === ".vst" || ext === ".vst3") {
    return findMatchingAudioUnit(resolved);
  }
  return null;
}

function readAudioUnitMetadata(componentPath) {
  const plistPath = path.join(componentPath, "Contents", "Info.plist");
  if (!fs.existsSync(plistPath)) {
    return {
      componentPath,
      componentType: "",
      isInstrument: false
    };
  }
  try {
    const result = spawnSync("plutil", ["-extract", "AudioComponents", "json", "-o", "-", plistPath], {
      encoding: "utf8"
    });
    if (result.status !== 0 || !result.stdout.trim()) {
      return {
        componentPath,
        componentType: "",
        isInstrument: false
      };
    }
    const audioComponents = JSON.parse(result.stdout);
    const first = Array.isArray(audioComponents) ? audioComponents[0] : null;
    const componentType = String(first?.type || "").trim();
    return {
      componentPath,
      componentType,
      isInstrument: INSTRUMENT_AUDIO_COMPONENT_TYPES.has(componentType),
      name: String(first?.description || path.parse(componentPath).name || "Audio Unit").trim() || "Audio Unit"
    };
  } catch {
    return {
      componentPath,
      componentType: "",
      isInstrument: false
    };
  }
}

function openAudioUnitPlugin({
  componentPath,
  rootDir,
  auHostSource,
  auHostBinary
}) {
  compileAuHost({
    rootDir,
    sourcePath: auHostSource,
    binaryPath: auHostBinary
  });
  openDetached(auHostBinary, [componentPath], rootDir);
}

async function openPluginUi({
  pluginPath,
  rootDir,
  platform,
  auHostSource,
  auHostBinary
}) {
  const resolved = path.resolve(pluginPath);
  if (!fs.existsSync(resolved)) {
    throw new Error(`Plugin file was not found: ${resolved}`);
  }

  const pluginHostCommand = String(process.env.PLUGIN_HOST_COMMAND || "").trim();
  if (pluginHostCommand) {
    const [command, ...baseArgs] = pluginHostCommand.split(" ");
    openDetached(command, [...baseArgs, resolved], rootDir);
    return {
      opened: true,
      mode: "host",
      message: `Opened ${path.parse(resolved).name} in the configured local plugin host.`
    };
  }

  const ext = path.extname(resolved).toLowerCase();
  if (platform === "darwin" && (ext === ".vst" || ext === ".vst3")) {
    const carlaPath = getCarlaSinglePath();
    if (carlaPath) {
      openDetached(carlaPath, [ext === ".vst3" ? "vst3" : "vst", resolved], rootDir);
      return {
        opened: true,
        mode: "carla",
        message: `Opening ${path.parse(resolved).name} in Carla plugin host.`
      };
    }
    const matchingAudioUnit = findMatchingAudioUnit(resolved);
    if (matchingAudioUnit) {
      openAudioUnitPlugin({
        componentPath: matchingAudioUnit,
        rootDir,
        auHostSource,
        auHostBinary
      });
      return {
        opened: true,
        mode: "au-fallback",
        message: `Opening ${path.parse(resolved).name} with the installed Audio Unit editor.`
      };
    }
  }

  if (platform === "darwin" && ext === ".component") {
    openAudioUnitPlugin({
      componentPath: resolved,
      rootDir,
      auHostSource,
      auHostBinary
    });
    return {
      opened: true,
      mode: "au-host",
      message: `Opening ${path.parse(resolved).name} in the built-in AU plugin host.`
    };
  }

  shell.showItemInFolder(resolved);
  return {
    opened: false,
    mode: "reveal",
    message: platform === "darwin"
      ? "WaveForge could not open the native plugin editor because no VST/VST3 host is configured. Install Carla or set PLUGIN_HOST_COMMAND."
      : "WaveForge could not open the native plugin editor because no local plugin host is configured for this format."
  };
}

module.exports = {
  compileAuHost,
  openPluginUi,
  readAudioUnitMetadata,
  resolveAudioUnitPluginPath
};
