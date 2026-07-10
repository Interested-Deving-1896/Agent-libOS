import { AlertTriangle, Bot, Database, MessageSquare, Pause, Play, RefreshCw, Send, Settings, Square } from "lucide-react";
import { useMemo, useState } from "react";
import type { GuiConnection, HumanRequest, HumanResponseInput, ImageSummary, LLMProfileInput, LLMProfileSummary, RuntimeProcess, RuntimeSnapshot } from "../api/types";
import { useI18n } from "../i18n";
import { parseOptionalQuanta } from "../quanta";
import { deriveUserConversation, humanRequestPrompt, type UserConversationItem } from "../userConversation";
import { ImageSelect } from "./ImageSelect";
import { HumanRequestCard } from "./HumanRequestCard";
import { LanguageSwitch } from "./LanguageSwitch";
import { LLMProfileSelect } from "./LLMProfileSelect";
import { MarkdownMessage } from "./MarkdownMessage";
import { RatingPanel } from "./RatingPanel";

type UserPageProps = {
  connection: GuiConnection | null;
  snapshot: RuntimeSnapshot | null;
  selectedPid: string | null;
  selectedProcess: RuntimeProcess | null;
  maxQuanta: number | null;
  spawnGoal: string;
  spawnImage: string;
  spawnLlmProfile: string;
  spawnWorkingDirectory: string;
  message: string;
  images: ImageSummary[];
  llmProfiles: LLMProfileSummary[];
  onSelectPid(pid: string): void;
  onMaxQuantaChange(value: number | null): void;
  onSpawnGoalChange(value: string): void;
  onSpawnImageChange(value: string): void;
  onSpawnLlmProfileChange(value: string): void;
  onSpawnWorkingDirectoryChange(value: string): void;
  onMessageChange(value: string): void;
  onSpawn(): void;
  onImportImage(): void;
  onCommitImage(request: { imageId: string; name: string; version: string; replace: boolean; checkpointId?: string }): void;
  onSend(kind: "message" | "interrupt"): void;
  onRespond(request: HumanRequest, response: HumanResponseInput): Promise<boolean>;
  onRate(pid: string, score: number, comment: string): Promise<boolean>;
  onCreateLlmProfile(profile: LLMProfileInput): Promise<boolean>;
  onUpdateLlmProfile(profileId: string, profile: LLMProfileInput): Promise<boolean>;
  onDeleteLlmProfile(profileId: string): Promise<boolean>;
  onRun(): void;
  onPause(): void;
  onRefresh(): void;
  onOpenDb(): void;
  onShowOperator(): void;
  onStop(): void;
};

