import * as path from "node:path";

export const productionRendererScheme = "agent-libos";
export const productionRendererHost = "app";
export const productionRendererOrigin = `${productionRendererScheme}://${productionRendererHost}`;
export const productionRendererEntryUrl = `${productionRendererOrigin}/index.html`;

export function resolveProductionRendererPath(distRoot: string, requestUrl: string): string | null {
  const authorityMarker = "://";
  const authorityIndex = requestUrl.indexOf(authorityMarker);
  if (authorityIndex < 0) return null;
  const rawPathIndex = requestUrl.indexOf("/", authorityIndex + authorityMarker.length);
  const rawPath = (rawPathIndex < 0 ? "/" : requestUrl.slice(rawPathIndex)).split(/[?#]/, 1)[0];
  try {
    if (decodeURIComponent(rawPath).replace(/\\/g, "/").split("/").includes("..")) return null;
  } catch {
    return null;
  }
  let parsed: URL;
  try {
    parsed = new URL(requestUrl);
  } catch {
    return null;
  }
  if (
    parsed.protocol !== `${productionRendererScheme}:` ||
    parsed.hostname !== productionRendererHost ||
    parsed.username !== "" ||
    parsed.password !== "" ||
    parsed.port !== ""
  ) {
    return null;
  }
  let pathname: string;
  try {
    pathname = decodeURIComponent(parsed.pathname);
  } catch {
    return null;
  }
  if (pathname.includes("\0")) return null;
  const relative = pathname === "/" ? "index.html" : pathname.replace(/^\/+/, "");
  const root = path.resolve(distRoot);
  const candidate = path.resolve(root, relative);
  if (candidate !== root && !candidate.startsWith(`${root}${path.sep}`)) return null;
  return candidate;
}
