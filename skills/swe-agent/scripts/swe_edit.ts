function numberValue(value: unknown, fallback: number, min: number, max: number): number {
  const parsed = Number(value);
  const selected = Number.isFinite(parsed) ? Math.trunc(parsed) : fallback;
  return Math.max(min, Math.min(max, selected));
}

function positionsOf(text: string, needle: string): number[] {
  const result: number[] = [];
  let index = 0;
  while (true) {
    const found = text.indexOf(needle, index);
    if (found < 0) return result;
    result.push(found);
    index = found + needle.length;
  }
}

export async function run(args: Record<string, unknown>, libos: { syscall(name: string, args: unknown): Promise<any> }) {
  const path = String(args.path ?? "");
  if (!path) throw new Error("path is required");
  const newText = String(args.new_text ?? "");
  const create = Boolean(args.create_if_missing);
  const oldProvided = typeof args.old_text === "string" && args.old_text.length > 0;
  const hasRange = args.start_line !== undefined && args.end_line !== undefined;
  if (create && !oldProvided && !hasRange) {
    const write = await libos.syscall("filesystem.write_text", {
      path,
      content: newText,
      overwrite: false,
    });
    return { path: write.path ?? path, created: Boolean(write.created), edit: "create" };
  }
  const file = await libos.syscall("filesystem.read_text", { path, max_bytes: 1048576 });
  if (Boolean(file.truncated)) {
    throw new Error(
      "swe_edit refuses to overwrite a truncated source file; use a bounded editor that preserves the complete file",
    );
  }
  const content = String(file.content ?? "");
  let updated = content;
  let edit = "replace_text";
  let replacements = 0;
  if (hasRange) {
    const lines = content.split(/\r?\n/);
    const start = numberValue(args.start_line, 1, 1, Math.max(lines.length, 1));
    const end = numberValue(args.end_line, start, start, Math.max(lines.length, start));
    const replacement = newText.split(/\r?\n/);
    updated = [
      ...lines.slice(0, start - 1),
      ...replacement,
      ...lines.slice(end),
    ].join("\n");
    edit = "replace_lines";
    replacements = Math.max(end - start + 1, 0);
  } else {
    const oldText = String(args.old_text ?? "");
    if (!oldText) throw new Error("old_text or start_line/end_line is required");
    const positions = positionsOf(content, oldText);
    const occurrence = numberValue(args.occurrence, 1, 1, Math.max(positions.length, 1));
    if (positions.length === 0) throw new Error("old_text was not found");
    if (occurrence > positions.length) throw new Error(`occurrence ${occurrence} exceeds matches ${positions.length}`);
    const at = positions[occurrence - 1];
    updated = content.slice(0, at) + newText + content.slice(at + oldText.length);
    replacements = 1;
  }
  if (updated === content) {
    return { path: file.path ?? path, changed: false, edit, replacements };
  }
  const write = await libos.syscall("filesystem.write_text", {
    path,
    content: updated,
    overwrite: true,
  });
  return {
    path: write.path ?? path,
    changed: true,
    edit,
    replacements,
    bytes_written: write.bytes_written,
  };
}
