import { describe, expect, it } from "vitest";
import { previewImageManifest } from "./imagePreview";

describe("previewImageManifest", () => {
  it("previews JSON image manifests", () => {
    const preview = previewImageManifest(JSON.stringify({
      image: {
        image_id: "json-agent:v0",
        name: "json-agent",
        version: "v0",
        default_tools: ["echo"],
        required_capabilities: [{ resource: "filesystem:/README.md", rights: ["read"] }],
        required_modules: [{ module_id: "module:v0", source_sha256: "0".repeat(64) }]
      }
    }));

    expect(preview).toMatchObject({
      image_id: "json-agent:v0",
      name: "json-agent",
      version: "v0",
      default_tools_count: 1,
      required_capabilities_count: 1,
      required_modules_count: 1
    });
  });

  it("previews simple IMAGE.yaml package manifests", () => {
    const preview = previewImageManifest(`
image:
  image_id: package-agent:v0
  name: package-agent
  version: v1
  default_tools:
    - echo
    - list_files
  required_capabilities:
    - resource: filesystem:/README.md
      rights: [read]
  required_modules:
    - module_id: module:v0
      source_sha256: ${"0".repeat(64)}
`);

    expect(preview).toMatchObject({
      image_id: "package-agent:v0",
      name: "package-agent",
      version: "v1",
      default_tools_count: 2,
      required_capabilities_count: 1,
      required_modules_count: 1
    });
  });
});
