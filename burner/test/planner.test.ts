import { describe, expect, it } from "vitest";
import { planTasks } from "../src/planner.js";
import type { Idea, ProjectInfo, TaskKind } from "../src/types.js";

const SAFETY =
  "work only in this working directory; never git push; never read .env, keys, or credentials; finish with a short summary of files changed.";

function project(over: Partial<ProjectInfo> = {}): ProjectInfo {
  return {
    id: "widget-1",
    name: "widget",
    path: "/repos/widget",
    languages: ["typescript"],
    lastCommitAt: "2026-01-01T00:00:00.000Z",
    dirty: false,
    hasTests: false,
    todoCount: 4,
    packageManager: "npm",
    testCommand: "npm test",
    readmeExcerpt: "tiny",
    score: 50,
    ...over,
  };
}

function idea(over: Partial<Idea> = {}): Idea {
  return {
    id: "idea-1",
    text: "Add a keyboard shortcut for search",
    source: "transcripts/abc.jsonl",
    score: 3,
    status: "new",
    ...over,
  };
}

const KINDS: TaskKind[] = ["tests", "todos", "docs", "review", "custom"];

describe("planTasks", () => {
  it("emits tests, todos, and docs with priorities and token estimates", () => {
    const now = new Date("2026-10-07T15:00:00.000Z");
    const tasks = planTasks([project()], [], now);

    expect(tasks.map((t) => t.kind)).toEqual(["tests", "todos", "docs"]);
    expect(tasks.map((t) => t.priority)).toEqual([70, 65, 50]);
    expect(tasks.map((t) => t.estTokens)).toEqual([12_000, 8_000, 6_000]);
    expect(tasks.map((t) => t.title)).toEqual([
      "Add tests for widget",
      "Resolve 4 TODOs in widget",
      "Write README for widget",
    ]);

    for (const task of tasks) {
      expect(task.id.startsWith("task")).toBe(true);
      expect(task.id.length).toBeGreaterThan("task".length);
      expect(task.projectId).toBe("widget-1");
      expect(task.createdAt).toBe("2026-10-07T15:00:00.000Z");
      expect(task.context).toEqual([]);
      expect(task.lane).toBeUndefined();
      expect(task.prompt.startsWith(SAFETY)).toBe(true);
      expect(task.title.length).toBeLessThan(80);
    }

    expect(tasks[0]?.prompt).toContain("typescript");
    expect(tasks[0]?.prompt).toContain("npm test");
    expect(tasks[2]?.prompt).toContain("tiny");
    expect(new Set(tasks.map((t) => t.id)).size).toBe(3);
  });

  it("skips kinds the project does not need", () => {
    const ready = project({
      hasTests: true,
      languages: ["typescript"],
      todoCount: 0,
      readmeExcerpt: "x".repeat(80),
    });
    const noLang = project({
      id: "bare",
      name: "bare",
      path: "/repos/bare",
      hasTests: false,
      languages: [],
      todoCount: 0,
      readmeExcerpt: "x".repeat(80),
      score: 1,
    });

    expect(planTasks([ready, noLang], [])).toEqual([]);
  });

  it("still plans tests when there is no test command, and docs when the readme is empty", () => {
    const tasks = planTasks(
      [
        project({
          hasTests: false,
          languages: ["python"],
          testCommand: null,
          todoCount: 0,
          readmeExcerpt: "",
          score: 10,
        }),
      ],
      [],
    );

    expect(tasks.map((t) => t.kind)).toEqual(["tests", "docs"]);
    expect(tasks[0]?.prompt).toContain("python");
    expect(tasks[0]?.prompt).toContain("Add a test command");
    expect(tasks[1]?.prompt).toContain("no README");
    expect(tasks[1]?.priority).toBe(10);
  });

  it("turns matching new ideas into higher-priority custom tasks", () => {
    const byPath = idea({
      id: "by-path",
      text: "Cache the session list",
      projectPath: "/repos/widget",
    });
    const byName = idea({
      id: "by-name",
      text: "Please polish Widget empty states",
      projectPath: "/somewhere/else",
    });
    const planned = idea({
      id: "planned",
      text: "widget should wait",
      projectPath: "/repos/widget",
      status: "planned",
    });
    const done = idea({
      id: "done",
      text: "widget already shipped",
      status: "done",
    });
    const stranger = idea({
      id: "other",
      text: "Rewrite the billing service",
      projectPath: "/repos/billing",
    });

    const tasks = planTasks([project({ todoCount: 0, hasTests: true, readmeExcerpt: "x".repeat(90) })], [
      byPath,
      byName,
      planned,
      done,
      stranger,
    ]);

    expect(tasks.map((t) => t.kind)).toEqual(["custom", "custom"]);
    expect(tasks.every((t) => t.priority === 80)).toBe(true);
    expect(tasks.every((t) => t.estTokens === 10_000)).toBe(true);
    expect(tasks[0]?.prompt).toContain("Cache the session list");
    expect(tasks[1]?.prompt).toContain("Please polish Widget empty states");
    expect(tasks.map((t) => t.title)).toEqual(["Cache the session list", "Please polish Widget empty states"]);
  });

  it("matches an idea to every project it belongs to", () => {
    const shared = idea({
      text: "Port the widget picker into ledger",
      projectPath: "/repos/widget",
    });
    const widget = project({ id: "w", name: "widget", path: "/repos/widget", score: 10, hasTests: true, todoCount: 0, readmeExcerpt: "x".repeat(80) });
    const ledger = project({
      id: "l",
      name: "ledger",
      path: "/repos/ledger",
      score: 40,
      hasTests: true,
      todoCount: 0,
      readmeExcerpt: "x".repeat(80),
    });

    const tasks = planTasks([widget, ledger], [shared]);
    expect(tasks.map((t) => t.projectId)).toEqual(["l", "w"]);
    expect(tasks.map((t) => t.priority)).toEqual([70, 40]);
    expect(tasks.every((t) => t.prompt.includes("Port the widget picker into ledger"))).toBe(true);
  });

  it("strips sk- and ghp_ secrets from idea text", () => {
    const tasks = planTasks(
      [project({ hasTests: true, todoCount: 0, readmeExcerpt: "x".repeat(80) })],
      [
        idea({
          projectPath: "/repos/widget",
          text: "Ship dark mode. Key sk-testsecret123 and token ghp_abcdefghijklmnopqrstuvwxyz123456 stay out.",
        }),
      ],
    );

    expect(tasks).toHaveLength(1);
    const task = tasks[0]!;
    expect(task.prompt).toContain("Ship dark mode");
    expect(task.prompt).toContain("[redacted]");
    expect(task.prompt).not.toContain("sk-");
    expect(task.prompt).not.toContain("ghp_");
    expect(task.title).not.toContain("sk-");
    expect(task.title).not.toContain("ghp_");
    expect(task.title.length).toBeLessThan(80);
  });

  it("caps each project at 8 tasks, keeping the highest priorities", () => {
    const ideas = Array.from({ length: 9 }, (_, i) =>
      idea({ id: `idea-${i}`, text: `widget follow-up ${i}`, projectPath: "/repos/widget" }),
    );
    const tasks = planTasks([project({ score: 5 })], ideas);

    expect(tasks).toHaveLength(8);
    expect(tasks.every((t) => t.kind === "custom")).toBe(true);
    expect(tasks.every((t) => t.priority === 35)).toBe(true);
    expect(tasks.map((t) => t.title)).toEqual(ideas.slice(0, 8).map((item) => item.text));
  });

  it("sorts every project together by priority descending", () => {
    const low = project({
      id: "low",
      name: "low",
      path: "/repos/low",
      score: 10,
      hasTests: false,
      todoCount: 2,
      readmeExcerpt: "short",
    });
    const high = project({
      id: "high",
      name: "high",
      path: "/repos/high",
      score: 100,
      hasTests: true,
      todoCount: 0,
      readmeExcerpt: "",
    });

    const tasks = planTasks([low, high], [idea({ text: "Retheme low", projectPath: "/repos/low" })]);

    expect(tasks.map((t) => [t.projectId, t.kind, t.priority])).toEqual([
      ["high", "docs", 100],
      ["low", "custom", 40],
      ["low", "tests", 30],
      ["low", "todos", 25],
      ["low", "docs", 10],
    ]);
  });

  it("keeps equal priorities in input order and stamps a fresh timestamp when now is omitted", () => {
    const before = Date.now();
    const a = project({ id: "a", name: "alpha", path: "/repos/alpha", score: 7, hasTests: true, todoCount: 0, readmeExcerpt: "" });
    const b = project({ id: "b", name: "beta", path: "/repos/beta", score: 7, hasTests: true, todoCount: 0, readmeExcerpt: "" });
    const tasks = planTasks([a, b], []);
    const after = Date.now();

    expect(tasks.map((t) => t.projectId)).toEqual(["a", "b"]);
    expect(tasks.every((t) => t.priority === 7 && t.kind === "docs")).toBe(true);
    for (const task of tasks) {
      const stamped = Date.parse(task.createdAt);
      expect(stamped).toBeGreaterThanOrEqual(before);
      expect(stamped).toBeLessThanOrEqual(after);
    }
  });

  it("returns no tasks for empty input", () => {
    expect(planTasks([], [])).toEqual([]);
    expect(planTasks([], [idea()])).toEqual([]);
  });

  it("plans a code review when the worktree is dirty", () => {
    const tasks = planTasks(
      [project({ dirty: true, hasTests: true, todoCount: 0, readmeExcerpt: "x".repeat(90) })],
      [],
    );
    expect(tasks).toHaveLength(1);
    expect(tasks[0]?.kind).toBe("review");
    expect(tasks[0]?.title).toBe("Review uncommitted changes in widget");
    expect(tasks[0]?.estTokens).toBe(9_000);
    expect(tasks[0]?.priority).toBe(75);
    expect(tasks[0]?.prompt).toContain("code review plan");
    expect(tasks[0]?.prompt).toContain("secret");
    expect(tasks[0]?.prompt.startsWith(SAFETY)).toBe(true);
  });

  it("does not invent kinds outside the planner", () => {
    const tasks = planTasks([project()], [idea({ projectPath: "/repos/widget", text: "widget tweak" })]);
    expect(tasks.every((t) => KINDS.includes(t.kind))).toBe(true);
  });
});
