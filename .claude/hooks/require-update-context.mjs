// PreToolUse hook (Bash): block `git push` and `gh pr create` until the
// /update-context skill has run for the current commit. The skill writes
// HEAD's sha to .git/update-context-head as its last step.
import { execSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";

let input = "";
for await (const chunk of process.stdin) input += chunk;

let command = "";
let cwd = process.cwd();
try {
  const data = JSON.parse(input);
  command = data?.tool_input?.command ?? "";
  if (data?.cwd) cwd = data.cwd;
} catch {
  process.exit(0);
}

// Only where a command starts (start of input or after ; & | ( or a newline),
// so commit messages or echo text that merely mention "git push" pass.
const pushOrPr = /(^|[;&|(\n])\s*(git\s+(?:-C\s+\S+\s+)?push|gh\s+pr\s+create)\b/;
if (!pushOrPr.test(command)) process.exit(0);

const git = (args) =>
  execSync(`git ${args}`, { cwd, encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim();

let head;
let gitDir;
try {
  head = git("rev-parse HEAD");
  gitDir = resolve(cwd, git("rev-parse --git-dir"));
} catch {
  process.exit(0); // not a git repository: nothing to guard
}

const marker = join(gitDir, "update-context-head");
const recorded = existsSync(marker) ? readFileSync(marker, "utf8").trim() : "";
if (recorded === head) process.exit(0);

process.stderr.write(
  "Blocked: run the /update-context skill before pushing or opening a PR. " +
    "It checks the docs against this branch's changes, commits any doc updates, " +
    `and records the commit in ${marker}. Current HEAD ${head.slice(0, 8)} has not been checked yet.\n`,
);
process.exit(2);
