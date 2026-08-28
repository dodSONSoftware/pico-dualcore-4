# /git-commit: Analyze and Commit Workflow (MicroPython/Pico W)

Analyze all changes in the repository, create a formatted commit message, stage everything, and commit. All commands execute from the project root.

This version is tailored for MicroPython firmware projects (Raspberry Pi Pico W) which store version in `version.py`.

## Git Command Guard

**ONLY the following git subcommands are permitted within this workflow:**
- `git status`
- `git diff`
- `git log`
- `git add`
- `git commit`

**Prohibited git subcommands (do not use):**
- `git push`, `git pull`, `git fetch`
- `git branch`, `git checkout`, `git merge`, `git rebase`
- `git rm`, `git mv`, `git stash`
- `git reset`, `git reset --hard`, `git revert`
- `git restore`, `git clean`
- `git remote`, `git tag`
- Any other git subcommand not explicitly listed above

If changes require push, branch switching, or other git operations, stop and request user guidance.

**Preserve working tree on failure:** On any failure, do not automatically run `git reset`, `git revert`, `git restore`, or `git checkout`. Report the failure and leave the working tree intact.

## Steps

### 0. PRE-FLIGHT CHECKS

Run these before making any changes:
- `git status --short` — inspect working tree for uncommitted changes
- `grep "^FIRMWARE_VERSION = " version.py` — get current version from version.py
- Extract version value for reference

Uncommitted changes should not cause an automatic abort. The workflow's purpose is to inspect, stage, and commit changes.

**Current dirty tree is valuable:** The archive may contain recovered uncommitted work across multiple production/test/workflow files. Do not treat a dirty tree as disposable. Before any broad automated change, inspect status, inspect diff, preserve current work, and never "clean up" by reset/revert unless explicitly directed by the human.

If version.py is missing, fail with error message.

### 1. UPDATE CLAUDE.md IF NEEDED (README.md NOT ASSUMED)

Check if documentation needs updating based on changes:

**For CLAUDE.md:**
- **New files/directories** — add entries to directory tree
- **Modified commands/skills** — update references or descriptions
- **Removed files** — remove stale entries
- **Architecture/config changes** — update architecture notes or commands

**For README.md:** Only if it exists (check first). Do not assume it exists.
- **New features** — add feature highlights or usage examples
- **API changes** — update endpoint tables or request/response examples
- **Configuration changes** — update config examples or environment variables
- **Dependency updates** — note major version changes
- **Breaking changes** — add migration notes or deprecation warnings

Skip if no structural or documentation-relevant changes. Do NOT stage documentation files yet.

### 2. ANALYZE GIT CHANGES

Run these commands to understand what changed:
- `git status --short` — list all modified/added/deleted files
- `git diff --stat HEAD` — show file-level change summary
- `git diff HEAD` — show full diff for review

**Git status --short format:** The output uses two status columns (XY):
- First column (X): Index status (staged changes)
  - `M` = Modified in index
  - `A` = Added to index
  - `D` = Deleted from index
  - `R` = Renamed in index
  - `C` = Copied to index
  - ` ` = No index change
- Second column (Y): Working tree status (unstaged changes)
  - `M` = Modified in working tree
  - `D` = Deleted in working tree
  - ` ` = No working tree change

Examples:
- ` M` = Staged, unstaged changes
- `AM` = Added to index, modified in working tree
- `??` = Untracked file

### 3. DETERMINE VERSION BUMP FROM CURRENT CHANGES

**DO NOT use recent commit history to determine version bump.** The current diff, changed files, and intended commit determine the version impact.

Analyze the current changes to classify the commit type:
- **Breaking change**: Any change with `BREAKING CHANGE:` in commit message OR explicit API/protocol breaking changes in the diff
- **New features** (`feat:`): New functionality added (new files, new APIs, new configuration options)
- **Bug fixes** (`fix:`): Correcting broken behavior
- **Other** (`chore:`, `docs:`, `refactor:`, `test:`, `perf:`, `ci:`): Minor changes that don't affect API

**Version format support:**
- Base version: `X.Y.Z` (semantic versioning)
- Prerelease: `.beta-N` format is explicitly supported and preserved
- If current version is `0.3.1.beta-1`, preserve the `.beta-1` suffix unless the commit explicitly requires a release version change

