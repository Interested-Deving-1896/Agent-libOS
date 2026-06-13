import { AlertTriangle, Bot, CheckCircle2, MessageSquare, Radio, UserRound } from "lucide-react";
import type { AuditRecord, HumanRequest, LlmCall, ProcessMessage, RuntimeEvent } from "../api/types";

type TimelineItem =
  | { kind: "message"; time: string; item: ProcessMessage }
  | { kind: "human"; time: string; item: HumanRequest }
  | { kind: "llm"; time: string; item: LlmCall }
  | { kind: "event"; time: string; item: RuntimeEvent }
  | { kind: "audit"; time: string; item: AuditRecord };

export function Timeline({
  pid,
  messages,
  humanRequests,
  llmCalls,
  events,
  audit
}: {
  pid: string | null;
  messages: ProcessMessage[];
  humanRequests: HumanRequest[];
  llmCalls: LlmCall[];
  events: RuntimeEvent[];
  audit: AuditRecord[];
}) {
  if (!pid) return <div className="empty">Select a process to inspect its timeline.</div>;
  const items: TimelineItem[] = [
    ...messages.map((item) => ({ kind: "message" as const, time: item.created_at, item })),
    ...humanRequests.filter((item) => item.pid === pid).map((item) => ({ kind: "human" as const, time: item.created_at, item })),
    ...llmCalls.filter((item) => item.pid === pid).map((item) => ({ kind: "llm" as const, time: item.created_at, item })),
    ...events.filter((item) => item.target === pid || item.source === pid).map((item) => ({ kind: "event" as const, time: item.created_at, item })),
    ...audit.filter((item) => item.actor === pid || item.target === `process:${pid}`).map((item) => ({ kind: "audit" as const, time: item.timestamp, item }))
  ].sort((a, b) => a.time.localeCompare(b.time));

  if (items.length === 0) return <div className="empty">No timeline entries for this process yet.</div>;

  return (
    <section className="timeline" aria-label="Process timeline">
      {items.map((entry, index) => (
        <article className={`timelineItem ${entry.kind}`} key={`${entry.kind}-${entry.time}-${index}`}>
          <div className="timelineIcon">{icon(entry)}</div>
          <div className="timelineBody">
            <div className="timelineHeader">
              <strong>{title(entry)}</strong>
              <time>{formatTime(entry.time)}</time>
            </div>
            <p>{summary(entry)}</p>
            <pre>{JSON.stringify(entry.item, null, 2)}</pre>
          </div>
        </article>
      ))}
    </section>
  );
}

function icon(entry: TimelineItem) {
  if (entry.kind === "message") return entry.item.kind === "interrupt" ? <AlertTriangle size={16} /> : <MessageSquare size={16} />;
  if (entry.kind === "human") return <UserRound size={16} />;
  if (entry.kind === "llm") return <Bot size={16} />;
  if (entry.kind === "audit") return <CheckCircle2 size={16} />;
  return <Radio size={16} />;
}

function title(entry: TimelineItem) {
  if (entry.kind === "message") return `${entry.item.kind} message`;
  if (entry.kind === "human") return `human request ${entry.item.status}`;
  if (entry.kind === "llm") return `LLM ${entry.item.status}`;
  if (entry.kind === "audit") return entry.item.action;
  return entry.item.type;
}

function summary(entry: TimelineItem) {
  if (entry.kind === "message") return entry.item.subject || entry.item.body || "(empty message)";
  if (entry.kind === "human") return String(entry.item.payload?.question ?? entry.item.payload?.type ?? "human interaction");
  if (entry.kind === "llm") return entry.item.error ?? entry.item.response_content ?? entry.item.purpose;
  if (entry.kind === "audit") return entry.item.target ?? "audit record";
  return entry.item.target ?? entry.item.source;
}

function formatTime(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleTimeString();
}
