import { randomBytes } from "node:crypto";
import type { TokenUsage } from "./types.js";

export const nowIso = () => new Date().toISOString();

export const newId = (prefix = "") =>
  `${prefix}${Date.now().toString(36)}${randomBytes(3).toString("hex")}`;

export const clamp = (n: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, n));

export const slug = (s: string) =>
  s
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");

export const truncate = (s: string, max: number) => (s.length > max ? `${s.slice(0, max - 1)}…` : s);

export function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(signal.reason ?? new Error("aborted"));
    const t = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(t);
      reject(signal?.reason ?? new Error("aborted"));
    };
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

export const emptyUsage = (): TokenUsage => ({ input: 0, output: 0, cacheRead: 0, cacheWrite: 0 });

export const totalTokens = (u: TokenUsage) => u.input + u.output + u.cacheRead + u.cacheWrite;

export const addUsage = (a: TokenUsage, b: TokenUsage): TokenUsage => ({
  input: a.input + b.input,
  output: a.output + b.output,
  cacheRead: a.cacheRead + b.cacheRead,
  cacheWrite: a.cacheWrite + b.cacheWrite,
  costUsd: (a.costUsd ?? 0) + (b.costUsd ?? 0) || undefined,
});

// Files that must never be read into prompts or sent to any remote lane.
export const SECRET_FILE_RE =
  /(^|\/)(\.env(\..*)?|.*\.pem|.*\.key|id_rsa.*|id_ed25519.*|credentials(\.json)?|\.npmrc|\.pypirc|secrets?\.(json|ya?ml|toml))$/i;

const quiet = () => process.env.BURNER_QUIET === "1";
const paint = (code: number, s: string) => (process.stderr.isTTY ? `\x1b[${code}m${s}\x1b[0m` : s);

export const log = {
  info: (...a: unknown[]) => !quiet() && console.error(paint(36, "•"), ...a),
  warn: (...a: unknown[]) => !quiet() && console.error(paint(33, "!"), ...a),
  error: (...a: unknown[]) => console.error(paint(31, "✗"), ...a),
  debug: (...a: unknown[]) => process.env.BURNER_DEBUG === "1" && console.error(paint(90, "·"), ...a),
};
