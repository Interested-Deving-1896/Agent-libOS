import { Circle, Pause, Play, Square } from "lucide-react";
import type { RuntimeProcess } from "../api/types";
import { useI18n } from "../i18n";

type ProcessTreeProps = {
  processes: RuntimeProcess[];
  selectedPid: string | null;
  onSelect(pid: string): void;
};

export function ProcessTree({ processes, selectedPid, onSelect }: ProcessTreeProps) {
  const { t } = useI18n();
  const { roots, children } = indexProcessTree(processes);

  return (
    <nav className="processTree" aria-label={t("processTree.label")}>
      {roots.map((process) => (
        <ProcessNode
          key={process.pid}
          process={process}
          selectedPid={selectedPid}
          childrenByPid={children}
          onSelect={onSelect}
          depth={0}
        />
      ))}
      {processes.length === 0 ? <div className="empty">{t("processTree.empty")}</div> : null}
    </nav>
  );
}

export function indexProcessTree(processes: RuntimeProcess[]) {
  const roots: RuntimeProcess[] = [];
  const children = new Map<string, RuntimeProcess[]>();
  const visiblePids = new Set(processes.map((process) => process.pid));
  for (const process of processes) {
    // A source-bounded snapshot can contain a high-priority child while its
    // lower-priority ancestor falls outside the visible window. Keep that
    // child reachable instead of attaching it to an absent node.
    if (!process.parent_pid || !visiblePids.has(process.parent_pid)) {
      roots.push(process);
      continue;
    }
    const siblings = children.get(process.parent_pid);
    if (siblings) {
      siblings.push(process);
    } else {
      children.set(process.parent_pid, [process]);
    }
  }
  return { roots, children };
}

function ProcessNode({
  process,
  selectedPid,
  childrenByPid,
  onSelect,
  depth
}: {
  process: RuntimeProcess;
  selectedPid: string | null;
  childrenByPid: Map<string, RuntimeProcess[]>;
  onSelect(pid: string): void;
  depth: number;
}) {
  const icon = iconForStatus(process.status);
  return (
    <div>
      <button
        className={`processNode ${selectedPid === process.pid ? "selected" : ""}`}
        style={{ paddingLeft: 12 + depth * 14 }}
        onClick={() => onSelect(process.pid)}
      >
        {icon}
        <span className="processMain">
          <span className="pid">{process.pid}</span>
          <span className="subtle">{process.image_id}</span>
        </span>
        {process.interrupt_count > 0 ? <span className="badge urgent">{process.interrupt_count}</span> : null}
        {process.unread_message_count > 0 ? <span className="badge">{process.unread_message_count}</span> : null}
      </button>
      {(childrenByPid.get(process.pid) ?? []).map((child) => (
        <ProcessNode
          key={child.pid}
          process={child}
          selectedPid={selectedPid}
          childrenByPid={childrenByPid}
          onSelect={onSelect}
          depth={depth + 1}
        />
      ))}
    </div>
  );
}

function iconForStatus(status: string) {
  if (status === "runnable" || status === "running") return <Play size={14} />;
  if (status.startsWith("waiting")) return <Pause size={14} />;
  if (["exited", "failed", "killed"].includes(status)) return <Square size={14} />;
  return <Circle size={14} />;
}
