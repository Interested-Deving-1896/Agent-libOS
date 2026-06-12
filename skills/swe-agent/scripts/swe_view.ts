function numberValue(value: unknown, fallback: number, min: number, max: number): number {
  const parsed = Number(value);
  const selected = Number.isFinite(parsed) ? Math.trunc(parsed) : fallback;
  return Math.max(min, Math.min(max, selected));
}

function numbered(lines: string[], offset: number): string {
  return lines.map((line, index) => `${String(offset + index).padStart(6, " ")}  ${line}`).join("\n");
}

export async function run(args: Record<string, unknown>, libos: { syscall(name: string, args: unknown): Promise<any> }) {
  const path = String(args.path ?? ".");
  const kind = String(args.kind ?? "file");
  if (kind === "directory") {
    const limit = numberValue(args.limit, 200, 1, 1024);
    const listing = await libos.syscall("filesystem.read_directory", { path, limit });
    return {
      kind: "directory",
      path: listing.path ?? path,
      count: listing.count ?? (listing.entries ?? []).length,
      truncated: Boolean(listing.truncated),
      entries: (listing.entries ?? []).map((entry: any) => ({
        name: entry.name,
        path: entry.path,
        kind: entry.kind,
        size_bytes: entry.size_bytes ?? null,
      })),
    };
  }
  const maxBytes = numberValue(args.max_bytes, 65536, 1, 1048576);
  const file = await libos.syscall("filesystem.read_text", { path, max_bytes: maxBytes });
  const content = String(file.content ?? "");
  const lines = content.split(/\r?\n/);
  const total = lines.length;
  const start = numberValue(args.start_line, 1, 1, Math.max(total, 1));
  const window = numberValue(args.window, 100, 1, 200);
  const selected = lines.slice(start - 1, start - 1 + window);
  const end = start + selected.length - 1;
  return {
    kind: "file",
    path: file.path ?? path,
    start_line: start,
    end_line: end,
    total_lines: total,
    truncated_by_bytes: Boolean(file.truncated),
    lines_above: start - 1,
    lines_below: Math.max(total - end, 0),
    content: numbered(selected, start),
  };
}
