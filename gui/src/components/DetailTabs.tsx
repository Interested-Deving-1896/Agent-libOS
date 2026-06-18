import { useState } from "react";
import type { RuntimeProcess, RuntimeSnapshot } from "../api/types";
import { useI18n, type TranslationKey } from "../i18n";
import { CollapsibleJson } from "./CollapsibleJson";

const tabs = [
  { key: "overview", label: "details.overview" },
  { key: "capabilities", label: "details.capabilities" },
  { key: "toolsSkills", label: "details.toolsSkills" },
  { key: "checkpoints", label: "details.checkpoints" },
  { key: "audit", label: "details.audit" },
  { key: "llmCalls", label: "details.llmCalls" },
  { key: "jsonRpc", label: "details.jsonRpc" },
  { key: "objectMemory", label: "details.objectMemory" }
] as const satisfies ReadonlyArray<{ key: string; label: TranslationKey }>;

type TabKey = (typeof tabs)[number]["key"];

export function DetailTabs({ process, snapshot }: { process: RuntimeProcess | null; snapshot: RuntimeSnapshot | null }) {
  const { t } = useI18n();
  const [tab, setTab] = useState<TabKey>("overview");
  if (!snapshot) return <div className="empty">{t("details.snapshotMissing")}</div>;
  return (
    <aside className="details">
      <div className="tabs" role="tablist">
        {tabs.map(({ key, label }) => (
          <button key={key} className={tab === key ? "active" : ""} onClick={() => setTab(key)}>
            {t(label)}
          </button>
        ))}
      </div>
      <div className="tabPanel">{renderTab(tab, process, snapshot, t)}</div>
    </aside>
  );
}

function renderTab(tab: TabKey, process: RuntimeProcess | null, snapshot: RuntimeSnapshot, t: (key: TranslationKey) => string) {
  if (!process && tab !== "jsonRpc" && tab !== "toolsSkills") return <div className="empty">{t("details.selectProcess")}</div>;
  if (tab === "overview") return <JsonBlock value={process} />;
  if (tab === "capabilities") return <JsonBlock value={{ capability_ids: process?.capabilities }} />;
  if (tab === "toolsSkills") return <JsonBlock value={{ process_tools: process?.tool_table, loaded_skills: process?.loaded_skills, registry: snapshot.skills, tools: snapshot.tools }} />;
  if (tab === "checkpoints") return <JsonBlock value={{ checkpoint_head: process?.checkpoint_head }} />;
  if (tab === "audit") return <JsonBlock value={snapshot.audit.filter((item) => item.actor === process?.pid || item.target === `process:${process?.pid}`)} />;
  if (tab === "llmCalls") return <JsonBlock value={snapshot.llm_calls.filter((item) => item.pid === process?.pid)} />;
  if (tab === "jsonRpc") return <JsonBlock value={snapshot.jsonrpc_endpoints} />;
  return <JsonBlock value={{ goal_oid: process?.goal_oid, note: t("details.objectMemoryNote") }} />;
}

function JsonBlock({ value }: { value: unknown }) {
  const { t } = useI18n();
  return <CollapsibleJson value={value} label={t("details.rawData")} />;
}
