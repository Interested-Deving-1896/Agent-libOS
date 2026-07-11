import { useEffect, useState } from "react";
import type { ExplainOperationResponse, ImageInspectResult, OperationListResponse, RuntimeProcess, RuntimeSnapshot } from "../api/types";
import { useI18n, type TranslationKey } from "../i18n";
import { CollapsibleJson } from "./CollapsibleJson";
import { ImagePanel } from "./ImagePanel";
import { ExplainPanel } from "./ExplainPanel";
import { RatingPanel } from "./RatingPanel";

const tabs = [
  { key: "overview", label: "details.overview" },
  { key: "rating", label: "details.rating" },
  { key: "capabilities", label: "details.capabilities" },
  { key: "toolsSkills", label: "details.toolsSkills" },
  { key: "checkpoints", label: "details.checkpoints" },
  { key: "audit", label: "details.audit" },
  { key: "explain", label: "details.explain" },
  { key: "llmCalls", label: "details.llmCalls" },
  { key: "jsonRpc", label: "details.jsonRpc" },
  { key: "mcp", label: "details.mcp" },
  { key: "images", label: "details.images" },
  { key: "objectMemory", label: "details.objectMemory" }
] as const satisfies ReadonlyArray<{ key: string; label: TranslationKey }>;

type TabKey = (typeof tabs)[number]["key"];

export function DetailTabs({
  process,
  snapshot,
  onImportImage,
  onCommitImage,
  onUseImageForSpawn,
  onUseImageForExec,
  onRate,
  onInspectImage,
  onListOperations,
  onExplainOperation,
  onResolveOperation,
  explainLookup
}: {
  process: RuntimeProcess | null;
  snapshot: RuntimeSnapshot | null;
  onImportImage(replace: boolean): void;
  onCommitImage(request: { imageId: string; name: string; version: string; replace: boolean; checkpointId?: string }): void;
  onUseImageForSpawn(imageId: string): void;
  onUseImageForExec(imageId: string): void;
  onRate(pid: string, score: number, comment: string): Promise<boolean>;
  onInspectImage(imageId: string): Promise<ImageInspectResult>;
  onListOperations(pid: string, cursor?: string): Promise<OperationListResponse>;
  onExplainOperation(operationId: string, cursor?: string): Promise<ExplainOperationResponse>;
  onResolveOperation(kind: string, id: string): Promise<ExplainOperationResponse>;
  explainLookup: { kind: string; id: string; nonce: number } | null;
}) {
  const { t } = useI18n();
  const [tab, setTab] = useState<TabKey>("overview");
  useEffect(() => {
    if (explainLookup) setTab("explain");
  }, [explainLookup?.nonce]);
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
      <div className="tabPanel">{renderTab(tab, process, snapshot, t, {
        onImportImage,
        onCommitImage,
        onUseImageForSpawn,
        onUseImageForExec,
        onRate,
        onInspectImage,
        onListOperations,
        onExplainOperation,
        onResolveOperation,
        explainLookup
      })}</div>
    </aside>
  );
}

function renderTab(
  tab: TabKey,
  process: RuntimeProcess | null,
  snapshot: RuntimeSnapshot,
  t: (key: TranslationKey) => string,
  imageActions: {
    onImportImage(replace: boolean): void;
    onCommitImage(request: { imageId: string; name: string; version: string; replace: boolean; checkpointId?: string }): void;
    onUseImageForSpawn(imageId: string): void;
    onUseImageForExec(imageId: string): void;
    onRate(pid: string, score: number, comment: string): Promise<boolean>;
    onInspectImage(imageId: string): Promise<ImageInspectResult>;
    onListOperations(pid: string, cursor?: string): Promise<OperationListResponse>;
    onExplainOperation(operationId: string, cursor?: string): Promise<ExplainOperationResponse>;
    onResolveOperation(kind: string, id: string): Promise<ExplainOperationResponse>;
    explainLookup: { kind: string; id: string; nonce: number } | null;
  }
) {
  if (!process && !["jsonRpc", "mcp", "toolsSkills", "images"].includes(tab)) return <div className="empty">{t("details.selectProcess")}</div>;
  if (tab === "overview") return <JsonBlock value={process} />;
  if (tab === "rating") return <RatingPanel process={process} onSave={imageActions.onRate} />;
  if (tab === "capabilities") return <JsonBlock value={{ capability_ids: process?.capabilities }} />;
  if (tab === "toolsSkills") return <JsonBlock value={{ process_tools: process?.tool_table, loaded_skills: process?.loaded_skills, registry: snapshot.skills, tools: snapshot.tools }} />;
  if (tab === "checkpoints") return <JsonBlock value={{ checkpoint_head: process?.checkpoint_head }} />;
  if (tab === "audit") return <JsonBlock value={snapshot.audit.filter((item) => item.actor === process?.pid || item.target === `process:${process?.pid}`)} />;
  if (tab === "explain" && process) {
    return (
      <ExplainPanel
        key={explainRefreshKey(process, snapshot)}
        pid={process.pid}
        listOperations={imageActions.onListOperations}
        explainOperation={imageActions.onExplainOperation}
        resolveOperation={imageActions.onResolveOperation}
        lookup={imageActions.explainLookup}
      />
    );
  }
  if (tab === "llmCalls") return <JsonBlock value={snapshot.llm_calls.filter((item) => item.pid === process?.pid)} />;
  if (tab === "jsonRpc") return <JsonBlock value={snapshot.jsonrpc_endpoints} />;
  if (tab === "mcp") return <JsonBlock value={snapshot.mcp_servers} />;
  if (tab === "images") {
    return (
      <ImagePanel
        images={snapshot.images}
        selectedProcess={process}
        allowReplace
        onImportImage={imageActions.onImportImage}
        onCommitImage={imageActions.onCommitImage}
        onUseForSpawn={imageActions.onUseImageForSpawn}
        onUseForExec={imageActions.onUseImageForExec}
        onInspectImage={imageActions.onInspectImage}
      />
    );
  }
  return <JsonBlock value={{ goal_oid: process?.goal_oid, note: t("details.objectMemoryNote") }} />;
}

export function explainRefreshKey(process: RuntimeProcess, snapshot: RuntimeSnapshot): string {
  return [
    process.pid,
    snapshot.audit.at(-1)?.record_id,
    snapshot.events.at(-1)?.event_id,
    snapshot.llm_calls.at(-1)?.call_id,
    snapshot.human_requests.at(-1)?.updated_at
  ].join(":");
}

function JsonBlock({ value }: { value: unknown }) {
  const { t } = useI18n();
  return <CollapsibleJson value={value} label={t("details.rawData")} />;
}