export function UserPage({
  connection,
  snapshot,
  selectedPid,
  selectedProcess,
  maxQuanta,
  spawnGoal,
  spawnImage,
  spawnLlmProfile,
  spawnWorkingDirectory,
  message,
  images,
  llmProfiles,
  onSelectPid,
  onMaxQuantaChange,
  onSpawnGoalChange,
  onSpawnImageChange,
  onSpawnLlmProfileChange,
  onSpawnWorkingDirectoryChange,
  onMessageChange,
  onSpawn,
  onImportImage,
  onCommitImage,
  onSend,
  onRespond,
  onRate,
  onCreateLlmProfile,
  onUpdateLlmProfile,
  onDeleteLlmProfile,
  onRun,
  onPause,
  onRefresh,
  onOpenDb,
  onShowOperator,
  onStop
}: UserPageProps) {
  const { t } = useI18n();
  const [commitImageId, setCommitImageId] = useState("");
  const [commitName, setCommitName] = useState("");
  const [commitVersion, setCommitVersion] = useState("v0");
  const conversation = useMemo(() => deriveUserConversation(snapshot, selectedPid), [snapshot, selectedPid]);
  const pendingRequests = conversation.filter((item): item is Extract<UserConversationItem, { role: "request" }> => item.role === "request");
  const isRunning = Boolean(snapshot?.scheduler.running);
  const hasProcess = Boolean(selectedProcess);
  const commitReady = Boolean(hasProcess && commitImageId.trim() && commitName.trim() && commitVersion.trim());

  return (
    <main className="userPage">
      <header className="userTopBar">
        <div className="userBrand">
          <Bot size={18} />
          <div>
            <strong>Agent libOS</strong>
            <span>{connection?.db ?? t("app.defaultDb")}</span>
          </div>
        </div>
        <div className="userTopActions">
          <LanguageSwitch />
          <button title={t("user.openDbTitle")} onClick={onOpenDb}><Database size={15} />{t("user.openDb")}</button>
          <button title={t("user.refreshTitle")} onClick={onRefresh}><RefreshCw size={15} /></button>
          <button className="secondary" onClick={onShowOperator}><Settings size={15} />{t("user.operatorConsole")}</button>
        </div>
      </header>

      <section className="userTaskBar">
        <div className="userTaskMain">
          <label>
            {t("user.process")}
            <select value={selectedPid ?? ""} onChange={(event) => onSelectPid(event.currentTarget.value)}>
              {(snapshot?.processes.length ?? 0) === 0 ? <option value="">{t("user.noProcess")}</option> : null}
              {(snapshot?.processes ?? []).map((process) => (
                <option key={process.pid} value={process.pid}>{process.pid} · {process.status}</option>
              ))}
            </select>
          </label>
          <div className="userStatus">
            <span className={`statusDot ${isRunning ? "running" : ""}`} />
            {isRunning ? t("user.running") : snapshot?.scheduler.paused ? t("user.paused") : t("user.idle")}
          </div>
          {selectedProcess ? (
            <div className="userProcessMeta">
              <span>{selectedProcess.image_id}</span>
              <span>{selectedProcess.llm_profile_id}</span>
              <span>{selectedProcess.status}</span>
              <span>{t("user.llmCalls", { count: selectedProcess.llm_call_count })}</span>
              <span>{t("user.tokens", { count: selectedProcess.token_total })}</span>
            </div>
          ) : <span className="subtle">{t("user.noProcessYet")}</span>}
        </div>
        <div className="userRunControls">
          <label className="quanta">
            {t("user.quanta")}
            <input
              type="number"
              min={1}
              step={1}
              value={maxQuanta ?? ""}
              placeholder={t("scheduler.unlimitedPlaceholder")}
              title={t("scheduler.unlimitedHint")}
              onChange={(event) => onMaxQuantaChange(parseOptionalQuanta(event.currentTarget.value))}
            />
          </label>
          <button disabled={!hasProcess || isRunning} onClick={onRun}><Play size={15} />{t("user.run")}</button>
          <button onClick={onPause}><Pause size={15} />{t("user.pause")}</button>
          <button className="danger" disabled={!hasProcess} onClick={onStop}><Square size={13} />{t("user.stop")}</button>
        </div>
      </section>

      <div className="userNotices">
        <section className="userImageControls">
          <ImageSelect images={images} value={spawnImage} onChange={onSpawnImageChange} />
          <button onClick={() => onImportImage()}>{t("image.import")}</button>
          <input value={commitImageId} onChange={(event) => setCommitImageId(event.currentTarget.value)} placeholder={t("image.commitIdPlaceholder")} />
          <input value={commitName} onChange={(event) => setCommitName(event.currentTarget.value)} placeholder={t("image.commitNamePlaceholder")} />
          <input value={commitVersion} onChange={(event) => setCommitVersion(event.currentTarget.value)} placeholder={t("image.version")} />
          <button
            className="warning"
            disabled={!commitReady}
            onClick={() => onCommitImage({
              imageId: commitImageId.trim(),
              name: commitName.trim(),
              version: commitVersion.trim(),
              replace: false
            })}
          >
            {t("image.save")}
          </button>
        </section>

        {hasProcess ? <RatingPanel process={selectedProcess} onSave={onRate} /> : null}

        {!hasProcess ? (
          <section className="userStart">
            <h1>{t("user.startTask")}</h1>
            <input
              value={spawnWorkingDirectory}
              onChange={(event) => onSpawnWorkingDirectoryChange(event.currentTarget.value)}
              placeholder={t("user.initialCwdPlaceholder")}
              aria-label={t("user.initialCwd")}
            />
            <LLMProfileSelect
              profiles={llmProfiles}
              value={spawnLlmProfile}
              label={t("llmProfile.spawnLabel")}
              onChange={onSpawnLlmProfileChange}
              onCreate={onCreateLlmProfile}
              onUpdate={onUpdateLlmProfile}
              onDelete={onDeleteLlmProfile}
            />
            <textarea value={spawnGoal} onChange={(event) => onSpawnGoalChange(event.currentTarget.value)} />
            <button className="primary" disabled={!spawnGoal.trim()} onClick={onSpawn}>{t("user.start")}</button>
          </section>
        ) : null}

        {pendingRequests.length > 0 ? (
          <section className="userPendingRequests" aria-label={t("user.pendingRequests")}>
            {pendingRequests.map(({ request }) => (
              <HumanRequestCard
                className="userRequestCard"
                key={request.request_id}
                request={request}
                onRespond={onRespond}
              />
            ))}
          </section>
        ) : null}
      </div>

      <section className="userConversation" aria-label={t("user.conversation")}>
        {conversation.length === 0 ? (
          <div className="userEmpty">
            <MessageSquare size={20} />
            <span>{t("user.emptyConversation")}</span>
          </div>
        ) : conversation.map((item) => <ConversationBubble key={item.id} item={item} />)}
      </section>

      <footer className="userComposer">
        <div className="userComposerStatus">
          {selectedProcess?.interrupt_count ? <span className="interruptBanner"><AlertTriangle size={15} /> {t("operator.interruptPending")}</span> : null}
        </div>
        <input
          value={message}
          onChange={(event) => onMessageChange(event.currentTarget.value)}
          placeholder={t("user.messageAgent")}
          onKeyDown={(event) => {
            if (event.key === "Enter" && message.trim()) onSend("message");
          }}
        />
        <button disabled={!hasProcess || !message.trim()} onClick={() => onSend("message")}><Send size={15} />{t("user.send")}</button>
        <button disabled={!hasProcess || !message.trim()} className="warning" onClick={() => onSend("interrupt")}>{t("user.interrupt")}</button>
      </footer>
    </main>
  );
}

function ConversationBubble({ item }: { item: UserConversationItem }) {
  const { formatTime, t } = useI18n();
  if (item.role === "request") {
    return (
      <article className="conversationBubble request">
        <span className="bubbleRole">{t("user.needsInput")}</span>
        <p>{humanRequestPrompt(item.request)}</p>
        <time>{formatTime(item.time)}</time>
      </article>
    );
  }
  if (item.role === "decision") {
    const fallback = item.status === "rejected" ? t("user.requestRejected") : t("user.requestApproved");
    return (
      <article className="conversationBubble user">
        <span className="bubbleRole">{t("user.you")}</span>
        <p>{item.text || fallback}</p>
        <time>{formatTime(item.time)}</time>
      </article>
    );
  }
  return (
    <article className={`conversationBubble ${item.role}`}>
      <span className="bubbleRole">{item.role === "assistant" ? t("user.agent") : t("user.you")}</span>
      {item.role === "assistant" ? (
        <MarkdownMessage text={item.text} fallback={t("user.empty")} />
      ) : (
        <p>{item.text || t("user.empty")}</p>
      )}
      <time>{formatTime(item.time)}</time>
    </article>
  );
}
