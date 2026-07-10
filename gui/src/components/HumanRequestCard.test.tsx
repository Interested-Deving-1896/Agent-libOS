import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { HumanRequest } from "../api/types";
import { I18nProvider } from "../i18n";
import { buildHumanResponse, humanDecisionReducer, HumanRequestCard, type HumanDecisionState } from "./HumanRequestCard";

describe("HumanRequestCard", () => {
  it("builds directionally valid permission decisions", () => {
    const request = humanRequest("permission_request");

    expect(buildHumanResponse(request, true, { answer: "", policy: "always_allow" })).toEqual({
      response: { kind: "permission", approved: true, decision: { policy: "always_allow" } }
    });
    expect(buildHumanResponse(request, false, { answer: "", policy: "always_deny" })).toEqual({
      response: { kind: "permission", approved: false, decision: { policy: "always_deny" } }
    });
    expect(buildHumanResponse(request, true, { answer: "", policy: "ask_each_time" })).toEqual({
      response: { kind: "permission", approved: true, decision: { policy: "ask_each_time" } }
    });
    expect(buildHumanResponse(request, false, { answer: "", policy: "ask_each_time" })).toEqual({
      response: { kind: "permission", approved: false, decision: { policy: "ask_each_time" } }
    });
    expect(buildHumanResponse(request, false, { answer: "", policy: "always_allow" })).toEqual({
      error: "permission_reject_allow"
    });
    expect(buildHumanResponse(request, true, { answer: "", policy: "always_deny" })).toEqual({
      error: "permission_approve_deny"
    });
  });

  it("requires a non-empty string for approved questions and omits it on rejection", () => {
    const request = humanRequest("question");

    expect(buildHumanResponse(request, true, { answer: "   ", policy: "ask_each_time" })).toEqual({
      error: "question_answer_required"
    });
    expect(buildHumanResponse(request, true, { answer: " eu-west ", policy: "ask_each_time" })).toEqual({
      response: { kind: "question", approved: true, answer: "eu-west" }
    });
    expect(buildHumanResponse(request, false, { answer: "draft remains local", policy: "ask_each_time" })).toEqual({
      response: { kind: "question", approved: false }
    });
  });

  it("keeps ordinary approvals boolean-only", () => {
    const request = humanRequest("external_operation_approval");

    expect(buildHumanResponse(request, true, { answer: "ignored", policy: "always_allow" })).toEqual({
      response: { kind: "approval", approved: true }
    });
    expect(buildHumanResponse(request, false, { answer: "ignored", policy: "always_deny" })).toEqual({
      response: { kind: "approval", approved: false }
    });
  });

  it("keeps the draft when submission fails instead of optimistically clearing it", () => {
    const state: HumanDecisionState = {
      answer: "carefully chosen answer",
      policy: "always_allow",
      submitting: true,
      errorKey: null
    };

    expect(humanDecisionReducer(state, { type: "submission_finished", accepted: false })).toEqual({
      answer: "carefully chosen answer",
      policy: "always_allow",
      submitting: false,
      errorKey: "human.submitFailed"
    });
  });

  it("renders request-type-specific controls", () => {
    const permissionHtml = render(humanRequest("permission_request"));
    const questionHtml = render(humanRequest("question"));
    const approvalHtml = render(humanRequest("external_operation_approval"));

    expect(permissionHtml).toContain('value="always_allow"');
    expect(permissionHtml).toContain('value="ask_each_time"');
    expect(permissionHtml).toContain('value="always_deny"');
    expect(permissionHtml).not.toContain('name="human-answer"');
    expect(questionHtml).toContain('name="human-answer"');
    expect(questionHtml).toContain('required=""');
    expect(approvalHtml).not.toContain('name="human-answer"');
    expect(approvalHtml).not.toContain('name="permission-policy"');
  });
});

function render(request: HumanRequest): string {
  return renderToStaticMarkup(
    <I18nProvider>
      <HumanRequestCard request={request} onRespond={async () => true} />
    </I18nProvider>
  );
}

function humanRequest(type: string): HumanRequest {
  return {
    request_id: `request_${type}`,
    pid: "pid_1",
    human: "owner",
    payload: { type, question: `Handle ${type}?` },
    status: "pending",
    decision: null,
    blocking: true,
    created_at: "2026-07-10T00:00:00Z",
    updated_at: "2026-07-10T00:00:00Z"
  };
}
