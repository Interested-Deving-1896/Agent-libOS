import type { HumanRequest, ProcessMessage, RuntimeSnapshot } from "./api/types";

export type UserConversationItem =
  | {
      id: string;
      role: "user";
      time: string;
      text: string;
      message: ProcessMessage;
    }
  | {
      id: string;
      role: "assistant";
      time: string;
      text: string;
      request: HumanRequest;
    }
  | {
      id: string;
      role: "request";
      time: string;
      text: string;
      request: HumanRequest;
    };

export function deriveUserConversation(snapshot: RuntimeSnapshot | null, pid: string | null): UserConversationItem[] {
  if (!snapshot || !pid) return [];
  const process = snapshot.processes.find((item) => item.pid === pid);
  const messages = process?.messages ?? [];
  const items: UserConversationItem[] = [];

  for (const message of messages) {
    if (!isHumanUserMessage(message)) continue;
    items.push({
      id: `message:${message.message_id}`,
      role: "user",
      time: message.created_at,
      text: message.body || message.subject || "(empty message)",
      message
    });
  }

  for (const request of snapshot.human_requests) {
    if (request.pid !== pid) continue;
    if (isHumanOutput(request)) {
      items.push({
        id: `assistant:${request.request_id}`,
        role: "assistant",
        time: request.updated_at || request.created_at,
        text: String(request.payload.message ?? ""),
        request
      });
      continue;
    }
    if (request.status === "pending") {
      items.push({
        id: `request:${request.request_id}`,
        role: "request",
        time: request.created_at,
        text: humanRequestPrompt(request),
        request
      });
    }
  }

  return items.sort((left, right) => left.time.localeCompare(right.time));
}

export function isHumanOutput(request: HumanRequest): boolean {
  return request.status === "delivered" && request.payload?.type === "output";
}

export function isHumanUserMessage(message: ProcessMessage): boolean {
  return message.sender.startsWith("human:") || message.payload?.source === "human_input";
}

export function humanRequestPrompt(request: HumanRequest): string {
  return String(
    request.payload?.question ??
      request.payload?.reason ??
      request.payload?.type ??
      "Human input required"
  );
}
