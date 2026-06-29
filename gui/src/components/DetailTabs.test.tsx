import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { RuntimeSnapshot } from "../api/types";
import { I18nProvider } from "../i18n";
import { DetailTabs } from "./DetailTabs";

describe("DetailTabs", () => {
  it("renders the MCP registry tab from snapshot data", () => {
    const html = renderToStaticMarkup(
      <I18nProvider>
        <DetailTabs
          process={null}
          snapshot={snapshot()}
          onImportImage={() => undefined}
          onCommitImage={() => undefined}
          onUseImageForSpawn={() => undefined}
          onUseImageForExec={() => undefined}
          onRate={async () => true}
          onInspectImage={async () => ({ image: {} as never, registry: {}, artifact: null })}
        />
      </I18nProvider>
    );

    expect(html).toContain("MCP");
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
      default_max_quanta: null
    },
    processes: [],
    human_requests: [],
    events: [],
    audit: [],
    llm_calls: [],
    object_tasks: [],
    tools: [],
    llm_profiles: [],
    images: [],
    skills: [],
    jsonrpc_endpoints: [],
    mcp_servers: [{ server_id: "demo-mcp" }],
    modules: []
  };
}
