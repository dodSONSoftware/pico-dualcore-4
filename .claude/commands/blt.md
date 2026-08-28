# /build-lint-test: Build, Lint, Test Quality Gate

Run the build → lint → test quality gate in sequence. All commands execute from `code/`.

## Steps

1. **Analyze git changes** — run `git status`, `git diff --stat`, and `git log --oneline -5` to understand what changed since the last commit.

2. **Build** — run `npm run build` (from `code/`). If it fails, stop and report errors. Do not proceed.

3. **Lint** — run `npm run lint` (from `code/`). If it fails, fix lint errors with `npm run lint:fix`, then re-run `npm run lint`. Stop if fixes don't resolve issues.

4. **Test** — run `npm test` (from `code/`). If any tests fail, diagnose and fix them before proceeding.

BLT does not commit or push — that is the developer's decision.

## Output

- List of added/deleted/changed files with a one-sentence description each
- Brief summary of notable changes
