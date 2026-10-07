// Mine unfinished ideas from Claude Code JSONL transcripts.
// `root` is injected (tests must not touch ~/.claude). Nothing is sent over the network.

import { createReadStream } from "node:fs";
import { readdir } from "node:fs/promises";
import { relative, sep } from "node:path";
import { createInterface } from "node:readline";
import type { Idea } from "./types.js";
import { newId, SECRET_FILE_RE, truncate } from "./util.js";

const MIN_CHARS = 24;
const MAX_CHARS = 400;
const DEFAULT_LIMIT = 40;
/** About eight hours of recency, so a marker nudges rank without burying newer sessions. */
const UNFINISHED_BOOST = 30;

const VERBS = new Set(
  `add adjust allow analyze apply ask attach audit avoid bind bootstrap build bump cache call change check
  clarify clean cleanup close coerce collapse combine compare compile compress configure connect consider
  consolidate convert copy cover create debug decode decrypt dedupe delete deploy design detect disable
  document downgrade drop emit enable encode encrypt ensure escape expand explain export expose extend
  extract filter find finish fix flatten flush format generate guard handle hash help hide hoist hook
  implement improve include inject inline install integrate introduce invalidate investigate keep limit
  lint list listen load log make memoize migrate mine minify mock monitor mount move nest normalize open
  optimize parse patch persist pin plan polish prefetch preload prevent profile protect publish purge
  queue read record redact reduce refactor refresh register reload remember remove rename render replace
  reset resolve retry return review revise revoke rewrite rotate run save scan scaffold schedule send
  separate set setup ship show simplify skip sort speed split start stop store strip support switch sync
  tag test throttle toggle trace track trim truncate turn undo update upgrade use validate verify watch
  wire wrap write`
    .split(/\s+/)
    .filter(Boolean),
);

