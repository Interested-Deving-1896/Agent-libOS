import { contextBridge, ipcRenderer } from "electron";

contextBridge.exposeInMainWorld("libosApi", {
  getConnection: () => ipcRenderer.invoke("libos:getConnection"),
  chooseDatabase: () => ipcRenderer.invoke("libos:chooseDatabase"),
  chooseImagePackage: () => ipcRenderer.invoke("libos:chooseImagePackage"),
  useDatabase: (db: string) => ipcRenderer.invoke("libos:useDatabase", db),
  openExternal: (url: string) => ipcRenderer.invoke("libos:openExternal", url)
});
