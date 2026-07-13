import { describe, expect, it } from "vitest";
import { isCompletedShutdownResponse } from "./shutdown.js";

describe("isCompletedShutdownResponse", () => {
  it("accepts only an explicit completed teardown", () => {
    expect(
      isCompletedShutdownResponse({ ok: true, status: 200, body: JSON.stringify({ ok: true, status: "stopped" }) })
    ).toBe(true);
    expect(
      isCompletedShutdownResponse({
        ok: false,
        status: 503,
        body: JSON.stringify({ ok: false, error: { retryable: true } })
      })
    ).toBe(false);
    expect(
      isCompletedShutdownResponse({ ok: true, status: 200, body: JSON.stringify({ ok: true, status: "shutting_down" }) })
    ).toBe(false);
    expect(isCompletedShutdownResponse({ ok: true, status: 200, body: "not-json" })).toBe(false);
  });
});
