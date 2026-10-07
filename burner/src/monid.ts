import type { Task } from "./types.js";

// Opt-in research notes attached before a local agent runs.
// Sends only a short query (title + prompt prefix). Never transcripts, file contents, or .env.

type MonidConfig = { apiKey?: string; baseUrl: string; enabled: boolean };

const NOTE_LIMIT = 3;
const NOTE_CHARS = 500;
const PROMPT_CHARS = 1000;

function isOn(monid: MonidConfig): monid is MonidConfig & { apiKey: string } {
  return Boolean(monid.enabled && monid.apiKey);
}

function researchUrl(baseUrl: string): string {
  return `${baseUrl.replace(/\/+$/, "")}/v1/research`;
}

function redact(text: string): string {
  return text
    .replace(/Bearer\s+\S+/gi, "[redacted]")
    .replace(/\bsk-\S*/g, "[redacted]")
    .replace(/\bghp_\S*/g, "[redacted]");
}

function isRateLimitBody(text: string): boolean {
  return /rate[\s_-]*limit|usage[\s_-]*limit|too many requests|payment required|insufficient[_\s-]*(quota|funds|balance)/i.test(
    text,
  );
}

function takeNotes(text: string): string[] | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== "object" || !("notes" in parsed) || !Array.isArray(parsed.notes)) return null;
  const out: string[] = [];
  for (const note of parsed.notes) {
    if (out.length >= NOTE_LIMIT) break;
    if (typeof note !== "string") continue;
    const cleaned = redact(note).slice(0, NOTE_CHARS);
    if (!cleaned) continue;
    out.push(cleaned);
  }
  return out;
}

function isLimited(status: number, text: string, notes: string[] | null): boolean {
  if (status === 429 || status === 402) return true;
  if (notes && notes.length > 0) return false;
  return isRateLimitBody(text);
}

function errorString(err: unknown): string {
  if (err instanceof Error) return err.message;
  return String(err);
}

async function postResearch(
  monid: MonidConfig & { apiKey: string },
  query: string,
  fetchImpl: typeof fetch,
): Promise<{ status: number; text: string }> {
  const res = await fetchImpl(researchUrl(monid.baseUrl), {
    method: "POST",
    headers: {
      Authorization: `Bearer ${monid.apiKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ query }),
  });
  return { status: res.status, text: await res.text() };
}

export async function enrichTask(task: Task, monid: MonidConfig, fetchImpl?: typeof fetch): Promise<Task> {
  if (!isOn(monid)) return task;
  const doFetch = fetchImpl ?? fetch;
  try {
    const query = redact(`${task.title}\n${task.prompt.slice(0, PROMPT_CHARS)}`);
    const { status, text } = await postResearch(monid, query, doFetch);
    const notes = status === 200 ? takeNotes(text) : null;
    if (isLimited(status, text, notes) || !notes?.length) return task;
    return { ...task, context: [...task.context, ...notes] };
  } catch {
    return task;
  }
}

export async function monidStatus(
  monid: MonidConfig,
  fetchImpl?: typeof fetch,
): Promise<{ ok: boolean; message: string }> {
  if (!isOn(monid)) return { ok: false, message: "monid disabled" };
  const doFetch = fetchImpl ?? fetch;
  try {
    // Same route as enrichment so a 429/402 is visible. Probe text is not the task.
    const { status, text } = await postResearch(monid, "status", doFetch);
    const notes = status === 200 ? takeNotes(text) : null;
    if (isLimited(status, text, notes)) return { ok: false, message: "limited" };
    if (status !== 200) return { ok: false, message: `HTTP ${status}` };
    return { ok: true, message: "ok" };
  } catch (err) {
    return { ok: false, message: errorString(err) };
  }
}
