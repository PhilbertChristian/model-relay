import type { Idea, ProjectInfo, Task, TaskKind } from "./types.js";
import { newId, nowIso } from "./util.js";

const MAX_PER_PROJECT = 8;

const SAFETY =
  "work only in this working directory; never git push; never read .env, keys, or credentials; finish with a short summary of files changed.";

const EST = {
  tests: 12_000,
  todos: 8_000,
  docs: 6_000,
  custom: 10_000,
} as const;

function stripSecrets(text: string): string {
  return text.replace(/\bsk-\S*/g, "[redacted]").replace(/\bghp_\S*/g, "[redacted]");
}

function clipTitle(title: string): string {
  const compact = title.replace(/\s+/g, " ").trim();
  if (compact.length < 80) return compact;
  return `${compact.slice(0, 76)}…`;
}

function withSafety(body: string): string {
  return `${SAFETY}\n\n${stripSecrets(body).trim()}`;
}

function makeTask(
  project: ProjectInfo,
  kind: TaskKind,
  title: string,
  body: string,
  priority: number,
  estTokens: number,
  createdAt: string,
): Task {
  return {
    id: newId("task"),
    projectId: project.id,
    kind,
    title: clipTitle(stripSecrets(title)),
    prompt: withSafety(body),
    priority,
    estTokens,
    context: [],
    createdAt,
  };
}

function ideaMatches(project: ProjectInfo, idea: Idea): boolean {
  if (idea.status !== "new") return false;
  if (idea.projectPath === project.path) return true;
  const name = project.name.trim().toLowerCase();
  if (!name) return false;
  return idea.text.toLowerCase().includes(name);
}

function tasksForProject(project: ProjectInfo, ideas: Idea[], createdAt: string): Task[] {
  const tasks: Task[] = [];

  for (const idea of ideas) {
    if (!ideaMatches(project, idea)) continue;
    const text = stripSecrets(idea.text).trim();
    tasks.push(
      makeTask(project, "custom", text || "Custom idea", text || idea.text, project.score + 30, EST.custom, createdAt),
    );
  }

  if (!project.hasTests && project.languages.length > 0) {
    const langs = project.languages.join(", ");
    const cmd = project.testCommand
      ? `Use or extend the test command \`${project.testCommand}\`.`
      : "Add a test command and a small suite covering the main behavior.";
    tasks.push(
      makeTask(
        project,
        "tests",
        `Add tests for ${project.name}`,
        `Add automated tests for ${project.name}. Languages: ${langs}. ${cmd}`,
        project.score + 20,
        EST.tests,
        createdAt,
      ),
    );
  }

  if (project.todoCount > 0) {
    tasks.push(
      makeTask(
        project,
        "todos",
        `Resolve ${project.todoCount} TODOs in ${project.name}`,
        `Find and resolve about ${project.todoCount} TODO, FIXME, and HACK comments in ${project.name}. Implement the intended change or delete stale notes.`,
        project.score + 15,
        EST.todos,
        createdAt,
      ),
    );
  }

  if (project.readmeExcerpt.length < 80) {
    const excerpt = project.readmeExcerpt.trim();
    const current = excerpt ? `Current README excerpt:\n${excerpt}` : "There is no README yet.";
    tasks.push(
      makeTask(
        project,
        "docs",
        `Write README for ${project.name}`,
        `Write or expand the README for ${project.name} so a newcomer can install and run it. ${current}`,
        project.score,
        EST.docs,
        createdAt,
      ),
    );
  }

  tasks.sort((a, b) => b.priority - a.priority);
  return tasks.slice(0, MAX_PER_PROJECT);
}

export function planTasks(projects: ProjectInfo[], ideas: Idea[], now?: Date): Task[] {
  const createdAt = now ? now.toISOString() : nowIso();
  const tasks = projects.flatMap((project) => tasksForProject(project, ideas, createdAt));
  tasks.sort((a, b) => b.priority - a.priority);
  return tasks;
}
