/// <reference types="vite/client" />

export type GuiConnection = {
  url: string;
  token: string;
  db: string;
};

declare global {
  interface Window {
    libosApi?: {
      getConnection(): Promise<GuiConnection | null>;
      chooseDatabase(): Promise<GuiConnection | null>;
      useDatabase(db: string): Promise<GuiConnection | null>;
      openExternal(url: string): Promise<boolean>;
    };
  }
}
