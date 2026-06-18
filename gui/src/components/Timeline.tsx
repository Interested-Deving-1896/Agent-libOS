import { AlertTriangle, Bot, CheckCircle2, MessageSquare, Radio, UserRound } from "lucide-react";
import type { AuditRecord, HumanRequest, LlmCall, ProcessMessage, RuntimeEvent } from "../api/types";
import { useI18n, type TranslationKey } from "../i18n";
import { CollapsibleJson } from "./CollapsibleJson";
import { isHumanOutput } from "../userConversation";

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
  const { formatTime, t } = useI18n();
  if (!pid) return <div className="empty">{t("timeline.selectProcess")}</div>;
  const items: TimelineItem[] = [
    ...messages.map((item) => ({ kind: "message" as const, time: item.created_at, item })),
    ...humanRequests.filter((item) => item.pid === pid).map((item) => ({ kind: "human" as const, time: item.created_at, item })),
    ...llmCalls.filter((item) => item.pid === pid).map((item) => ({ kind: "llm" as const, time: item.created_at, item })),
    ...events.filter((item) => item.target === pid || item.source === pid).map((item) => ({ kind: "event" as const, time: item.created_at, item })),
    ...audit.filter((item) => item.actor === pid || item.target === `process:${pid}`).map((item) => ({ kind: "audit" as const, time: item.timestamp, item }))
  ].sort((a, b) => a.time.localeCompare(b.time));

  if (items.length === 0) return <div className="empty">{t("timeline.empty")}</div>;

  return (
    <section className="timeline" aria-label={t("timeline.label")}>
      {items.map((entry, index) => (
        <article className={`timelineItem ${entry.kind}`} key={`${entry.kind}-${entry.time}-${index}`}>
          <div className="timelineIcon">{icon(entry)}</div>
          <div className="timelineBody">
            <div className="timelineHeader">
              <strong>{title(entry, t)}</strong>
              <time>{formatTime(entry.time)}</time>
            </div>
            <p className="timelineSummary" title={summary(entry, t)}>{summary(entry, t)}</p>
            <CollapsibleJson value={entry.item} />
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

function title(entry: TimelineItem, t: (key: TranslationKey, vars?: Record<string, string | number>) => string) {
  if (entry.kind === "message") return t("timeline.messageTitle", { kind: entry.item.kind });
  if (entry.kind === "human" && isHumanOutput(entry.item)) return t("timeline.agentOutput");
  if (entry.kind === "human") return t("timeline.humanRequest", { status: entry.item.status });
  if (entry.kind === "llm") return t("timeline.llmStatus", { status: entry.item.status });
  if (entry.kind === "audit") return entry.item.action;
  return entry.item.type;
}

function summary(entry: TimelineItem, t: (key: TranslationKey) => string) {
  if (entry.kind === "message") return entry.item.subject || entry.item.body || t("timeline.emptyMessage");
  if (entry.kind === "human" && isHumanOutput(entry.item)) return String(entry.item.payload.message ?? t("timeline.emptyOutput"));
  if (entry.kind === "human") return String(entry.item.payload?.question ?? entry.item.payload?.type ?? t("timeline.humanInteraction"));
  if (entry.kind === "llm") return entry.item.error ?? entry.item.response_content ?? entry.item.purpose;
  if (entry.kind === "audit") return entry.item.target ?? t("timeline.auditRecord");
  return entry.item.target ?? entry.item.source;
}