const REQUEST_RE = /\b(?:i want|we should|todo|let['’]s|lets)\b/i;
const PLEASE_RE = /^(?:please\b|(?:can|could|would)\s+you\b)/i;
const UNFINISHED_RE = /\b(?:todo|later|should|bug|fix)\b/i;
const MARKER_RE = /\b(?:please\b|(?:can|could|would)\s+you\b|i want|we should|todo|let['’]s|lets)\b/gi;

const SECRET_RES: RegExp[] = [
  /(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{10,}/gi,
  /(?<![A-Za-z0-9])ghp_[A-Za-z0-9]{10,}/g,
  /(?<![A-Za-z0-9])sk_live_[A-Za-z0-9]{8,}/gi,
  /(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{8,}/gi,
  /(?<![A-Za-z0-9])Bearer\s+[A-Za-z0-9\-._~+/]+=*/gi,
  /(?<![A-Za-z0-9])AKIA[A-Z0-9]{16}(?![A-Za-z0-9])/g,
  /(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{10,}/gi,
  /(?<![A-Za-z0-9_])api_key\s*=\s*["']?[^\s"'&,}]+/gi,
];

function redact(input: string): string {
  let out = input;
  for (const re of SECRET_RES) {
    re.lastIndex = 0;
    out = out.replace(re, "[redacted]");
  }
  return out;
}

function isSecretPath(p: string): boolean {
  const norm = p.replace(/\\/g, "/");
  SECRET_FILE_RE.lastIndex = 0;
  return SECRET_FILE_RE.test(norm);
}

function normKey(s: string): string {
  return s
    .toLowerCase()
    .replace(/[^a-z0-9\s]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function stripPrefix(text: string): string {
  let t = text.replace(/^(?:[\s>*\-•]+|\d+[.)]\s+)+/u, "");
  t = t.replace(/^(?:hey|hi|hello|yo|ok|okay)(?:\s+[a-z0-9_-]+){0,4}\s*[,:-]+\s+/i, "");
  return t.trim();
}

function looksLikeIdea(text: string): boolean {
  const t = stripPrefix(text.replace(/\s+/g, " ").trim());
  if (!t) return false;
  if (REQUEST_RE.test(t) || PLEASE_RE.test(t)) return true;
  const first = t.split(/\s+/, 1)[0]?.toLowerCase().replace(/[^a-z']/g, "");
  return !!first && VERBS.has(first);
}

function polish(text: string): string {
  const one = text.replace(/\s+/g, " ").trim();
  const stripped = stripPrefix(one);
  return stripped.length >= MIN_CHARS ? stripped : one;
}

function clampSnippet(text: string): { text: string; end: number } {
  const one = text.replace(/\s+/g, " ").trim();
  if (one.length <= MAX_CHARS) return { text: one, end: one.length };
  let end = MAX_CHARS;
  const sp = one.lastIndexOf(" ", MAX_CHARS);
  if (sp >= MIN_CHARS) end = sp;
  return { text: one.slice(0, end).trim(), end };
}

function scoreIdea(sessionAt: string | undefined, text: string): number {
  const ts = sessionAt ? Date.parse(sessionAt) : NaN;
  const recency = Number.isFinite(ts) ? ts / 1_000_000 : 0;
  return recency + (UNFINISHED_RE.test(text) ? UNFINISHED_BOOST : 0);
}

function pushSnippet(out: string[], seen: Set<string>, raw: string) {
  const one = polish(raw);
  if (one.length < MIN_CHARS || one.length > MAX_CHARS) return;
  if (!looksLikeIdea(one)) return;
  const key = normKey(one);
  if (!key || seen.has(key)) return;
  seen.add(key);
  out.push(one);
}

function fromMarker(text: string): string | undefined {
  const m = text.match(/\b(?:please\b|(?:can|could|would)\s+you\b|i want|we should|todo|let['’]s|lets)\b/i);
  if (!m || m.index == null || m.index === 0) return undefined;
  return text.slice(m.index).trim();
}

function considerPiece(piece: string, out: string[], seen: Set<string>) {
  const collapsed = piece.replace(/\s+/g, " ").trim();
  if (!collapsed) return;
  if (collapsed.length <= MAX_CHARS) {
    if (looksLikeIdea(polish(collapsed))) pushSnippet(out, seen, collapsed);
    else {
      const sliced = fromMarker(collapsed);
      if (sliced) pushSnippet(out, seen, sliced);
    }
    return;
  }
  const head = clampSnippet(collapsed);
  const headText = polish(head.text);
  const headKept = headText.length >= MIN_CHARS && looksLikeIdea(headText);
  if (headKept) pushSnippet(out, seen, head.text);
  let cursor = headKept ? head.end : 0;
  MARKER_RE.lastIndex = 0;
  for (const m of collapsed.matchAll(MARKER_RE)) {
    const idx = m.index ?? 0;
    if (idx < cursor) continue;
    const slice = collapsed.slice(idx);
    pushSnippet(out, seen, clampSnippet(slice).text);
    cursor = idx + Math.max(MIN_CHARS, 1);
  }
}

function snippets(raw: string): string[] {
  const clean = redact(raw).replace(/\u0000/g, "").trim();
  if (!clean) return [];
  const out: string[] = [];
  const seen = new Set<string>();
  const lines = clean
    .split(/\n+/)
    .map((l) => l.trim())
    .filter(Boolean);
  for (const line of lines.length ? lines : [clean]) {
    const collapsed = line.replace(/\s+/g, " ").trim();
    const sentences = collapsed
      .split(/(?<=[.!?])\s+/)
      .map((s) => s.trim())
      .filter(Boolean);
    if (sentences.length > 1) {
      for (const sentence of sentences) considerPiece(sentence, out, seen);
    } else {
      considerPiece(collapsed, out, seen);
    }
  }
  return out;
}

function textFromContent(content: unknown): string {
  if (typeof content === "string") return content;
  if (content && typeof content === "object" && !Array.isArray(content)) {
    const block = content as Record<string, unknown>;
    if (block.type === "text" && typeof block.text === "string") return block.text;
    return "";
  }
  if (!Array.isArray(content)) return "";
  const bits: string[] = [];
  for (const block of content) {
    if (typeof block === "string") {
      if (block.trim()) bits.push(block);
      continue;
    }
    if (!block || typeof block !== "object") continue;
    const b = block as Record<string, unknown>;
    if (b.type === "text" && typeof b.text === "string" && b.text.trim()) bits.push(b.text);
  }
  return bits.join("\n");
}

function isAssistant(obj: Record<string, unknown>): boolean {
  if (obj.type === "assistant" || obj.role === "assistant") return true;
  const msg = obj.message;
  return !!msg && typeof msg === "object" && !Array.isArray(msg) && (msg as Record<string, unknown>).role === "assistant";
}

function extractUserText(obj: Record<string, unknown>): string {
  if (isAssistant(obj)) return "";
  const msg = obj.message;
  const msgObj = msg && typeof msg === "object" && !Array.isArray(msg) ? (msg as Record<string, unknown>) : undefined;
  const role = typeof obj.role === "string" ? obj.role : undefined;
  const msgRole = typeof msgObj?.role === "string" ? msgObj.role : undefined;
  const type = typeof obj.type === "string" ? obj.type : undefined;
  const isUser = type === "user" || role === "user" || msgRole === "user";
  if (!isUser) return "";
  const fromMessage = msgObj ? textFromContent(msgObj.content) : "";
  if (fromMessage.trim()) return fromMessage;
  return textFromContent(obj.content);
}

function readSessionAt(value: unknown): string | undefined {
  if (typeof value === "number" && Number.isFinite(value)) return new Date(value).toISOString();
  if (typeof value === "string" && value.trim()) return value.trim();
  return undefined;
}

function readCwd(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  const cwd = value.trim();
  return cwd ? redact(cwd) : undefined;
}

function ideasFromLine(line: string, source: string): Idea[] {
  const trimmed = line.replace(/^\uFEFF/, "").trim();
  if (!trimmed) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    return [];
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return [];
  const obj = parsed as Record<string, unknown>;
  const text = extractUserText(obj);
  if (!text.trim()) return [];
  const sessionAt = readSessionAt(obj.timestamp);
  const projectPath = readCwd(obj.cwd);
  return snippets(text).map((snippet) => ({
    id: newId("idea"),
    text: snippet,
    summary: truncate(snippet, 200),
    source,
    projectPath,
    sessionAt,
    score: scoreIdea(sessionAt, snippet),
    status: "new" as const,
  }));
}

function better(a: Idea, b: Idea): boolean {
  if (a.score !== b.score) return a.score > b.score;
  return (a.sessionAt ?? "") > (b.sessionAt ?? "");
}

function keep(map: Map<string, Idea>, idea: Idea) {
  const key = normKey(idea.text);
  if (!key) return;
  const prev = map.get(key);
  if (!prev || better(idea, prev)) map.set(key, idea);
}

function toSource(root: string, file: string): string {
  const rel = relative(root, file);
  if (!rel || rel.startsWith("..")) return redact(file);
  return redact(rel.split(sep).join("/"));
}

async function collectJsonl(root: string): Promise<string[]> {
  const files: string[] = [];
  const stack = [root];
  while (stack.length) {
    const dir = stack.pop();
    if (!dir) continue;
    let entries;
    try {
      entries = await readdir(dir, { withFileTypes: true });
    } catch {
      continue;
    }
    for (const ent of entries) {
      const full = `${dir}${sep}${ent.name}`;
      if (isSecretPath(full) || isSecretPath(ent.name)) continue;
      if (ent.isSymbolicLink()) continue;
      if (ent.isDirectory()) stack.push(full);
      else if (ent.isFile() && ent.name.toLowerCase().endsWith(".jsonl")) files.push(full);
    }
  }
  files.sort((a, b) => a.localeCompare(b));
  return files;
}

async function scanFile(file: string, root: string, into: Map<string, Idea>): Promise<void> {
  const source = toSource(root, file);
  const stream = createReadStream(file, { encoding: "utf8" });
  const rl = createInterface({ input: stream, crlfDelay: Infinity });
  stream.on("error", () => {
    rl.close();
  });
  try {
    for await (const line of rl) {
      for (const idea of ideasFromLine(line, source)) keep(into, idea);
    }
  } catch {
    /* unreadable transcript — skip */
  }
}

export async function mineIdeas(root: string, limit?: number): Promise<Idea[]> {
  const cap = limit == null || !Number.isFinite(limit) ? DEFAULT_LIMIT : Math.max(0, Math.floor(limit));
  if (cap === 0) return [];
  const files = await collectJsonl(root);
  const found = new Map<string, Idea>();
  for (const file of files) await scanFile(file, root, found);
  return [...found.values()]
    .sort((a, b) => b.score - a.score || (b.sessionAt ?? "").localeCompare(a.sessionAt ?? "") || a.text.localeCompare(b.text))
    .slice(0, cap);
}
