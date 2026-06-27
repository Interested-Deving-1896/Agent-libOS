import { describe, expect, it } from "vitest";
import { parseOptionalQuanta } from "./quanta";

describe("parseOptionalQuanta", () => {
  it("accepts only positive safe integers", () => {
    expect(parseOptionalQuanta("")).toBeNull();
    expect(parseOptionalQuanta(" 3 ")).toBe(3);
    expect(parseOptionalQuanta("0")).toBeNull();
    expect(parseOptionalQuanta("-1")).toBeNull();
    expect(parseOptionalQuanta("1.5")).toBeNull();
    expect(parseOptionalQuanta("abc")).toBeNull();
  });
});
