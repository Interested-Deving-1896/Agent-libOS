import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, Send } from "lucide-react";
import { LibOSClient } from "./api/client";
import type { GuiConnection, HumanRequest, RuntimeSnapshot } from "./api/types";
import { ConfirmDialog } from "./components/ConfirmDialog";
import { DetailTabs } from "./components/DetailTabs";
import { ImageSelect } from "./components/ImageSelect";
import { LLMProfileSelect } from "./components/LLMProfileSelect";
import { ProcessTree } from "./components/ProcessTree";
import { Timeline } from "./components/Timeline";
import { TopBar } from "./components/TopBar";
import { UserPage } from "./components/UserPage";
import { previewImageManifest } from "./imagePreview";
import { useI18n } from "./i18n";
import type { OptionalQuanta } from "./quanta";
import { reconcileSelectedPid } from "./selection";
import type { LLMProfileInput } from "./api/types";

type PendingConfirm = {
  title: string;
  message: string;
  details: Record<string, unknown>;
  action(): Promise<void>;
};

export function App() {
  const { t } = useI18n();
  const [view, setView] = useState<"user" | "operator">("user");
  const [connection, setConnection] = useState<GuiConnection | null>(null);
  const [client, setClient] = useState<LibOSClient | null>(null);
  const [snapshot, setSnapshot] = useState<RuntimeSnapshot | null>(null);
  const [selectedPid, setSelectedPid] = useState<string | null>(null);
  const [maxQuanta, setMaxQuanta] = useState<OptionalQuanta>(null);
  const [spawnGoal, setSpawnGoal] = useState("Summarize the current project state.");
  const [spawnImage, setSpawnImage] = useState("coding-agent:v0");
  const [spawnLlmProfile, setSpawnLlmProfile] = useState("");
  const [spawnWorkingDirectory, setSpawnWorkingDirectory] = useState("");
  const [message, setMessage] = useState("");
  const [cwd, setCwd] = useState("");
  const [execImage, setExecImage] = useState("base-agent:v0");
  const [execLlmProfile, setExecLlmProfile] = useState("");
  const [execGoal, setExecGoal] = useState("");
  const [pendingConfirm, setPendingConfirm] = useState<PendingConfirm | null>(null);
  const [confirmBusy, setConfirmBusy] = useState(false);
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
          setSelectedPid((current) => reconcileSelectedPid(next, current));
        }
      }
      if (message.event === "snapshot_truncated" || message.event === "event.invalidated") {
        void refresh();
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
      if (!conn) throw new Error(t("app.preloadMissing"));
      const nextClient = new LibOSClient(conn);
      const nextSnapshot = await nextClient.snapshot();
      setConnection(conn);
      setClient(nextClient);
      setSnapshot(nextSnapshot);
      setSelectedPid(reconcileSelectedPid(nextSnapshot, null));
      setMaxQuanta(nextSnapshot.scheduler.default_max_quanta ?? null);
    } catch (reason) {
      setError(describeError(reason, t("app.confirmationRequiredSuffix")));
    }
  }

  async function refresh(): Promise<boolean> {
    if (!client) return false;
    try {
      const next = await client.snapshot();
      setSnapshot(next);
      setSelectedPid((current) => reconcileSelectedPid(next, current));
      return true;
    } catch (reason) {
      setError(describeError(reason, t("app.confirmationRequiredSuffix")));
      return false;
    }
  }

  async function reconnect(next: GuiConnection | null) {
    if (!next) return;
    if (connection && sameConnection(connection, next)) return;
    const nextClient = new LibOSClient(next);
    const nextSnapshot = await nextClient.snapshot();
    setConnection(next);
    setClient(nextClient);
    setSnapshot(nextSnapshot);
    setSelectedPid(reconcileSelectedPid(nextSnapshot, null, { preserveExisting: false }));
    setMaxQuanta(nextSnapshot.scheduler.default_max_quanta ?? null);
  }

  async function openDatabase() {
    try {
      setError(null);
      const next = await window.libosApi?.chooseDatabase();
      await reconnect(next ?? null);
    } catch (reason) {
      setError(describeError(reason, t("app.confirmationRequiredSuffix")));
    }
  }

  async function safe(action: () => Promise<void>): Promise<boolean> {
    try {
      setError(null);
      await action();
      return refresh();
    } catch (reason) {
      setError(describeError(reason, t("app.confirmationRequiredSuffix")));
      return false;
    }
  }

  async function spawnProcess() {
    if (!client) return;
    await safe(async () => {
      const result = await client.spawn(spawnGoal, spawnImage, maxQuanta, Boolean(snapshot?.scheduler.auto_run), {
        workingDirectory: spawnWorkingDirectory,
        llmProfile: spawnLlmProfile || undefined
      });
      const pid = (result as { pid?: string }).pid;
      if (pid) setSelectedPid(pid);
    });
  }

  async function send(kind: "message" | "interrupt"): Promise<boolean> {
    if (!client || !selectedProcess || !message.trim()) return false;
    const pid = selectedProcess.pid;
    return safe(async () => {
      await client.sendMessage(pid, message.trim(), kind, Boolean(snapshot?.scheduler.auto_run), maxQuanta);
      setMessage("");
    });
  }

  async function respond(request: HumanRequest, approved: boolean, answer = ""): Promise<boolean> {
    if (!client) return false;
    return safe(async () => {
      await client.respondHumanRequest(request.request_id, approved, answer, Boolean(snapshot?.scheduler.auto_run), maxQuanta);
    });
  }

  async function rateProcess(pid: string, score: number, comment: string): Promise<boolean> {
    if (!client) return false;
    return safe(async () => {
      await client.submitAgentRating(pid, score, comment);
    });
  }

  async function createLlmProfile(profile: LLMProfileInput): Promise<boolean> {
    if (!client) return false;
    return safe(async () => {
      await client.createLLMProfile(profile);
    });
  }

  async function updateLlmProfile(profileId: string, profile: LLMProfileInput): Promise<boolean> {
    if (!client) return false;
    return safe(async () => {
      await client.updateLLMProfile(profileId, profile);
    });
  }

  async function deleteLlmProfile(profileId: string): Promise<boolean> {
    if (!client) return false;
    return safe(async () => {
      await client.deleteLLMProfile(profileId);
      if (spawnLlmProfile === profileId) setSpawnLlmProfile("");
      if (execLlmProfile === profileId) setExecLlmProfile("");
    });
  }

  function confirmExec() {
    if (!client || !selectedProcess) return;
    const pid = selectedProcess.pid;
    setPendingConfirm({
      title: t("app.exec.title"),
      message: t("app.exec.message"),
      details: { pid, image: execImage, goal: execGoal, llm_profile: execLlmProfile || null, auto_run: snapshot?.scheduler.auto_run, max_quanta: maxQuanta },
      action: async () => {
        await client.execProcess(pid, execImage, execGoal, true, Boolean(snapshot?.scheduler.auto_run), maxQuanta, execLlmProfile || undefined);
        setPendingConfirm(null);
        await refresh();
      }
    });
  }

  function confirmExit() {
    if (!client || !selectedProcess) return;
    const pid = selectedProcess.pid;
    setPendingConfirm({
      title: t("app.exit.title"),
      message: t("app.exit.message"),
      details: { pid },
      action: async () => {
        await client.exitProcess(pid, "Exited from GUI", false, true);
        setPendingConfirm(null);
        await refresh();
      }
    });
  }

  async function chooseAndConfirmImageImport(replace = false) {
    if (!client) return;
    try {
      const imagePackage = await window.libosApi?.chooseImagePackage();
      if (!imagePackage) return;
      const preview = previewImageManifest(imagePackage.manifest);
      setPendingConfirm({
        title: t("image.register.title"),
        message: t("image.register.message"),
        details: {
          source: imagePackage.name,
          image_id: preview.image_id,
          name: preview.name,
          version: preview.version,
          default_tools_count: preview.default_tools_count,
          required_capabilities_count: preview.required_capabilities_count,
          required_modules_count: preview.required_modules_count,
          files: Object.keys(imagePackage.files).length,
          bytes: JSON.stringify(imagePackage.files).length,
          replace
        },
        action: async () => {
          const result = await client.registerImagePackage(imagePackage, true, replace);
          setSpawnImage(result.image_id);
          setExecImage(result.image_id);
          setPendingConfirm(null);
          await refresh();
        }
      });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  function confirmCommitImage(request: { imageId: string; name: string; version: string; replace: boolean; checkpointId?: string }) {
    if (!client || !selectedProcess) return;
    const pid = selectedProcess.pid;
    setPendingConfirm({
      title: t("image.commit.title"),
      message: t("image.commit.message"),
      details: {
        pid,
        checkpoint: request.checkpointId ?? t("image.autoCheckpoint"),
        image_id: request.imageId,
        name: request.name,
        version: request.version,
        replace: request.replace
      },
      action: async () => {
        const checkpointId = request.checkpointId
          ?? (await client.createCheckpoint(pid, "GUI image commit")).checkpoint_id;
        const result = await client.commitCheckpointToImage({
          checkpointId,
          imageId: request.imageId,
          name: request.name,
          version: request.version,
          confirmed: true,
          replace: request.replace
        });
        setSpawnImage(result.image_id);
        setExecImage(result.image_id);
        setPendingConfirm(null);
        await refresh();
      }
    });
  }

  async function confirmPendingAction() {
    if (!pendingConfirm || confirmBusy) return;
    setConfirmBusy(true);
    setError(null);
    try {
      await pendingConfirm.action();
    } catch (reason) {
      setError(describeError(reason, t("app.confirmationRequiredSuffix")));
    } finally {
      setConfirmBusy(false);
    }
  }

  return (
    <div className={view === "user" ? "userAppShell" : "appShell"}>
      {view === "user" ? (
        <UserPage
          connection={connection}
          snapshot={snapshot}
          selectedPid={selectedPid}
          selectedProcess={selectedProcess}
          maxQuanta={maxQuanta}
          spawnGoal={spawnGoal}
          spawnImage={spawnImage}
          spawnLlmProfile={spawnLlmProfile}
          spawnWorkingDirectory={spawnWorkingDirectory}
          message={message}
          images={snapshot?.images ?? []}
          llmProfiles={snapshot?.llm_profiles ?? []}
          onSelectPid={setSelectedPid}
          onMaxQuantaChange={setMaxQuanta}
          onSpawnGoalChange={setSpawnGoal}
          onSpawnImageChange={setSpawnImage}
          onSpawnLlmProfileChange={setSpawnLlmProfile}
          onSpawnWorkingDirectoryChange={setSpawnWorkingDirectory}
          onMessageChange={setMessage}
          onSpawn={() => void spawnProcess()}
          onImportImage={() => void chooseAndConfirmImageImport(false)}
          onCommitImage={confirmCommitImage}
          onSend={(kind) => void send(kind)}
          onRespond={(request, approved, answer = "") => respond(request, approved, answer)}
          onRate={rateProcess}
          onCreateLlmProfile={createLlmProfile}
          onUpdateLlmProfile={updateLlmProfile}
          onDeleteLlmProfile={deleteLlmProfile}
          onRun={() => selectedProcess && client && void safe(() => client.run(selectedProcess.pid, maxQuanta).then(() => undefined))}
          onPause={() => client && void safe(() => client.pauseScheduler().then(() => undefined))}
          onRefresh={() => void refresh()}
          onOpenDb={() => void openDatabase()}
          onShowOperator={() => setView("operator")}
          onStop={confirmExit}
        />
      ) : (
        <>
          <TopBar
            db={connection?.db ?? t("app.defaultDb")}
            scheduler={snapshot?.scheduler ?? null}
            maxQuanta={maxQuanta}
            selectedPid={selectedProcess?.pid ?? null}
            onMaxQuantaChange={setMaxQuanta}
            onOpenDb={() => void openDatabase()}
            onSpawn={() => void spawnProcess()}
            onRun={() => selectedProcess && client && void safe(() => client.run(selectedProcess.pid, maxQuanta).then(() => undefined))}
            onStep={() => selectedProcess && client && void safe(() => client.step(selectedProcess.pid).then(() => undefined))}
            onPause={() => client && void safe(() => client.pauseScheduler().then(() => undefined))}
            onAutoRunChange={(value) => client && void safe(() => client.setAutoRun(value).then(() => undefined))}
            onRefresh={() => void refresh()}
            onShowUser={() => setView("user")}
          />

          <main className="workspace">
            <section className="leftPane">
              <div className="paneHeader">
                <h1>{t("operator.processes.title")}</h1>
                <span>{snapshot?.processes.length ?? 0}</span>
              </div>
              <div className="spawnBox">
                <ImageSelect images={snapshot?.images ?? []} value={spawnImage} label={t("operator.spawnImage")} onChange={setSpawnImage} />
                <LLMProfileSelect
                  profiles={snapshot?.llm_profiles ?? []}
                  value={spawnLlmProfile}
                  label={t("llmProfile.spawnLabel")}
                  onChange={setSpawnLlmProfile}
                  onCreate={createLlmProfile}
                  onUpdate={updateLlmProfile}
                  onDelete={deleteLlmProfile}
                />
                <input
                  value={spawnWorkingDirectory}
                  onChange={(event) => setSpawnWorkingDirectory(event.currentTarget.value)}
                  placeholder={t("operator.initialCwdPlaceholder")}
                  aria-label={t("operator.initialCwd")}
                />
                <textarea value={spawnGoal} onChange={(event) => setSpawnGoal(event.currentTarget.value)} aria-label={t("operator.spawnGoal")} />
              </div>
              <ProcessTree processes={snapshot?.processes ?? []} selectedPid={selectedPid} onSelect={setSelectedPid} />
            </section>

            <section className="centerPane">
              <div className="paneHeader">
                <div>
                  <h1>{selectedProcess?.pid ?? t("operator.noProcessSelected")}</h1>
                  {selectedProcess ? <span>{selectedProcess.image_id} · {selectedProcess.status} · {selectedProcess.llm_profile_id} · {t("operator.cwd")} {selectedProcess.working_directory}</span> : null}
                </div>
                {selectedProcess?.interrupt_count ? <span className="interruptBanner"><AlertTriangle size={16} /> {t("operator.interruptPending")}</span> : null}
              </div>

              <div className="humanRequests">
                {(snapshot?.human_requests ?? []).filter((request) => request.status === "pending").map((request) => (
                  <div className="humanCard" key={request.request_id}>
                    <strong>{String(request.payload?.question ?? request.payload?.type ?? t("operator.humanRequestFallback"))}</strong>
                    <input placeholder={t("operator.answerPlaceholder")} onKeyDown={(event) => {
                      if (event.key === "Enter") void respond(request, true, event.currentTarget.value);
                    }} />
                    <button onClick={() => void respond(request, true)}>{t("operator.approve")}</button>
                    <button className="danger" onClick={() => void respond(request, false)}>{t("operator.reject")}</button>
                  </div>
                ))}
              </div>

              <Timeline
                pid={selectedProcess?.pid ?? null}
                messages={selectedProcess?.messages ?? []}
                humanRequests={snapshot?.human_requests ?? []}
                llmCalls={snapshot?.llm_calls ?? []}
                events={snapshot?.events ?? []}
                audit={snapshot?.audit ?? []}
              />

              <div className="composer">
                <input value={message} onChange={(event) => setMessage(event.currentTarget.value)} placeholder={t("operator.messagePlaceholder")} />
                <button disabled={!selectedProcess || !message.trim()} onClick={() => void send("message")}><Send size={16} />{t("operator.message")}</button>
                <button disabled={!selectedProcess || !message.trim()} className="warning" onClick={() => void send("interrupt")}>{t("operator.interrupt")}</button>
              </div>
            </section>

            <section className="rightPane">
              <div className="quickActions">
                <input value={cwd} placeholder={t("operator.newCwdPlaceholder")} onChange={(event) => setCwd(event.currentTarget.value)} />
                <button disabled={!client || !selectedProcess || !cwd.trim()} onClick={() => selectedProcess && void safe(() => client!.changeDirectory(selectedProcess.pid, cwd).then(() => undefined))}>cd</button>
                <ImageSelect images={snapshot?.images ?? []} value={execImage} label={t("operator.exec")} onChange={setExecImage} />
                <LLMProfileSelect
                  profiles={snapshot?.llm_profiles ?? []}
                  value={execLlmProfile}
                  label={t("llmProfile.execLabel")}
                  disabled={!selectedProcess}
                  onChange={setExecLlmProfile}
                  onCreate={createLlmProfile}
                  onUpdate={updateLlmProfile}
                  onDelete={deleteLlmProfile}
                />
                <input value={execGoal} onChange={(event) => setExecGoal(event.currentTarget.value)} aria-label={t("operator.spawnGoal")} />
                <button disabled={!selectedProcess} className="warning" onClick={confirmExec}>{t("operator.exec")}</button>
                <button disabled={!selectedProcess} className="danger" onClick={confirmExit}>{t("operator.exit")}</button>
              </div>
              <DetailTabs
                process={selectedProcess}
                snapshot={snapshot}
                onImportImage={(replace) => void chooseAndConfirmImageImport(replace)}
                onCommitImage={confirmCommitImage}
                onUseImageForSpawn={setSpawnImage}
                onUseImageForExec={setExecImage}
                onRate={rateProcess}
                onInspectImage={(imageId) => {
                  if (!client) throw new Error(t("app.clientUnavailable"));
                  return client.inspectImage(imageId);
                }}
              />
            </section>
          </main>
        </>
      )}

      {error ? <div className="toast" role="alert">{error}</div> : null}
      {pendingConfirm ? (
        <ConfirmDialog
          title={pendingConfirm.title}
          message={pendingConfirm.message}
          details={pendingConfirm.details}
          busy={confirmBusy}
          onCancel={() => setPendingConfirm(null)}
          onConfirm={() => void confirmPendingAction()}
        />
      ) : null}
    </div>
  );
}

function sameConnection(left: GuiConnection, right: GuiConnection): boolean {
  return left.url === right.url && left.token === right.token && left.db === right.db;
}

function describeError(reason: unknown, confirmationSuffix: string): string {
  const err = reason as Error & { payload?: { error?: { confirmation_required?: boolean } } };
  const message = err.message ?? String(reason);
  if (err.payload?.error?.confirmation_required) {
    return `${message}. ${confirmationSuffix}`;
  }
  return message;
}
