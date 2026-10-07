import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { searchConversations } from "../src/ideas.js";

describe("searchConversations", () => {
  const dirs: string[] = [];

  afterEach(async () => {
    await Promise.all(dirs.splice(0).map((dir) => rm(dir, { recursive: true, force: true })));
  });

  it("finds user and assistant turns and redacts secrets", async () => {
    const root = await mkdtemp(join(tmpdir(), "burner-search-"));
    dirs.push(root);
    const secret = "sk-" + "live" + "secretvalue";
    await writeFile(
      join(root, "chat.jsonl"),
      [
        JSON.stringify({
          type: "user",
          message: { role: "user", content: `Add a conversation search. token ${secret}` },
          cwd: "/repos/widget",
          timestamp: "2026-10-07T12:00:00.000Z",
        }),
        JSON.stringify({
          type: "assistant",
          message: {
            role: "assistant",
            content: [{ type: "text", text: "The code review plan lists each changed file." }],
          },
          timestamp: "2026-10-07T12:01:00.000Z",
        }),
        JSON.stringify({
          type: "user",
          message: { role: "user", content: [{ type: "tool_result", content: "conversation search hidden" }] },
        }),
      ].join("\n") + "\n",
    );

    const hits = await searchConversations(root, "conversation search");
    expect(hits.map((hit) => hit.role)).toEqual(["user"]);
    expect(hits[0]?.text).toContain("conversation search");
    expect(hits[0]?.text).not.toContain(secret);
    expect(hits[0]?.text).toContain("[redacted]");
    expect(hits[0]?.source).toBe("chat.jsonl");
    expect(hits[0]?.projectPath).toBe("/repos/widget");

    const review = await searchConversations(root, "code review plan");
    expect(review.map((hit) => hit.role)).toEqual(["assistant"]);
    expect(review[0]?.text).toContain("code review plan");
  });

  it("returns nothing for a short query or a missing directory", async () => {
    expect(await searchConversations("/tmp/does-not-exist-burner-search", "plan")).toEqual([]);
    const root = await mkdtemp(join(tmpdir(), "burner-search-"));
    dirs.push(root);
    expect(await searchConversations(root, "a")).toEqual([]);
    expect(await searchConversations(root, "plan", 0)).toEqual([]);
  });
});
