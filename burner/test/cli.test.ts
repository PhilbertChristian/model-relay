import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { parseArgs, seedDemoRepo } from "../src/cli.js";

const dirs: string[] = [];

afterEach(() => {
  for (const dir of dirs.splice(0)) rmSync(dir, { recursive: true, force: true });
});

describe("parseArgs", () => {
  it("reads the command from argv[2]", () => {
    expect(parseArgs(["node", "src/cli.ts", "demo"])).toEqual({ cmd: "demo" });
    expect(parseArgs(["node", "src/cli.ts", "run"])).toEqual({ cmd: "run" });
    expect(parseArgs(["node", "src/cli.ts", "discover"])).toEqual({ cmd: "discover" });
    expect(parseArgs(["node", "src/cli.ts", "ideas"])).toEqual({ cmd: "ideas" });
    expect(parseArgs(["node", "src/cli.ts", "search"])).toEqual({ cmd: "search" });
    expect(parseArgs(["node", "src/cli.ts", "review"])).toEqual({ cmd: "review" });
    expect(parseArgs(["node", "src/cli.ts"])).toEqual({ cmd: "" });
    expect(parseArgs(["node", "demo"])).toEqual({ cmd: "" });
  });
});

describe("seedDemoRepo", () => {
  it("commits a tiny repo without writing git user.name", async () => {
    const parent = mkdtempSync(join(tmpdir(), "burner-cli-"));
    dirs.push(parent);
    const repo = await seedDemoRepo(parent);

    expect(repo).toBe(join(parent, "demo-repo"));

    const log = spawnSync("git", ["log", "-1", "--format=%an%n%s"], { cwd: repo, encoding: "utf8" });
    expect(log.status).toBe(0);
    expect(log.stdout.split("\n")[0]).toBe("BurnerAgent");
    expect(log.stdout).toContain("README");

    const files = spawnSync("git", ["ls-files"], { cwd: repo, encoding: "utf8" });
    expect(files.stdout).toContain("README.md");

    const localName = spawnSync("git", ["config", "--local", "--get", "user.name"], {
      cwd: repo,
      encoding: "utf8",
    });
    expect(localName.status).not.toBe(0);
  });
});
