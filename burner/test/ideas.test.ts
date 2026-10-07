import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { mineIdeas } from "../src/ideas.js";

const dirs: string[] = [];

afterEach(async () => {
  await Promise.all(dirs.map((d) => rm(d, { recursive: true, force: true })));
  dirs.length = 0;
});

async function tempRoot(): Promise<string> {
  const dir = await mkdtemp(join(tmpdir(), "burner-ideas-"));
  dirs.push(dir);
  return dir;
}

async function writeJsonl(root: string, rel: string, records: unknown[]): Promise<string> {
  const file = join(root, rel);
  await mkdir(dirname(file), { recursive: true });
  const body = records.map((r) => (typeof r === "string" ? r : JSON.stringify(r))).join("\n");
  await writeFile(file, `${body}\n`, "utf8");
  return file;
}

const SECRET_BITS = [
  "sk-testTEST123456",
  "sk_live_abcDEF123456",
  "ghp_abcdefghij123456",
  "github_pat_abcDEF1234567890",
  "ya29.secretTOKEN",
  "AKIAIOSFODNN7EXAMPLE",
  "xoxb-123456789012-abcdefghij",
  "supersecretvalue",
];

describe("mineIdeas", () => {
  it("keeps a user idea, redacts secrets, and drops assistant text", async () => {
    const root = await tempRoot();
    const secretText = `Please revoke ${SECRET_BITS[0]} ${SECRET_BITS[1]} ${SECRET_BITS[2]} ${SECRET_BITS[3]} Bearer ${SECRET_BITS[4]} ${SECRET_BITS[5]} ${SECRET_BITS[6]} api_key=${SECRET_BITS[7]} before shipping`;
    await writeJsonl(root, "projects/demo/session.jsonl", [
      {
        type: "user",
        message: {
          role: "user",
          content: [
            { type: "text", text: "Please add a retry button to the checkout page when payment fails" },
            {
              type: "tool_result",
              content: "Please build TOOL_RESULT_SHOULD_NOT_APPEAR into the product surface immediately",
            },
          ],
        },
        cwd: "/proj/shop",
        timestamp: "2026-03-15T18:04:00.000Z",
      },
      {
        type: "user",
        message: { role: "user", content: secretText },
        cwd: "/work/sk-projSECRET123456/billing",
        timestamp: "2026-03-16T18:04:00.000Z",
      },
      {
        type: "assistant",
        message: {
          role: "assistant",
          content: [
            {
              type: "text",
              text: "Please implement ASSISTANT_ONLY_PHRASE into the shipping checklist before Friday",
            },
          ],
        },
        cwd: "/proj/shop",
        timestamp: "2026-03-17T18:04:00.000Z",
      },
      "{",
      "not json",
      "",
    ]);

    const ideas = await mineIdeas(root);
    const blob = JSON.stringify(ideas);

    expect(ideas).toHaveLength(2);
    for (const bit of SECRET_BITS) expect(blob).not.toContain(bit);
    expect(blob).not.toContain("sk-projSECRET123456");
    expect(blob).not.toContain("ASSISTANT_ONLY_PHRASE");
    expect(blob).not.toContain("TOOL_RESULT_SHOULD_NOT_APPEAR");
    expect(blob).toContain("[redacted]");

    const normal = ideas.find((i) => i.text.includes("retry button"));
    expect(normal).toMatchObject({
      text: "Please add a retry button to the checkout page when payment fails",
      source: "projects/demo/session.jsonl",
      projectPath: "/proj/shop",
      sessionAt: "2026-03-15T18:04:00.000Z",
      status: "new",
    });
    expect(normal?.id.startsWith("idea")).toBe(true);
    expect(normal?.summary).toBe(normal?.text);

    const secret = ideas.find((i) => i.text.includes("revoke"));
    expect(secret?.text).toContain("[redacted]");
    expect(secret?.summary).toContain("[redacted]");
    expect(secret?.text.length).toBeGreaterThanOrEqual(24);
    expect(secret?.text.length).toBeLessThanOrEqual(400);
    expect(secret?.projectPath).toBe("/work/[redacted]/billing");
    expect(secret?.source).toBe("projects/demo/session.jsonl");
    expect(secret?.status).toBe("new");
    expect(ideas.every((i) => i.status === "new")).toBe(true);
  });

  it("reads nested transcripts and alternate user content shapes", async () => {
    const root = await tempRoot();
    await writeJsonl(root, "nested/chat.jsonl", [
      {
        type: "user",
        content: "I want a quiet mode for the dashboard notifications panel",
        cwd: "/work/app",
        timestamp: "2026-02-02T00:00:00.000Z",
      },
      {
        role: "user",
        content: [{ type: "text", text: "Let's add keyboard shortcuts to the command palette soon" }],
        timestamp: "2026-02-03T00:00:00.000Z",
      },
      {
        type: "user",
        message: { role: "user", content: "Hey Claude, please add a dark mode toggle to the settings page" },
      },
      { type: "user", message: { role: "user", content: "ok" } },
      {
        type: "user",
        message: { role: "user", content: "The weather is nice today outside the office window" },
      },
    ]);

    const ideas = await mineIdeas(root);
    const texts = ideas.map((i) => i.text);
    expect(texts.some((t) => t.includes("quiet mode"))).toBe(true);
    expect(texts.some((t) => /keyboard shortcuts/i.test(t))).toBe(true);
    expect(texts.some((t) => /dark mode toggle/i.test(t))).toBe(true);
    expect(texts.some((t) => t === "ok" || t.includes("weather"))).toBe(false);
    expect(ideas.every((i) => i.source === "nested/chat.jsonl")).toBe(true);
    const quiet = ideas.find((i) => i.text.includes("quiet mode"));
    expect(quiet?.projectPath).toBe("/work/app");
    expect(quiet?.sessionAt).toBe("2026-02-02T00:00:00.000Z");
  });

  it("ranks newer sessions higher and slightly boosts unfinished markers", async () => {
    const root = await tempRoot();
    const same = "2024-06-01T00:00:00.000Z";
    await writeJsonl(root, "rank.jsonl", [
      {
        type: "user",
        message: { role: "user", content: "Add a footer link on the about page for status" },
        timestamp: same,
      },
      {
        type: "user",
        message: { role: "user", content: "Fix the login bug on the about page later please" },
        timestamp: same,
      },
      {
        type: "user",
        message: { role: "user", content: "Add a prefix header to the exported csv report now" },
        timestamp: same,
      },
      {
        type: "user",
        message: { role: "user", content: "Add a fresh onboarding checklist for brand new accounts" },
        timestamp: "2026-01-01T00:00:00.000Z",
      },
      {
        type: "user",
        message: { role: "user", content: "Fix the ancient migration bug later in the importer" },
        timestamp: "2020-01-01T00:00:00.000Z",
      },
    ]);

    const ideas = await mineIdeas(root);
    expect(ideas[0]?.text).toContain("onboarding checklist");
    const footer = ideas.find((i) => i.text.includes("footer link"));
    const login = ideas.find((i) => i.text.includes("login bug"));
    const prefix = ideas.find((i) => i.text.includes("prefix header"));
    expect(login && footer && prefix).toBeTruthy();
    expect(login!.score).toBeGreaterThan(footer!.score);
    expect(prefix!.score).toBe(footer!.score);
    expect(ideas.findIndex((i) => i.text.includes("onboarding"))).toBeLessThan(
      ideas.findIndex((i) => i.text.includes("ancient migration")),
    );
  });

  it("dedupes normalized text and keeps the higher-scoring copy", async () => {
    const root = await tempRoot();
    await writeJsonl(root, "dup.jsonl", [
      {
        type: "user",
        message: { role: "user", content: "Please add a dark mode toggle to the settings screen" },
        timestamp: "2024-01-01T00:00:00.000Z",
        cwd: "/old",
      },
      {
        type: "user",
        message: { role: "user", content: "please   add a dark mode toggle to the settings screen!!!" },
        timestamp: "2025-01-01T00:00:00.000Z",
        cwd: "/new",
      },
    ]);

    const ideas = await mineIdeas(root);
    expect(ideas).toHaveLength(1);
    expect(ideas[0]?.projectPath).toBe("/new");
    expect(ideas[0]?.sessionAt).toBe("2025-01-01T00:00:00.000Z");
  });

  it("defaults to 40 ideas and honors an explicit limit", async () => {
    const root = await tempRoot();
    const records = Array.from({ length: 45 }, (_, n) => ({
      type: "user",
      message: { role: "user", content: `Add feature number ${String(n).padStart(2, "0")} to the settings page soon` },
      timestamp: new Date(Date.UTC(2024, 0, 1, 0, n)).toISOString(),
      cwd: "/proj/app",
    }));
    await writeJsonl(root, "many/ideas.jsonl", records);

    const all = await mineIdeas(root);
    expect(all).toHaveLength(40);
    expect(all[0]?.text).toContain("number 44");
    expect(all.some((i) => i.text.includes("number 04"))).toBe(false);
    expect(all.some((i) => i.text.includes("number 05"))).toBe(true);

    const one = await mineIdeas(root, 1);
    expect(one).toHaveLength(1);
    expect(one[0]?.text).toContain("number 44");
    expect(await mineIdeas(root, 0)).toEqual([]);
  });

  it("skips secret-like filenames and non-jsonl files", async () => {
    const root = await tempRoot();
    await writeJsonl(root, ".env.jsonl", [
      {
        type: "user",
        message: { role: "user", content: "Please add SECRET_FILE_SHOULD_NOT_APPEAR to the public dashboard now" },
      },
    ]);
    await writeJsonl(root, "keep/session.jsonl", [
      {
        type: "user",
        message: { role: "user", content: "Please add a visible empty state to the ideas list view" },
      },
    ]);
    await mkdir(join(root, "keep"), { recursive: true });
    await writeFile(
      join(root, "keep/notes.txt"),
      `${JSON.stringify({
        type: "user",
        message: { role: "user", content: "Please add NOTES_TXT_SHOULD_NOT_APPEAR to the ideas list view" },
      })}\n`,
    );

    const ideas = await mineIdeas(root);
    const blob = JSON.stringify(ideas);
    expect(blob).not.toContain("SECRET_FILE_SHOULD_NOT_APPEAR");
    expect(blob).not.toContain("NOTES_TXT_SHOULD_NOT_APPEAR");
    expect(ideas.map((i) => i.text).join("\n")).toContain("empty state");
  });

  it("returns an empty list when the root is missing", async () => {
    const root = await tempRoot();
    expect(await mineIdeas(join(root, "does-not-exist"))).toEqual([]);
  });
});
