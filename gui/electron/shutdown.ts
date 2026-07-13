export type ServerResponse = {
  ok: boolean;
  status: number;
  body: string;
};

export function isCompletedShutdownResponse(response: ServerResponse): boolean {
  if (!response.ok || response.status < 200 || response.status >= 300) return false;
  try {
    const payload: unknown = JSON.parse(response.body);
    return Boolean(
      payload &&
        typeof payload === "object" &&
        (payload as { ok?: unknown }).ok === true &&
        (payload as { status?: unknown }).status === "stopped"
    );
  } catch {
    return false;
  }
}
