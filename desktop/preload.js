const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("waveforgeDesktop", {
  platform: process.platform,
  scanPlugins: (options = {}) => ipcRenderer.invoke("waveforge:scan-plugins", options),
  openPluginUi: (pluginPath) => ipcRenderer.invoke("waveforge:open-plugin-ui", pluginPath),
  getBackendInfo: () => ipcRenderer.invoke("waveforge:get-backend-info"),
  requestMicrophoneAccess: () => ipcRenderer.invoke("waveforge:request-microphone-access"),
  ensurePluginInstrument: (payload) => ipcRenderer.invoke("waveforge:ensure-plugin-instrument", payload),
  openPluginInstrumentEditor: (trackId) => ipcRenderer.invoke("waveforge:open-plugin-instrument-editor", trackId),
  focusPluginInstrumentEditor: (trackId) => ipcRenderer.invoke("waveforge:focus-plugin-instrument-editor", trackId),
  pluginInstrumentNoteOn: (payload) => ipcRenderer.invoke("waveforge:plugin-instrument-note-on", payload),
  pluginInstrumentNoteOff: (payload) => ipcRenderer.invoke("waveforge:plugin-instrument-note-off", payload),
  destroyPluginInstrument: (trackId) => ipcRenderer.invoke("waveforge:destroy-plugin-instrument", trackId),
  ensureEmbeddedPlugin: (payload) => ipcRenderer.invoke("waveforge:ensure-embedded-plugin", payload),
  destroyEmbeddedPlugin: (trackId) => ipcRenderer.invoke("waveforge:destroy-embedded-plugin", trackId),
  getEmbeddedPluginInfo: (trackId) => ipcRenderer.invoke("waveforge:get-embedded-plugin-info", trackId),
  saveProjectFile: (payload) => ipcRenderer.invoke("waveforge:save-project-file", payload),
  saveAudioExportFile: (payload) => ipcRenderer.invoke("waveforge:save-audio-export-file", payload),
  showUnsavedChangesDialog: () => ipcRenderer.invoke("waveforge:show-unsaved-changes-dialog")
});
