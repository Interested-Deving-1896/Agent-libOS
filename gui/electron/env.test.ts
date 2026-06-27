import { afterEach, describe, expect, it } from "vitest";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { mergeDotenvIntoEnv, readDotenv, runtimeServerEnv } from "./env.js";

const roots: string[] = [];

function tempRoot() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "agent-libos-env-"));
  roots.push(root);
  return root;
}

afterEach(() => {
  while (roots.length > 0) {
    fs.rmSync(roots.pop()!, { recursive: true, force: true });
  }
});

describe("readDotenv", () => {
  it("parses the repo .env format used by the Python launcher", () => {
    const root = tempRoot();
    const envPath = path.join(root, ".env");
    fs.writeFileSync(
      envPath,
      [
        "# comment",
        "OPENAI_API_KEY='from-env-file'",
        'OPENAI_MODEL="gpt-test"',
        "export OPENAI_BASE_URL=https://api.example.test/v1",
        "MALFORMED",
        ""
      ].join("\n"),
      "utf8"
    );

    expect(readDotenv(envPath)).toEqual({
      OPENAI_API_KEY: "from-env-file",
      OPENAI_MODEL: "gpt-test",
      OPENAI_BASE_URL: "https://api.example.test/v1"
    });
  });
});

describe("runtimeServerEnv", () => {
  it("merges .env values without overriding inherited environment variables", () => {
    const root = tempRoot();
    fs.writeFileSync(path.join(root, ".env"), "OPENAI_API_KEY=from-file\nOPENAI_MODEL=gpt-test\n", "utf8");

    expect(runtimeServerEnv(root, { OPENAI_API_KEY: "inherited" })).toMatchObject({
      OPENAI_API_KEY: "inherited",
      OPENAI_MODEL: "gpt-test"
    });
  });

  it("does not duplicate inherited Windows environment keys with different casing", () => {
    const merged = mergeDotenvIntoEnv({ Path: "inherited" }, { PATH: "from-file" }, "win32");

    expect(merged.Path).toBe("inherited");
    expect(merged.PATH).toBeUndefined();
  });
});
