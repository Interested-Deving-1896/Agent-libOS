import type { RuntimeSnapshot } from "./api/types";

export function reconcileSelectedPid(
  snapshot: RuntimeSnapshot,
  current: string | null,
  { preserveExisting = true }: { preserveExisting?: boolean } = {}
): string | null {
  if (preserveExisting && current && snapshot.processes.some((process) => process.pid === current)) {
    return current;
  }
  return snapshot.processes[0]?.pid ?? null;
}
