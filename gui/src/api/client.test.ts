import { describe, expect, it } from "vitest";
import { parseSseFrame } from "./client";

describe("parseSseFrame", () => {
  it("parses named JSON SSE events", () => {
    const message = parseSseFrame('id: 42\nevent: snapshot\ndata: {"ok": true}\n');
    expect(message).toEqual({ id: "42", event: "snapshot", data: { ok: true } });
  });
});
