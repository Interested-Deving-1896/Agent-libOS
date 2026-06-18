import type { GuiConnection, ImageInspectResult, ImageMutationResult, ImageSummary, RuntimeSnapshot, SseMessage } from "./types";

type JsonBody = Record<string, unknown>;
export type OptionalQuanta = number | null;

export class LibOSClient {
  constructor(private connection: GuiConnection) {}

  get db() {
    return this.connection.db;
  }

  updateConnection(connection: GuiConnection) {
    this.connection = connection;
  }

  async snapshot(): Promise<RuntimeSnapshot> {
    return this.request<RuntimeSnapshot>("GET", "/api/snapshot");
  }

  async health() {
    return this.request("GET", "/api/health");
  }

  async images(): Promise<ImageSummary[]> {
    return this.request<ImageSummary[]>("GET", "/api/images");
  }

  async inspectImage(imageId: string): Promise<ImageInspectResult> {
    return this.request<ImageInspectResult>("GET", `/api/images/${encodeURIComponent(imageId)}`);
  }

  async registerImageFromManifest(manifestText: string, source: string, confirmed: boolean, replace = false, actor?: string) {
    return this.request<ImageMutationResult>("POST", "/api/images/register", {
      manifest_text: manifestText,
      source,
      confirmed,
      replace,
      ...(actor ? { actor } : {})
    });
  }

  async createCheckpoint(pid: string, reason: string) {
    return this.request<{ checkpoint_id: string }>("POST", "/api/checkpoints/create", { pid, reason });
  }

  async commitCheckpointToImage({
    checkpointId,
    imageId,
    name,
    version,
    confirmed,
    replace = false,
    actor
  }: {
    checkpointId: string;
    imageId: string;
    name: string;
    version: string;
    confirmed: boolean;
    replace?: boolean;
    actor?: string;
  }) {
    return this.request<ImageMutationResult>("POST", "/api/images/commit", {
      checkpoint_id: checkpointId,
      image_id: imageId,
      name,
      version,
      confirmed,
      replace,
      ...(actor ? { actor } : {})
    });
  }

  async setAutoRun(enabled: boolean) {
    return this.request("POST", "/api/scheduler/auto", { enabled });
  }

  async pauseScheduler() {
    return this.request("POST", "/api/scheduler/pause", {});
  }

  async spawn(goal: string, image: string, maxQuanta: OptionalQuanta, autoRun: boolean) {
    return this.request("POST", "/api/processes", withOptionalQuanta({ goal, image, auto_run: autoRun }, maxQuanta));
  }

  async run(pid: string, maxQuanta: OptionalQuanta) {
    return this.request("POST", `/api/processes/${encodeURIComponent(pid)}/run`, withOptionalQuanta({}, maxQuanta));
  }

  async step(pid: string) {
    return this.request("POST", `/api/processes/${encodeURIComponent(pid)}/step`, {});
  }

  async pauseProcess(pid: string) {
    return this.request("POST", `/api/processes/${encodeURIComponent(pid)}/pause`, { reason: "paused from GUI" });
  }

  async resumeProcess(pid: string, autoRun: boolean) {
    return this.request("POST", `/api/processes/${encodeURIComponent(pid)}/resume`, { auto_run: autoRun });
  }

  async sendMessage(pid: string, body: string, kind: "message" | "interrupt", autoRun: boolean, maxQuanta: OptionalQuanta) {
    return this.request("POST", `/api/processes/${encodeURIComponent(pid)}/${kind}`, withOptionalQuanta({
      body,
      auto_run: autoRun,
      channel: "gui"
    }, maxQuanta));
  }

  async changeDirectory(pid: string, path: string) {
    return this.request("POST", `/api/processes/${encodeURIComponent(pid)}/cd`, { path });
  }

  async execProcess(pid: string, image: string, goal: string, confirmed: boolean, autoRun: boolean) {
    return this.request("POST", `/api/processes/${encodeURIComponent(pid)}/exec`, {
      image,
      goal,
      confirmed,
      auto_run: autoRun
    });
  }

  async exitProcess(pid: string, message: string, failed: boolean, confirmed: boolean) {
    return this.request("POST", `/api/processes/${encodeURIComponent(pid)}/exit`, { message, failed, confirmed });
  }

  async respondHumanRequest(requestId: string, approved: boolean, answer: string) {
    return this.request("POST", `/api/human-requests/${encodeURIComponent(requestId)}/respond`, {
      approved,
      answer,
      auto_run: true
    });
  }

  async request<T = unknown>(method: string, path: string, body?: JsonBody): Promise<T> {
    const response = await fetch(`${this.connection.url}${path}`, {
      method,
      headers: {
        Authorization: `Bearer ${this.connection.token}`,
        "Content-Type": "application/json"
      },
      body: body === undefined ? undefined : JSON.stringify(body)
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      const message = payload?.error?.message ?? `HTTP ${response.status}`;
      const error = new Error(message) as Error & { status?: number; payload?: unknown };
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload as T;
  }

  async stream(onMessage: (message: SseMessage) => void, signal: AbortSignal, cursor = "0") {
    const response = await fetch(`${this.connection.url}/api/events/stream?cursor=${encodeURIComponent(cursor)}`, {
      headers: { Authorization: `Bearer ${this.connection.token}` },
      signal
    });
    if (!response.ok || !response.body) throw new Error(`SSE connection failed: ${response.status}`);
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (!signal.aborted) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let boundary = buffer.indexOf("\n\n");
      while (boundary >= 0) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const parsed = parseSseFrame(frame);
        if (parsed) onMessage(parsed);
        boundary = buffer.indexOf("\n\n");
      }
    }
  }
}

function withOptionalQuanta(body: JsonBody, maxQuanta: OptionalQuanta): JsonBody {
  return maxQuanta === null ? body : { ...body, max_quanta: maxQuanta };
}

export function parseSseFrame(frame: string): SseMessage | null {
  const lines = frame.split(/\r?\n/).filter((line) => line.trim() && !line.startsWith(":"));
  if (lines.length === 0) return null;
  let id = "";
  let event = "message";
  const data: string[] = [];
  for (const line of lines) {
    if (line.startsWith("id:")) id = line.slice(3).trim();
    if (line.startsWith("event:")) event = line.slice(6).trim();
    if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }
  if (data.length === 0) return { id, event, data: null };
  return { id, event, data: JSON.parse(data.join("\n")) };
}
