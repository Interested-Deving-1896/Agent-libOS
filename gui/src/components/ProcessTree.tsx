import { Circle, Pause, Play, Square } from "lucide-react";
import type { RuntimeProcess } from "../api/types";

type ProcessTreeProps = {
  processes: RuntimeProcess[];
  selectedPid: string | null;
  onSelect(pid: string): void;
};

export function ProcessTree({ processes, selectedPid, onSelect }: ProcessTreeProps) {
  const roots = processes.filter((process) => !process.parent_pid);
  const children = new Map<string, RuntimeProcess[]>();
  for (const process of processes) {
    if (!process.parent_pid) continue;
    children.set(process.parent_pid, [...(children.get(process.parent_pid) ?? []), process]);
  }

  return (
    <nav className="processTree" aria-label="Process tree">
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
      {processes.length === 0 ? <div className="empty">No processes yet.</div> : null}
    </nav>
  );
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
