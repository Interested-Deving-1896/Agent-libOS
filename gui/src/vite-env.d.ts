/// <reference types="vite/client" />

export type GuiConnection = {
  url: string;
  token: string;
  db: string;
};

export type ImageManifestFile = {
  path: string;
  name: string;
  content: string;
};

declare global {
  interface Window {
    libosApi?: {
      getConnection(): Promise<GuiConnection | null>;
      chooseDatabase(): Promise<GuiConnection | null>;
      chooseImageManifest(): Promise<ImageManifestFile | null>;
      useDatabase(db: string): Promise<GuiConnection | null>;
      openExternal(url: string): Promise<boolean>;
    };
  }
}