**Update `FIRMWARE_VERSION` in `version.py`:**
- Patch: increment third number, reset lower numbers
- Minor: increment second number, reset third to 0
- Major: increment first number, reset others to 0
- Prerelease: preserve `.beta-N` suffix if present

**Version replacement must verify exactly one assignment:**
```python
import re
with open("version.py", "r") as f:
    content = f.read()

# Use anchored expression with count verification
updated, count = re.subn(
    r'^FIRMWARE_VERSION\s*=\s*"[^"]*"\s*$',
    'FIRMWARE_VERSION = "{}"'.format(new_version),
    content,
    count=1,
    flags=re.MULTILINE
)

if count != 1:
    raise RuntimeError(f"Expected exactly one FIRMWARE_VERSION assignment, found {count}")

# Verify the replacement worked
with open("version.py", "r") as f:
    content = f.read()
    if f'FIRMWARE_VERSION = "{new_version}"' not in content:
        raise RuntimeError("Version verification failed")

with open("version.py", "w") as f:
    f.write(content)
```

### 4. GENERATE COMMIT MESSAGE

Format the commit message:

```
[X.Y.Z] <type>: <overview>

- <change 1>
- <change 2>
```

Rules:
- Version in square brackets on first line (e.g., `[1.0.1]`)
- Conventional commit type (`feat:`, `fix:`, `chore:`, `refactor:`, `docs:`, `test:`, `perf:`, `ci:`, `build:`, `style:`)
- Overview is brief summary
- One-line descriptions per file/group of changes
- If breaking change, add `BREAKING CHANGE:` footer with migration notes

### 5. PRE-COMMIT TEST VALIDATION

**Required before staging/committing:**

Run these tests to ensure the working tree is in a valid state:

```bash
# Compile all Python files (catch syntax errors)
python3 -m compileall -q .

# Run pytest suite
pytest -q

# Run direct test runners if project uses them
python3 test_queue_drainer.py
python3 test_regression.py
python3 test_device_manager.py
python3 test_system_worker.py
```

**Do not commit when tests fail.** If any test fails, report:
- Test command that failed
- Failure output
- Current git status

Stop execution and require human review. Do not attempt to force a commit.

### 6. STAGE ALL CHANGES

Stage all changes:
```bash
git add .
```

**Review staged diff after staging:**
```bash
git status --short
git diff --cached --stat
git diff --cached
```

Verify the staged set matches the intended commit. If not, report:
- What is staged
- What was expected
- Stopped execution

Do not automatically unstage/revert if it does not match. Report and stop.

### 7. CREATE COMMIT

```bash
git commit -m "$(cat <<'EOF'
[X.Y.Z] <type>: <overview>

- <change 1>
- <change 2>
EOF
)"
```

### 8. POST-COMMIT VERIFICATION

Run:
- `git status` — confirm working tree is clean
- `git log --oneline -3` — confirm commit landed correctly
- `grep "^FIRMWARE_VERSION = " version.py` — verify version in committed commit

## Error Handling

On any failure, do the following:

1. **Report the failure clearly:**
   - Command that failed
   - Exit code (if applicable)
   - Error output

2. **Report current git state:**
   - `git status --short`
   - List of modified files
   - List of staged files

3. **Stop execution.** Do not attempt automatic rollback using:
   - `git reset`
   - `git revert`
   - `git restore`
   - `git checkout`
   - `git clean`
   - `git stash`

**Preserve the working tree.** The uncommitted changes may be valuable recovered work.

## Output

After successful commit, return:

1. **Full commit message** used
2. **Files changed** with descriptions:
   ```
   - <file_path>: <description>
   ```
3. **Documentation updates**: CLAUDE.md (if applicable). README.md only if it exists and was updated.
4. **Version bump**: old → new
5. **Summary** of notable changes

## EXAMPLE OUTPUT

```
[1.0.1] fix: update network configuration

- config.json: Update WiFi SSID, timeserver IP, MQTT broker IP
- system_info.py: Update firmware version to 1.0.1

Notable changes: Network configuration updated for new environment.
```

## MICROPYTHON-SPECIFIC NOTES

- No `package.json` — version stored in `version.py`
- No submodules — all code in single repository
- No npm/yarn dependencies to update
- **Deployment**: Use the project's MicroPython upload tooling (mpremote, VS Code/vREPL, or the currently documented deployment path). BOOTSEL/UF2 mass-storage mode is for firmware image installation, not ordinary filesystem synchronization.
