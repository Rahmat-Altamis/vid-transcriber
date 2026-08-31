const { contextBridge } = require("electron");

contextBridge.exposeInMainWorld("captionerApp", {
  platform: process.platform,
});
