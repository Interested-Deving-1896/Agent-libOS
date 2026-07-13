import { describe, expect, it } from "vitest";
import * as path from "node:path";
import {
  productionRendererEntryUrl,
  productionRendererOrigin,
  resolveProductionRendererPath
} from "./rendererProtocol.js";

describe("production renderer protocol", () => {
  it("uses the exact origin allowed by the GUI server", () => {
    expect(productionRendererOrigin).toBe("agent-libos://app");
    expect(productionRendererEntryUrl).toBe("agent-libos://app/index.html");
  });

  it("maps app assets inside dist and rejects other origins or traversal", () => {
    const root = path.resolve("/tmp/agent-libos-gui-dist");
    expect(resolveProductionRendererPath(root, "agent-libos://app/index.html")).toBe(
      path.join(root, "index.html")
    );
    expect(resolveProductionRendererPath(root, "agent-libos://app/assets/app.js")).toBe(
      path.join(root, "assets", "app.js")
    );
    expect(resolveProductionRendererPath(root, "agent-libos://untrusted/index.html")).toBeNull();
    expect(resolveProductionRendererPath(root, "agent-libos://user@app/index.html")).toBeNull();
    expect(resolveProductionRendererPath(root, "agent-libos://app:123/index.html")).toBeNull();
    expect(resolveProductionRendererPath(root, "agent-libos://app/%2e%2e/secret.txt")).toBeNull();
  });
});
