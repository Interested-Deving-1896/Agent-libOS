export type ImageManifestPreview = {
  image_id: string | null;
  name: string | null;
  version: string | null;
  default_tools_count: number | null;
  required_capabilities_count: number | null;
  bytes: number;
};

export function previewImageManifest(text: string): ImageManifestPreview {
  const bytes = new Blob([text]).size;
  const parsed = parseJsonPreview(text);
  if (parsed) return { ...parsed, bytes };
  return {
    image_id: yamlValue(text, "image_id"),
    name: yamlValue(text, "name"),
    version: yamlValue(text, "version"),
    default_tools_count: yamlListCount(text, "default_tools"),
    required_capabilities_count: yamlListCount(text, "required_capabilities"),
    bytes
  };
}

function parseJsonPreview(text: string): Omit<ImageManifestPreview, "bytes"> | null {
  try {
    const value = JSON.parse(text) as unknown;
    const image = unwrapImage(value);
    if (!image) return null;
    return {
      image_id: stringValue(image.image_id),
      name: stringValue(image.name),
      version: stringValue(image.version),
      default_tools_count: Array.isArray(image.default_tools) ? image.default_tools.length : null,
      required_capabilities_count: Array.isArray(image.required_capabilities) ? image.required_capabilities.length : null
    };
  } catch {
    return null;
  }
}

function unwrapImage(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  const image = record.image;
  if (image && typeof image === "object" && !Array.isArray(image)) return image as Record<string, unknown>;
  return record;
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function yamlValue(text: string, key: string): string | null {
  const escaped = key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = text.match(new RegExp(`^\\s*${escaped}\\s*:\\s*["']?([^"'#\\n]+)`, "m"));
  return match?.[1]?.trim() || null;
}

function yamlListCount(text: string, key: string): number | null {
  const lines = text.split(/\r?\n/);
  const header = new RegExp(`^(\\s*)${key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*:\\s*$`);
  const start = lines.findIndex((line) => header.test(line));
  if (start < 0) return null;
  const baseIndent = (lines[start].match(/^\s*/) ?? [""])[0].length;
  let count = 0;
  for (const line of lines.slice(start + 1)) {
    if (!line.trim()) continue;
    const indent = (line.match(/^\s*/) ?? [""])[0].length;
    if (indent <= baseIndent) break;
    if (line.trimStart().startsWith("-")) count += 1;
  }
  return count;
}
