import { AlertTriangle, Bot, Database, MessageSquare, Pause, Play, RefreshCw, Send, Settings, Square } from "lucide-react";
import { useMemo, useState } from "react";
import type { GuiConnection, HumanRequest, RuntimeProcess, RuntimeSnapshot } from "../api/types";
import { useI18n } from "../i18n";
import { deriveUserConversation, humanRequestPrompt, type UserConversationItem } from "../userConversation";
import { LanguageSwitch } from "./LanguageSwitch";

type UserPageProps = {
  connection: GuiConnection | null;
  snapshot: RuntimeSnapshot | null;
  selectedPid: string | null;
  selectedProcess: RuntimeProcess | null;
  maxQuanta: number;
  spawnGoal: string;
  message: string;
  onSelectPid(pid: string): void;
  onMaxQuantaChange(value: number): void;
  onSpawnGoalChange(value: string): void;
  onMessageChange(value: string): void;
  onSpawn(): void;
  onSend(kind: "message" | "interrupt"): void;
  onRespond(request: HumanRequest, approved: boolean, answer?: string): void;
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
  message,
  onSelectPid,
  onMaxQuantaChange,
  onSpawnGoalChange,
  onMessageChange,
  onSpawn,
  onSend,
  onRespond,
  onRun,
  onPause,
  onRefresh,
  onOpenDb,
  onShowOperator,
  onStop
}: UserPageProps) {
  const { t } = useI18n();
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const conversation = useMemo(() => deriveUserConversation(snapshot, selectedPid), [snapshot, selectedPid]);
  const pendingRequests = conversation.filter((item): item is Extract<UserConversationItem, { role: "request" }> => item.role === "request");
  const isRunning = Boolean(snapshot?.scheduler.running);
  const hasProcess = Boolean(selectedProcess);

  function answerFor(requestId: string) {
    return answers[requestId] ?? "";
  }

  function updateAnswer(requestId: string, value: string) {
    setAnswers((current) => ({ ...current, [requestId]: value }));
  }

  function submitAnswer(request: HumanRequest, approved: boolean) {
    const answer = answerFor(request.request_id);
    onRespond(request, approved, answer);
    setAnswers((current) => {
      const next = { ...current };
      delete next[request.request_id];
      return next;
    });
  }

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
              <span>{selectedProcess.status}</span>
              <span>{t("user.llmCalls", { count: selectedProcess.llm_call_count })}</span>
              <span>{t("user.tokens", { count: selectedProcess.token_total })}</span>
            </div>
          ) : <span className="subtle">{t("user.noProcessYet")}</span>}
        </div>
        <div className="userRunControls">
          <label className="quanta">
            {t("user.quanta")}
            <input type="number" min={1} max={200} value={maxQuanta} onChange={(event) => onMaxQuantaChange(Number(event.currentTarget.value))} />
          </label>
          <button disabled={!hasProcess || isRunning} onClick={onRun}><Play size={15} />{t("user.run")}</button>
          <button onClick={onPause}><Pause size={15} />{t("user.pause")}</button>
          <button className="danger" disabled={!hasProcess} onClick={onStop}><Square size={13} />{t("user.stop")}</button>
        </div>
      </section>

      <div className="userNotices">
        {!hasProcess ? (
          <section className="userStart">
            <h1>{t("user.startTask")}</h1>
            <textarea value={spawnGoal} onChange={(event) => onSpawnGoalChange(event.currentTarget.value)} />
            <button className="primary" disabled={!spawnGoal.trim()} onClick={onSpawn}>{t("user.start")}</button>
          </section>
        ) : null}

        {pendingRequests.length > 0 ? (
          <section className="userPendingRequests" aria-label={t("user.pendingRequests")}>
            {pendingRequests.map(({ request, text }) => (
              <div className="userRequestCard" key={request.request_id}>
                <strong>{text}</strong>
                <input
                  placeholder={t("user.answerPlaceholder")}
                  value={answerFor(request.request_id)}
                  onChange={(event) => updateAnswer(request.request_id, event.currentTarget.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") submitAnswer(request, true);
                  }}
                />
                <button onClick={() => submitAnswer(request, true)}>{t("user.submit")}</button>
                <button className="secondary" onClick={() => submitAnswer(request, false)}>{t("user.reject")}</button>
              </div>
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
  return (
    <article className={`conversationBubble ${item.role}`}>
      <span className="bubbleRole">{item.role === "assistant" ? t("user.agent") : t("user.you")}</span>
      <p>{item.text || t("user.empty")}</p>
      <time>{formatTime(item.time)}</time>
    </article>
  );
}
