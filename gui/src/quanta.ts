export type OptionalQuanta = number | null;

export function parseOptionalQuanta(value: string): OptionalQuanta {
  const trimmed = value.trim();
  if (trimmed === "") return null;
  const parsed = Number(trimmed);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null;
}
