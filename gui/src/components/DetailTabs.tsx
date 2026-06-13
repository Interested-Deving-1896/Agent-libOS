import { useState } from "react";
import type { RuntimeProcess, RuntimeSnapshot } from "../api/types";

const tabs = ["Overview", "Capabilities", "Tools/Skills", "Checkpoints", "Audit", "LLM Calls", "JSON-RPC", "Object Memory"] as const;

export function DetailTabs({ process, snapshot }: { process: RuntimeProcess | null; snapshot: RuntimeSnapshot | null }) {
  const [tab, setTab] = useState<(typeof tabs)[number]>("Overview");
  if (!snapshot) return <div className="empty">Runtime snapshot is not loaded.</div>;
  return (
    <aside className="details">
      <div className="tabs" role="tablist">
        {tabs.map((name) => (
          <button key={name} className={tab === name ? "active" : ""} onClick={() => setTab(name)}>
            {name}
          </button>
        ))}
      </div>
      <div className="tabPanel">{renderTab(tab, process, snapshot)}</div>
    </aside>
  );
}

function renderTab(tab: (typeof tabs)[number], process: RuntimeProcess | null, snapshot: RuntimeSnapshot) {
  if (!process && tab !== "JSON-RPC" && tab !== "Tools/Skills") return <div className="empty">Select a process.</div>;
  if (tab === "Overview") return <JsonBlock value={process} />;
  if (tab === "Capabilities") return <JsonBlock value={{ capability_ids: process?.capabilities }} />;
  if (tab === "Tools/Skills") return <JsonBlock value={{ process_tools: process?.tool_table, loaded_skills: process?.loaded_skills, registry: snapshot.skills, tools: snapshot.tools }} />;
  if (tab === "Checkpoints") return <JsonBlock value={{ checkpoint_head: process?.checkpoint_head }} />;
  if (tab === "Audit") return <JsonBlock value={snapshot.audit.filter((item) => item.actor === process?.pid || item.target === `process:${process?.pid}`)} />;
  if (tab === "LLM Calls") return <JsonBlock value={snapshot.llm_calls.filter((item) => item.pid === process?.pid)} />;
  if (tab === "JSON-RPC") return <JsonBlock value={snapshot.jsonrpc_endpoints} />;
  return <JsonBlock value={{ goal_oid: process?.goal_oid, note: "Object payload materialization remains capability-controlled in the runtime." }} />;
}

function JsonBlock({ value }: { value: unknown }) {
  return <pre className="jsonBlock">{JSON.stringify(value, null, 2)}</pre>;
}
