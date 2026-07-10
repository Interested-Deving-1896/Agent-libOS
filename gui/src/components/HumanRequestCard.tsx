import { useReducer, useRef } from "react";
import type { HumanPermissionPolicy, HumanRequest, HumanResponseInput } from "../api/types";
import { useI18n, type TranslationKey } from "../i18n";

export type HumanDecisionDraft = {
  answer: string;
  policy: HumanPermissionPolicy;
};

export type HumanResponseValidationError =
  | "question_answer_required"
  | "permission_approve_deny"
  | "permission_reject_allow";

type HumanResponseBuildResult =
  | { response: HumanResponseInput }
  | { error: HumanResponseValidationError };

type HumanRequestCardProps = {
  request: HumanRequest;
  className?: string;
  onRespond(request: HumanRequest, response: HumanResponseInput): Promise<boolean>;
};

export type HumanDecisionState = HumanDecisionDraft & {
  submitting: boolean;
  errorKey: TranslationKey | null;
};

export type HumanDecisionAction =
  | { type: "answer_changed"; answer: string }
  | { type: "policy_changed"; policy: HumanPermissionPolicy }
  | { type: "validation_failed"; errorKey: TranslationKey }
  | { type: "submission_started" }
  | { type: "submission_finished"; accepted: boolean };

export function humanDecisionReducer(state: HumanDecisionState, action: HumanDecisionAction): HumanDecisionState {
  if (action.type === "answer_changed") return { ...state, answer: action.answer, errorKey: null };
  if (action.type === "policy_changed") return { ...state, policy: action.policy, errorKey: null };
  if (action.type === "validation_failed") return { ...state, errorKey: action.errorKey };
  if (action.type === "submission_started") return { ...state, submitting: true, errorKey: null };
  return {
    ...state,
    submitting: false,
    errorKey: action.accepted ? null : "human.submitFailed"
  };
}

export function buildHumanResponse(
  request: HumanRequest,
  approved: boolean,
  draft: HumanDecisionDraft
): HumanResponseBuildResult {
  const requestType = request.payload?.type;
  if (requestType === "permission_request") {
    if (approved) {
      if (draft.policy === "always_deny") return { error: "permission_approve_deny" };
      return { response: { kind: "permission", approved: true, decision: { policy: draft.policy } } };
    }
    if (draft.policy === "always_allow") return { error: "permission_reject_allow" };
    return { response: { kind: "permission", approved: false, decision: { policy: draft.policy } } };
  }
  if (requestType === "question") {
    if (!approved) return { response: { kind: "question", approved: false } };
    const answer = draft.answer.trim();
    if (!answer) return { error: "question_answer_required" };
    return { response: { kind: "question", approved: true, answer } };
  }
  return { response: { kind: "approval", approved } };
}

export function HumanRequestCard({ request, className = "humanCard", onRespond }: HumanRequestCardProps) {
  const { t } = useI18n();
  const requestType = request.payload?.type;
  const isPermission = requestType === "permission_request";
  const isQuestion = requestType === "question";
  const [state, dispatch] = useReducer(humanDecisionReducer, {
    answer: "",
    policy: "ask_each_time",
    submitting: false,
    errorKey: null
  });
  const submissionInFlight = useRef(false);

  async function submit(approved: boolean) {
    if (submissionInFlight.current) return;
    const built = buildHumanResponse(request, approved, state);
    if ("error" in built) {
      dispatch({ type: "validation_failed", errorKey: validationErrorKey(built.error) });
      return;
    }
    submissionInFlight.current = true;
    dispatch({ type: "submission_started" });
    try {
      const accepted = await onRespond(request, built.response).catch(() => false);
      dispatch({ type: "submission_finished", accepted });
      // The authoritative snapshot removes a completed request. Keep the draft
      // intact until then so a failed request or refresh never destroys input.
    } finally {
      submissionInFlight.current = false;
    }
  }

  const approveDisabled = state.submitting || (isQuestion && !state.answer.trim()) || (isPermission && state.policy === "always_deny");
  const rejectDisabled = state.submitting || (isPermission && state.policy === "always_allow");
  const prompt = String(request.payload?.question ?? request.payload?.reason ?? request.payload?.type ?? t("operator.humanRequestFallback"));

  return (
    <div className={`${className} typedHumanRequest`} aria-busy={state.submitting || undefined}>
      <strong className="humanRequestPrompt">{prompt}</strong>
      {isPermission ? (
        <label className="humanDecisionControl">
          <span>{t("human.permissionPolicy")}</span>
          <select
            name="permission-policy"
            value={state.policy}
            disabled={state.submitting}
            onChange={(event) => {
              dispatch({ type: "policy_changed", policy: event.currentTarget.value as HumanPermissionPolicy });
            }}
          >
            <option value="always_allow">{t("human.policyAlwaysAllow")}</option>
            <option value="ask_each_time">{t("human.policyAskEachTime")}</option>
            <option value="always_deny">{t("human.policyAlwaysDeny")}</option>
          </select>
        </label>
      ) : null}
      {isQuestion ? (
        <label className="humanDecisionControl">
          <span>{t("human.answer")}</span>
          <input
            name="human-answer"
            required
            placeholder={t("human.answerPlaceholder")}
            value={state.answer}
            disabled={state.submitting}
            onChange={(event) => {
              dispatch({ type: "answer_changed", answer: event.currentTarget.value });
            }}
            onKeyDown={(event) => {
              if (event.key === "Enter" && state.answer.trim()) void submit(true);
            }}
          />
        </label>
      ) : null}
      <div className="humanDecisionActions">
        <button disabled={approveDisabled} onClick={() => void submit(true)}>
          {isQuestion ? t("human.submitAnswer") : t("human.approve")}
        </button>
        <button disabled={rejectDisabled} className="danger" onClick={() => void submit(false)}>
          {t("human.reject")}
        </button>
      </div>
      {state.errorKey ? <span className="humanDecisionError" role="alert">{t(state.errorKey)}</span> : null}
    </div>
  );
}

function validationErrorKey(error: HumanResponseValidationError): TranslationKey {
  if (error === "question_answer_required") return "human.answerRequired";
  if (error === "permission_approve_deny") return "human.approveDenyInvalid";
  return "human.rejectAllowInvalid";
}
