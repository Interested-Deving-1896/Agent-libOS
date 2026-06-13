import { useEffect, useState } from "react";
import { Database, Pause, Play, RefreshCw, Square, StepForward } from "lucide-react";
import type { SchedulerStatus } from "../api/types";

export function TopBar({
  db,
  scheduler,
  maxQuanta,
  selectedPid,
  onMaxQuantaChange,
  onOpenDb,
  onUseDb,
  onSpawn,
  onRun,
  onStep,
  onPause,
  onAutoRunChange,
  onRefresh
}: {
  db: string;
  scheduler: SchedulerStatus | null;
  maxQuanta: number;
  selectedPid: string | null;
  onMaxQuantaChange(value: number): void;
  onOpenDb(): void;
  onUseDb(value: string): void;
  onSpawn(): void;
  onRun(): void;
  onStep(): void;
  onPause(): void;
  onAutoRunChange(value: boolean): void;
  onRefresh(): void;
}) {
  const [dbValue, setDbValue] = useState(db);
  useEffect(() => setDbValue(db), [db]);

  return (
    <header className="topBar">
      <div className="dbGroup">
        <Database size={17} />
        <input
          aria-label="Runtime database"
          value={dbValue}
          onChange={(event) => setDbValue(event.currentTarget.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") onUseDb(dbValue);
          }}
        />
        <button title="Open SQLite database" onClick={onOpenDb}>Open</button>
      </div>
      <button className="primary" onClick={onSpawn}>Spawn</button>
      <label className="toggle">
        <input type="checkbox" checked={Boolean(scheduler?.auto_run)} onChange={(event) => onAutoRunChange(event.currentTarget.checked)} />
        Auto Run
      </label>
      <label className="quanta">
        Quanta
        <input type="number" min={1} max={200} value={maxQuanta} onChange={(event) => onMaxQuantaChange(Number(event.currentTarget.value))} />
      </label>
      <button title="Run selected process" disabled={!selectedPid || scheduler?.running} onClick={onRun}><Play size={16} />Run</button>
      <button title="Step selected process" disabled={!selectedPid || scheduler?.running} onClick={onStep}><StepForward size={16} />Step</button>
      <button title="Pause scheduler" onClick={onPause}><Pause size={16} />Pause</button>
      <button title="Refresh snapshot" onClick={onRefresh}><RefreshCw size={16} /></button>
      <span className={`scheduler ${scheduler?.running ? "running" : ""}`}><Square size={11} />{scheduler?.running ? "Running" : scheduler?.paused ? "Paused" : "Idle"}</span>
    </header>
  );
}
