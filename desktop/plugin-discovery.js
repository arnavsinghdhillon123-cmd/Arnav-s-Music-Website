const fs = require("fs");
const os = require("os");
const path = require("path");
const crypto = require("crypto");

const DEFAULT_SCAN_SECONDS = 12;
const CACHE_TTL_MS = 1000 * 60 * 60 * 6;

function getHomeDir(app) {
  return app?.getPath?.("home") || os.homedir();
}

function expandHome(app, inputPath) {
  if (!inputPath.startsWith("~")) {
    return inputPath;
  }
  return path.join(getHomeDir(app), inputPath.slice(1));
}

function getPluginScanDirs(platform, app) {
  const homeDir = getHomeDir(app);
  if (platform === "darwin") {
    return [
      "~/Library/Audio/Plug-Ins/VST3",
      "/Library/Audio/Plug-Ins/VST3",
      "~/Library/Audio/Plug-Ins/VST",
      "/Library/Audio/Plug-Ins/VST"
    ];
  }
  if (platform === "win32") {
    return [
      process.env.COMMONPROGRAMFILES ? path.join(process.env.COMMONPROGRAMFILES, "VST3") : null,
      process.env.COMMONPROGRAMFILES ? path.join(process.env.COMMONPROGRAMFILES, "Steinberg", "VST2") : null,
      process.env.PROGRAMFILES ? path.join(process.env.PROGRAMFILES, "Steinberg", "VstPlugins") : null,
      process.env["PROGRAMFILES(X86)"] ? path.join(process.env["PROGRAMFILES(X86)"], "Steinberg", "VstPlugins") : null,
      path.join(homeDir, "Documents", "VSTPlugins")
    ].filter(Boolean);
  }
  return [
    "~/.vst3",
    "~/.vst",
    "/usr/local/lib/vst3",
    "/usr/local/lib/vst",
    "/usr/lib/vst3",
    "/usr/lib/vst"
  ];
}

function getPluginSuffixMap(platform) {
  if (platform === "darwin") {
    return new Map([
      [".vst3", "VST3"],
      [".vst", "VST"]
    ]);
  }
  if (platform === "win32") {
    return new Map([
      [".vst3", "VST3"],
      [".dll", "VST"]
    ]);
  }
  return new Map([
    [".vst3", "VST3"],
    [".so", "VST"]
  ]);
}

function getCachePath(app) {
  return path.join(app.getPath("userData"), "waveforge-plugin-scan-cache.json");
}

function readCache(app) {
  try {
    const payload = JSON.parse(fs.readFileSync(getCachePath(app), "utf8"));
    return payload && typeof payload === "object" ? payload : null;
  } catch {
    return null;
  }
}

function writeCache(app, payload) {
  try {
    fs.mkdirSync(app.getPath("userData"), { recursive: true });
    fs.writeFileSync(getCachePath(app), JSON.stringify(payload, null, 2));
  } catch {
    // Cache writes should never block the scan result.
  }
}

function makePluginId(format, resolvedPath) {
  return crypto
    .createHash("sha1")
    .update(`${format}:${resolvedPath}`)
    .digest("hex")
    .slice(0, 16);
}

function makePluginName(pluginPath) {
  const parsed = path.parse(pluginPath);
  return parsed.name || parsed.base || "Unknown Plugin";
}

function safeRealpath(targetPath) {
  try {
    return fs.realpathSync.native ? fs.realpathSync.native(targetPath) : fs.realpathSync(targetPath);
  } catch {
    return path.resolve(targetPath);
  }
}

function safeStat(targetPath) {
  try {
    return fs.statSync(targetPath);
  } catch {
    return null;
  }
}

function shouldUseCachedResult(cache, platform, scanRoots) {
  if (!cache || cache.platform !== platform || !Array.isArray(cache.scanRoots)) {
    return false;
  }
  if ((Date.now() - Number(cache.scannedAtMs || 0)) > CACHE_TTL_MS) {
    return false;
  }
  return JSON.stringify(cache.scanRoots) === JSON.stringify(scanRoots);
}

function scanInstalledPlugins({ app, platform, forceRescan = false, maxSeconds = DEFAULT_SCAN_SECONDS } = {}) {
  const resolvedPlatform = platform || process.platform;
  const suffixMap = getPluginSuffixMap(resolvedPlatform);
  const scanRoots = getPluginScanDirs(resolvedPlatform, app).map((entry) => expandHome(app, entry));
  const cache = forceRescan ? null : readCache(app);
  if (!forceRescan && shouldUseCachedResult(cache, resolvedPlatform, scanRoots)) {
    return {
      ...cache,
      usedCache: true
    };
  }

  const deadline = Date.now() + (Math.max(2, Number(maxSeconds) || DEFAULT_SCAN_SECONDS) * 1000);
  const seenPaths = new Set();
  const seenKeys = new Set();
  const plugins = [];
  let ignoredEntries = 0;

  const pushPlugin = (pluginPath, format) => {
    const resolved = safeRealpath(pluginPath);
    const dedupePathKey = resolved.toLowerCase();
    if (seenPaths.has(dedupePathKey)) {
      return;
    }
    const pluginName = makePluginName(resolved);
    const dedupeKey = `${format}:${pluginName.toLowerCase()}:${dedupePathKey}`;
    if (seenKeys.has(dedupeKey)) {
      return;
    }
    seenPaths.add(dedupePathKey);
    seenKeys.add(dedupeKey);
    plugins.push({
      id: makePluginId(format, resolved),
      name: pluginName,
      format,
      path: resolved,
      source: "desktop-scan",
      platform: resolvedPlatform,
      isExternal: true
    });
  };

  const walk = (rootDir) => {
    if (Date.now() >= deadline) {
      return;
    }
    let entries = [];
    try {
      entries = fs.readdirSync(rootDir, { withFileTypes: true });
    } catch {
      ignoredEntries += 1;
      return;
    }

    for (const entry of entries) {
      if (Date.now() >= deadline) {
        return;
      }
      const entryPath = path.join(rootDir, entry.name);
      const suffix = path.extname(entry.name).toLowerCase();
      const detectedFormat = suffixMap.get(suffix);
      if (detectedFormat) {
        const stat = safeStat(entryPath);
        if (!stat) {
          ignoredEntries += 1;
          continue;
        }
        pushPlugin(entryPath, detectedFormat);
        continue;
      }
      if (entry.isDirectory()) {
        walk(entryPath);
      }
    }
  };

  scanRoots.forEach((rootDir) => {
    if (Date.now() >= deadline || !fs.existsSync(rootDir)) {
      return;
    }
    walk(rootDir);
  });

  plugins.sort((left, right) => left.name.localeCompare(right.name) || left.format.localeCompare(right.format));
  const payload = {
    plugins,
    platform: resolvedPlatform,
    scanRoots,
    scannedAt: new Date().toISOString(),
    scannedAtMs: Date.now(),
    timedOut: Date.now() >= deadline,
    ignoredEntries,
    scanSeconds: Math.max(2, Number(maxSeconds) || DEFAULT_SCAN_SECONDS),
    hostMachine: os.hostname(),
    usedCache: false
  };
  writeCache(app, payload);
  return payload;
}

module.exports = {
  DEFAULT_SCAN_SECONDS,
  scanInstalledPlugins
};
