import { contextBridge, ipcRenderer } from "electron";

contextBridge.exposeInMainWorld("libosApi", {
  getConnection: () => ipcRenderer.invoke("libos:getConnection"),
  chooseDatabase: () => ipcRenderer.invoke("libos:chooseDatabase"),
  useDatabase: (db: string) => ipcRenderer.invoke("libos:useDatabase", db),
  openExternal: (url: string) => ipcRenderer.invoke("libos:openExternal", url)
});
