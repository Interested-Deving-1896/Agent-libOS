import { afterEach, describe, expect, it, vi } from "vitest";
import { LibOSClient, parseSseFrame } from "./client";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("parseSseFrame", () => {
  it("parses named JSON SSE events", () => {
    const message = parseSseFrame('id: 42\nevent: snapshot\ndata: {"ok": true}\n');
    expect(message).toEqual({ id: "42", event: "snapshot", data: { ok: true } });
  });

  it("ignores invalid JSON frames", () => {
    expect(parseSseFrame("id: 43\nevent: snapshot\ndata: {bad}\n")).toBeNull();
  });
});

describe("LibOSClient", () => {
  it("passes initial working directory through spawn requests", async () => {
    const fetchMock = mockFetch({});
    const client = new LibOSClient({ url: "http://127.0.0.1:1", token: "token", db: "local" });

    await client.spawn("goal", "coding-agent:v0", 4, false, { workingDirectory: " src/app " });

    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:1/api/processes",
      expect.objectContaining({
        body: JSON.stringify({
          goal: "goal",
          image: "coding-agent:v0",
          auto_run: false,
          working_directory: "src/app",
          max_quanta: 4
        })
      })
    );
  });

  it("passes max_quanta through exec requests", async () => {
    const fetchMock = mockFetch({});
    const client = new LibOSClient({ url: "http://127.0.0.1:1", token: "token", db: "local" });

    await client.execProcess("pid_1", "base-agent:v0", "goal", true, false, 7);

    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:1/api/processes/pid_1/exec",
      expect.objectContaining({
        body: JSON.stringify({
          image: "base-agent:v0",
          goal: "goal",
          confirmed: true,
          auto_run: false,
          max_quanta: 7
        })
      })
    );
  });

  it("passes auto_run and max_quanta through human responses", async () => {
    const fetchMock = mockFetch({});
    const client = new LibOSClient({ url: "http://127.0.0.1:1", token: "token", db: "local" });

    await client.respondHumanRequest("request_1", true, "answer", false, 3);

    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:1/api/human-requests/request_1/respond",
      expect.objectContaining({
        body: JSON.stringify({
          approved: true,
          answer: "answer",
          auto_run: false,
          max_quanta: 3
        })
      })
    );
  });
});

function mockFetch(payload: unknown) {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: true,
    json: vi.fn().mockResolvedValue(payload)
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}
