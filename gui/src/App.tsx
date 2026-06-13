import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, Send } from "lucide-react";
import { LibOSClient } from "./api/client";
import type { GuiConnection, HumanRequest, RuntimeProcess, RuntimeSnapshot } from "./api/types";
import { ConfirmDialog } from "./components/ConfirmDialog";
import { DetailTabs } from "./components/DetailTabs";
import { ProcessTree } from "./components/ProcessTree";
import { Timeline } from "./components/Timeline";
import { TopBar } from "./components/TopBar";

type PendingConfirm = {
  title: string;
  message: string;
  details: Record<string, unknown>;
  action(): Promise<void>;
};

export function App() {
  const [connection, setConnection] = useState<GuiConnection | null>(null);
  const [client, setClient] = useState<LibOSClient | null>(null);
  const [snapshot, setSnapshot] = useState<RuntimeSnapshot | null>(null);
  const [selectedPid, setSelectedPid] = useState<string | null>(null);
  const [maxQuanta, setMaxQuanta] = useState(25);
  const [spawnGoal, setSpawnGoal] = useState("Summarize the current project state.");
  const [spawnImage, setSpawnImage] = useState("coding-agent:v0");
  const [message, setMessage] = useState("");
  const [cwd, setCwd] = useState("");
  const [execImage, setExecImage] = useState("base-agent:v0");
  const [execGoal, setExecGoal] = useState("");
  const [pendingConfirm, setPendingConfirm] = useState<PendingConfirm | null>(null);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    void initialize();
    return () => abortRef.current?.abort();
  }, []);

  useEffect(() => {
    if (!client) return;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    void client.stream((message) => {
      if (message.event === "snapshot") {
        const next = (message.data as { snapshot?: RuntimeSnapshot }).snapshot;
        if (next) {
          setSnapshot(next);
          setSelectedPid((current) => current ?? next.processes[0]?.pid ?? null);
        }
      }
    }, controller.signal).catch((reason) => {
      if (!controller.signal.aborted) setError(String(reason));
    });
    return () => controller.abort();
  }, [client]);

  const selectedProcess = useMemo(
    () => snapshot?.processes.find((process) => process.pid === selectedPid) ?? null,
    [snapshot, selectedPid]
  );

  async function initialize() {
    try {
      const conn = await window.libosApi?.getConnection();
      if (!conn) throw new Error("Electron preload did not provide a GUI connection.");
      const nextClient = new LibOSClient(conn);
      setConnection(conn);
      setClient(nextClient);
      const nextSnapshot = await nextClient.snapshot();
      setSnapshot(nextSnapshot);
      setSelectedPid(nextSnapshot.processes[0]?.pid ?? null);
      setMaxQuanta(nextSnapshot.scheduler.default_max_quanta);
    } catch (reason) {
      setError(String(reason));
    }
  }

  async function refresh() {
    if (!client) return;
    const next = await client.snapshot();
    setSnapshot(next);
    setSelectedPid((current) => current ?? next.processes[0]?.pid ?? null);
  }

  async function reconnect(next: GuiConnection | null) {
    if (!next) return;
    const nextClient = new LibOSClient(next);
    setConnection(next);
    setClient(nextClient);
    setSnapshot(await nextClient.snapshot());
  }

  async function safe(action: () => Promise<void>) {
    try {
      setError(null);
      await action();
      await refresh();
    } catch (reason) {
      const err = reason as Error & { payload?: { error?: { confirmation_required?: boolean; preview?: Record<string, unknown>; action?: string } } };
      if (err.payload?.error?.confirmation_required) {
        setError(`${err.message}. Re-run this operation from its explicit confirmation control.`);
      } else {
        setError(err.message ?? String(reason));
      }
    }
  }

  async function spawnProcess() {
    if (!client) return;
    await safe(async () => {
      const result = await client.spawn(spawnGoal, spawnImage, maxQuanta, Boolean(snapshot?.scheduler.auto_run));
      const pid = (result as { pid?: string }).pid;
      if (pid) setSelectedPid(pid);
    });
  }

  async function send(kind: "message" | "interrupt") {
    if (!client || !selectedPid || !message.trim()) return;
    await safe(async () => {
      await client.sendMessage(selectedPid, message.trim(), kind, Boolean(snapshot?.scheduler.auto_run), maxQuanta);
      setMessage("");
    });
  }

  async function respond(request: HumanRequest, approved: boolean, answer = "") {
    if (!client) return;
    await safe(async () => {
      await client.respondHumanRequest(request.request_id, approved, answer);
    });
  }

  function confirmExec() {
    if (!client || !selectedPid) return;
    setPendingConfirm({
      title: "Exec process",
      message: "This replaces the selected process image and goal. It does not grant target-image capabilities.",
      details: { pid: selectedPid, image: execImage, goal: execGoal, auto_run: snapshot?.scheduler.auto_run },
      action: async () => {
        await client.execProcess(selectedPid, execImage, execGoal, true, Boolean(snapshot?.scheduler.auto_run));
        setPendingConfirm(null);
        await refresh();
      }
    });
  }

  function confirmExit() {
    if (!client || !selectedPid) return;
    setPendingConfirm({
      title: "Exit process",
      message: "This marks the selected process as exited.",
      details: { pid: selectedPid },
      action: async () => {
        await client.exitProcess(selectedPid, "Exited from GUI", false, true);
        setPendingConfirm(null);
        await refresh();
      }
    });
  }

  return (
    <div className="appShell">
      <TopBar
        db={connection?.db ?? "local"}
        scheduler={snapshot?.scheduler ?? null}
        maxQuanta={maxQuanta}
        selectedPid={selectedPid}
        onMaxQuantaChange={setMaxQuanta}
        onOpenDb={() => void window.libosApi?.chooseDatabase().then(reconnect)}
        onUseDb={(db) => void window.libosApi?.useDatabase(db).then(reconnect)}
        onSpawn={spawnProcess}
        onRun={() => selectedPid && client && void safe(() => client.run(selectedPid, maxQuanta).then(() => undefined))}
        onStep={() => selectedPid && client && void safe(() => client.step(selectedPid).then(() => undefined))}
        onPause={() => client && void safe(() => client.pauseScheduler().then(() => undefined))}
        onAutoRunChange={(value) => client && void safe(() => client.setAutoRun(value).then(() => undefined))}
        onRefresh={() => void refresh()}
      />

      <main className="workspace">
        <section className="leftPane">
          <div className="paneHeader">
            <h1>Processes</h1>
            <span>{snapshot?.processes.length ?? 0}</span>
          </div>
          <div className="spawnBox">
            <input value={spawnImage} onChange={(event) => setSpawnImage(event.currentTarget.value)} aria-label="Spawn image" />
            <textarea value={spawnGoal} onChange={(event) => setSpawnGoal(event.currentTarget.value)} aria-label="Spawn goal" />
          </div>
          <ProcessTree processes={snapshot?.processes ?? []} selectedPid={selectedPid} onSelect={setSelectedPid} />
        </section>

        <section className="centerPane">
          <div className="paneHeader">
            <div>
              <h1>{selectedProcess?.pid ?? "No process selected"}</h1>
              {selectedProcess ? <span>{selectedProcess.image_id} · {selectedProcess.status} · cwd {selectedProcess.working_directory}</span> : null}
            </div>
            {selectedProcess?.interrupt_count ? <span className="interruptBanner"><AlertTriangle size={16} /> Interrupt pending</span> : null}
          </div>

          <div className="humanRequests">
            {(snapshot?.human_requests ?? []).filter((request) => request.status === "pending").map((request) => (
              <div className="humanCard" key={request.request_id}>
                <strong>{String(request.payload?.question ?? request.payload?.type ?? "Human request")}</strong>
                <input placeholder="Answer or approval note" onKeyDown={(event) => {
                  if (event.key === "Enter") void respond(request, true, event.currentTarget.value);
                }} />
                <button onClick={() => void respond(request, true)}>Approve</button>
                <button className="danger" onClick={() => void respond(request, false)}>Reject</button>
              </div>
            ))}
          </div>

          <Timeline
            pid={selectedPid}
            messages={selectedProcess?.messages ?? []}
            humanRequests={snapshot?.human_requests ?? []}
            llmCalls={snapshot?.llm_calls ?? []}
            events={snapshot?.events ?? []}
            audit={snapshot?.audit ?? []}
          />

          <div className="composer">
            <input value={message} onChange={(event) => setMessage(event.currentTarget.value)} placeholder="Send a message to the selected process" />
            <button disabled={!selectedPid || !message.trim()} onClick={() => void send("message")}><Send size={16} />Message</button>
            <button disabled={!selectedPid || !message.trim()} className="warning" onClick={() => void send("interrupt")}>Interrupt</button>
          </div>
        </section>

        <section className="rightPane">
          <div className="quickActions">
            <input value={cwd} placeholder="New cwd" onChange={(event) => setCwd(event.currentTarget.value)} />
            <button disabled={!client || !selectedPid || !cwd.trim()} onClick={() => selectedPid && void safe(() => client!.changeDirectory(selectedPid, cwd).then(() => undefined))}>cd</button>
            <input value={execImage} onChange={(event) => setExecImage(event.currentTarget.value)} aria-label="Exec image" />
            <input value={execGoal} onChange={(event) => setExecGoal(event.currentTarget.value)} aria-label="Exec goal" />
            <button disabled={!selectedPid} className="warning" onClick={confirmExec}>Exec</button>
            <button disabled={!selectedPid} className="danger" onClick={confirmExit}>Exit</button>
          </div>
          <DetailTabs process={selectedProcess} snapshot={snapshot} />
        </section>
      </main>

      {error ? <div className="toast" role="alert">{error}</div> : null}
      {pendingConfirm ? (
        <ConfirmDialog
          title={pendingConfirm.title}
          message={pendingConfirm.message}
          details={pendingConfirm.details}
          onCancel={() => setPendingConfirm(null)}
          onConfirm={() => void pendingConfirm.action()}
        />
      ) : null}
    </div>
  );
}
