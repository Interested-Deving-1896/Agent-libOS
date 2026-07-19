import { describe, expect, it } from "vitest";
import type { RuntimeProcess } from "../api/types";
import { indexProcessTree } from "./ProcessTree";

describe("indexProcessTree", () => {
  it("groups roots and siblings in one pass while preserving snapshot order", () => {
    const root = process("root", null);
    const firstChild = process("child-1", "root");
    const secondRoot = process("root-2", null);
    const secondChild = process("child-2", "root");

    const indexed = indexProcessTree([root, firstChild, secondRoot, secondChild]);

    expect(indexed.roots).toEqual([root, secondRoot]);
    expect(indexed.children.get("root")).toEqual([firstChild, secondChild]);
    expect(indexed.children.size).toBe(1);
  });

  it("keeps a source-window child visible when its parent is omitted", () => {
    const orphanedChild = process("active-child", "omitted-parent");
    const root = process("visible-root", null);

    const indexed = indexProcessTree([orphanedChild, root]);

    expect(indexed.roots).toEqual([orphanedChild, root]);
    expect(indexed.children.size).toBe(0);
  });
});

function process(pid: string, parentPid: string | null): RuntimeProcess {
  return {
    pid,
    parent_pid: parentPid,
    image_id: "base-agent:v0",
    llm_profile_id: "default",
    status: "runnable",
    goal_oid: null,
    checkpoint_head: null,
    working_directory: ".",
    status_message: null,
    wait_state: null,
    outcome: null,
    state_generation: 0,
    loaded_skills: {},
    tool_table: {},
    capabilities: [],
    terminal: false,
    unread_message_count: 0,
    interrupt_count: 0,
    messages: [],
    llm_call_count: 0,
    token_total: 0,
    rating: null
  };
}
