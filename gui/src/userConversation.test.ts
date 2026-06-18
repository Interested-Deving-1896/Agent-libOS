import { describe, expect, it } from "vitest";
import type { RuntimeSnapshot } from "./api/types";
import { deriveUserConversation } from "./userConversation";

describe("deriveUserConversation", () => {
  it("maps delivered human_output to assistant messages", () => {
    const items = deriveUserConversation(snapshot(), "pid_1");

    expect(items).toContainEqual(
      expect.objectContaining({
        id: "assistant:hreq_output",
        role: "assistant",
        text: "Build completed."
      })
    );
  });

  it("maps human process messages to user messages", () => {
    const items = deriveUserConversation(snapshot(), "pid_1");

    expect(items).toContainEqual(
      expect.objectContaining({
        id: "message:pmsg_user",
        role: "user",
        text: "Please run the tests."
      })
    );
  });

  it("maps pending human questions to actionable request cards", () => {
    const items = deriveUserConversation(snapshot(), "pid_1");

    expect(items).toContainEqual(
      expect.objectContaining({
        id: "request:hreq_question",
        role: "request",
        text: "Which branch should I use?"
      })
    );
  });

  it("does not include raw audit events or llm calls in the user conversation", () => {
    const items = deriveUserConversation(snapshot(), "pid_1");

    expect(items).toHaveLength(3);
    expect(items.some((item) => item.id.includes("audit"))).toBe(false);
    expect(items.some((item) => item.id.includes("event"))).toBe(false);
    expect(items.some((item) => item.id.includes("llm"))).toBe(false);
  });
});

function snapshot(): RuntimeSnapshot {
  return {
    db: "local",
    scheduler: {
      auto_run: true,
      running: false,
      paused: false,
      task_id: null,
      reason: null,
      last_result: [],
      last_error: null,
      started_at: null,
      finished_at: null,
      default_max_quanta: 25
    },
    processes: [
      {
        pid: "pid_1",
        parent_pid: null,
        image_id: "coding-agent:v0",
        status: "runnable",
        goal_oid: null,
        checkpoint_head: null,
        working_directory: ".",
        status_message: null,
        loaded_skills: {},
        tool_table: {},
        capabilities: [],
        terminal: false,
        unread_message_count: 0,
        interrupt_count: 0,
        llm_call_count: 1,
        token_total: 12,
        messages: [
          {
            message_id: "pmsg_user",
            sender: "human:owner",
            recipient_pid: "pid_1",
            kind: "normal",
            subject: "",
            body: "Please run the tests.",
            channel: "gui",
            status: "unread",
            created_at: "2026-06-19T01:00:00.000Z",
            payload: { source: "human_input" }
          },
          {
            message_id: "pmsg_system",
            sender: "runtime",
            recipient_pid: "pid_1",
            kind: "normal",
            subject: "",
            body: "Internal scheduler note.",
            channel: "runtime",
            status: "unread",
            created_at: "2026-06-19T01:00:01.000Z",
            payload: {}
          }
        ]
      }
    ],
    human_requests: [
      {
        request_id: "hreq_output",
        pid: "pid_1",
        human: "owner",
        payload: { type: "output", message: "Build completed.", channel: "terminal" },
        status: "delivered",
        decision: { delivered: true },
        blocking: false,
        created_at: "2026-06-19T01:00:02.000Z",
        updated_at: "2026-06-19T01:00:03.000Z"
      },
      {
        request_id: "hreq_question",
        pid: "pid_1",
        human: "owner",
        payload: { type: "question", question: "Which branch should I use?" },
        status: "pending",
        decision: null,
        blocking: true,
        created_at: "2026-06-19T01:00:04.000Z",
        updated_at: "2026-06-19T01:00:04.000Z"
      }
    ],
    events: [
      {
        event_id: "event_1",
        type: "human_output",
        source: "pid_1",
        target: "human:owner",
        payload: { request_id: "hreq_output" },
        priority: "normal",
        created_at: "2026-06-19T01:00:03.000Z"
      }
    ],
    audit: [
      {
        record_id: "audit_1",
        timestamp: "2026-06-19T01:00:03.000Z",
        actor: "pid_1",
        action: "human.output",
        target: "human:owner",
        decision: { request_id: "hreq_output" },
        capability_refs: []
      }
    ],
    llm_calls: [
      {
        call_id: "llm_1",
        pid: "pid_1",
        image_id: "coding-agent:v0",
        purpose: "agent_loop",
        status: "ok",
        model: "mock",
        response_content: "raw model output",
        tool_calls: [],
        usage: { total_tokens: 12 },
        reasoning: null,
        error: null,
        created_at: "2026-06-19T01:00:01.000Z",
        completed_at: "2026-06-19T01:00:02.000Z"
      }
    ],
    tools: [],
    skills: [],
    jsonrpc_endpoints: [],
    modules: []
  };
}
