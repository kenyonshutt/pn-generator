# pn-service

A local PyQt6 desktop application for issuing sequential part numbers across multiple projects. Each project maintains its own part number namespace in a dedicated folder within its own repository. The service repo contains only application code and local configuration — no data.

---

## Concepts

### Service repo
The repository containing this codebase. Cloned once per machine. Contains the PyQt6 application, generator logic, and two gitignored local config files (`projects.csv`, `user.json`). No part number data lives here.

### Project repo
Any git repository that has opted in to pn-service by containing a `part_numbers/` folder. The service reads and writes this folder. The project repo can contain anything else — hardware files, firmware, documentation — pn-service only touches `part_numbers/`.

### Part numbers folder
A folder named `part_numbers/` at the root of a project repo containing exactly two files:
- `pn_log.csv` — the append-only log of every issued part number
- `next_up.json` — bookkeeping file storing the next sequence number

### Part number format
Defined per-project in `setup.json` via the `format` field. The format string is a literal descriptor where each character either maps to a typed segment or is emitted as-is into the output PN.

Character types:
- `P` — prefix character; one or more `P` characters are replaced by the `prefix` value from `setup.json` as a single token
- `B` — one digit of the zero-padded base sequence number; repeat N times for N digits
- `T` — part type letter; single character, selected by the user
- `S` — one digit of the zero-padded sub sequence number; repeat N times for N digits
- Any other character — emitted literally into the output (e.g. `-`, `_`)

Example: `PBBBBBB-TSS` → `P000001-C01`

### Cache

Each user maintains a cache file at `part_numbers/<username>_cache.json` inside the **project repo** (not the service repo). This file is committed — all team members can see who has what reserved. Because each user's cache is a separate file named after them, there are no merge conflicts between users.

The cache holds a pre-reserved block of `cache_size` base sequence numbers that have already been committed to `next_up.json` in the project repo.

The cache acts as a lookahead buffer: base numbers are reserved in advance so that a network failure during a normal issue operation does not block PN generation. Every individual PN issue still triggers its own git commit and push to `pn_log.csv`. Only cache refill operations touch `next_up.json`.

**Refill trigger**: on startup, and on project selection, if the user's cache file is empty or missing, the app immediately reserves the next `cache_size` base numbers by incrementing `next_up.json` by `cache_size` and pushing. The reserved numbers are stored in `<username>_cache.json` and committed.

**Normal issue (Option A)**: consume the next number from `<username>_cache.json`. No `next_up.json` write occurs. Commit and push `pn_log.csv` and the updated cache file together.

**Cache exhausted mid-session**: if the cache empties during a session, refill immediately before issuing the next PN.

**Network outage during refill**: if the push fails, neither the cache file nor `next_up.json` is updated. The app surfaces the error and disables issuing until connectivity is restored and the refill succeeds.

**Network outage during normal issue**: the PN is issued from the local cache and written to `pn_log.csv` locally. The git push failure is surfaced in the UI; the local commit is left in place for the user to push manually when connectivity returns.

**Option A — New base PN**: allocates a new `YYYYYY` from `next_up.json`, appends a row with `JJ = 01`, and increments the counter.

**Option B — Sub PN**: the user supplies an existing base PN root (e.g. `P-000001`), selects a part type, and the tool derives `JJ` by counting existing rows in `pn_log.csv` whose `pn` column starts with that root string, then issuing `count + 1`. `next_up.json` is not modified for sub PNs.

---

## Repository structure

```
pn-service/                         ← this repo
├── ui.py                           ← PyQt6 window and widgets
├── generate.py                     ← PN logic: issue, CSV append, git ops
├── pyproject.toml                  ← Python project metadata and dependencies
├── projects.csv                    ← project registry (gitignored)
├── user.json                       ← local user identity (gitignored)
└── README.md

project-repo/                       ← example project repo (separate)
└── part_numbers/
    ├── setup.json                  ← project PN configuration (committed)
    ├── pn_log.csv
    ├── next_up.json
    └── <username>_cache.json       ← per-user reserved PN cache (committed)
```

---

## Gitignored files in service repo

These files and folders are machine-local and never committed. Each developer on the service maintains their own copies.

### `projects.csv`

Registry of known project repos on this machine. Edited manually. Two columns:

```
path,accent
/Users/kshutt/projects/rocket/part_numbers,#E8593C
/Users/kshutt/projects/avionics/part_numbers,#534AB7
/Users/kshutt/projects/widget/part_numbers,#3B8BD4
```

- `path` — absolute path to the `part_numbers/` folder of a project repo
- `accent` — hex color string; the entire PyQt6 window repaints to this color when this project is selected

All PN configuration (prefix, segments, separator, part types, cache size) lives in `setup.json` inside the project repo — see Data files in project repos.

### `cache/<project_name>.json`

One file per project, created automatically on first use. Stores the list of pre-reserved base sequence numbers not yet consumed. Named after the project repo's directory name (derived from `path`).

```json
{
  "reserved": [3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
}
```

Numbers are consumed from the front of the list (index 0) on each Option A issue. When the list is empty the cache is exhausted and a refill is triggered. This file is gitignored in the service repo and never committed.

### `user.json`

Local user identity. Pre-fills the `who` field on issued part numbers.

```json
{
  "who": "kenyon"
}
```

---

## Committed files in service repo

### `pyproject.toml`

Python project metadata and dependencies. Used to install the environment with `pip install -e .` or any PEP 517-compatible tool.

```toml
[project]
name = "pn-service"
version = "0.1.0"
dependencies = ["PyQt6"]

[build-system]
requires = ["setuptools"]
build-backend = "setuptools.backends.legacy:build"
```

---

## Data files in project repos

### `setup.json`

Project PN configuration. Committed to the project repo — shared across all users and machines. Read by the service on project selection.

```json
{
  "prefix": "P",
  "format": "PBBBBBB-TSS",
  "cache_size": 10,
  "source_of_truth": ["git", "cad"],
  "part_types": [
    ["C", "Piece part"],
    ["H", "Harness"],
    ["A", "Assembly"],
    ["S", "System"],
    ["B", "PCB"]
  ]
}
```

- `prefix` — namespace/authority string substituted for the `P` token(s) in the format string
- `format` — literal descriptor string defining the full PN structure (see Part number format in Concepts)
- `cache_size` — integer; number of base part numbers to pre-reserve in the local cache per service instance
- `source_of_truth` — array of valid string values; the UI generates one radio button per entry. The first entry is the default selection on project load. Expected values are `"git"` and `"cad"`. A single-element array means only one option is shown. Stored as a column in `pn_log.csv` per issued PN but not encoded into the PN string itself.
- `part_types` — ordered array of `[letter, label]` pairs defining the part type dropdown. Order is preserved in the UI. Each project can define its own subset and ordering.

#### Format string examples

| `prefix` | `format` | Example output |
|----------|----------|----------------|
| `P` | `PBBBBBB-TSS` | `P000001-C01` |
| `HW` | `P-BBBBBB-T-SS` | `HW-000001-C-01` |
| `K` | `PTBBBBBB` | `KC000001` |

### `pn_log.csv`

Append-only. One row per issued part number. Never modified except by appending.

```
pn,who,timestamp,source_of_truth
P-000001-C-01,kenyon,2025-03-18T21:00:00Z,git
P-000001-H-02,kenyon,2025-03-18T21:05:00Z,cad
P-000002-A-01,alice,2025-03-19T09:14:32Z,git
```

Columns:
- `pn` — the full issued part number string
- `who` — value from `user.json` at time of issue
- `timestamp` — UTC ISO 8601 string, generated automatically
- `source_of_truth` — value of the selected radio button at time of issue; one of the strings defined in `setup.json → source_of_truth`

Sub PNs sharing the same base (`P-000001-C-01`, `P-000001-H-02`) are identified by scanning rows whose `pn` starts with the base root string. This is how `S` is derived for Option B — no separate counter is maintained.

### `next_up.json`

Tracks the next **unreserved** base sequence number — i.e. the next number that has not yet been allocated to any user's cache. Only updated during cache refill operations, not on every individual PN issue.

```json
{
  "next": 13
}
```

If kenyon's cache holds `[3, 4, 5, ..., 12]` and `next_up.json` reads `13`, that means numbers 1–2 have been issued and committed, 3–12 are reserved by kenyon, and 13 is the next number any user would receive on their next refill.

### `<username>_cache.json`

One file per user, committed to the project repo alongside all other data files. Named after the `who` value in `user.json` (e.g. `kenyon_cache.json`). All team members can see who has what reserved at any time. Because each user has a separate file, there are no merge conflicts between users on cache operations.

```json
{
  "reserved": [3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
}
```

Numbers are consumed from the front of the list (index 0) on each Option A issue. The updated cache file and `pn_log.csv` are committed together in a single commit per issue. When the list empties a refill is triggered.

---

## Application behavior

### Startup
1. Load `projects.csv` — populate the project dropdown with project names (derived from path), store `{path, accent}` per project in memory
2. Load `user.json` — store `who` value in memory
3. Select the first project in the dropdown
4. Load `setup.json` from the selected project's `part_numbers/` folder
5. Populate the part type dropdown from `setup.json → part_types` (array of `[letter, label]` pairs, in order)
6. Generate one radio button per entry in `setup.json → source_of_truth`; select the first entry as default
7. Apply the project's accent color to the window
8. Check `part_numbers/<username>_cache.json` in the selected project — if empty or missing, trigger a cache refill

### Project selection (dropdown change)
1. Load `setup.json` from the newly selected project's `part_numbers/` folder
2. Repaint the entire PyQt6 window with the project's accent color
3. Repopulate the part type dropdown from `setup.json → part_types`
4. Regenerate source of truth radio buttons from `setup.json → source_of_truth`; select the first entry
5. Verify `part_numbers/` path exists and contains `pn_log.csv`, `next_up.json`, and `setup.json`; show an error state if not
6. Check `part_numbers/<username>_cache.json` in this project — if empty or missing, trigger a cache refill

### Cache refill
1. Pull latest from project repo to ensure `next_up.json` is current
2. Read `next_up.json` → `next`
3. Reserve `[next, next+1, ..., next+cache_size-1]`
4. Write `next + cache_size` back to `next_up.json` (atomic via temp file + replace)
5. Write reserved list to `part_numbers/<username>_cache.json`
6. `git add part_numbers/next_up.json part_numbers/<username>_cache.json` → `git commit -m "reserve: {next}–{next+cache_size-1} ({who})"` → `git push`
7. On failure: revert both files, surface error in UI, disable issue button until resolved

### Issue part number — Option A (new base PN)
1. Pop the next number from the front of `part_numbers/<username>_cache.json` → `seq`
2. If cache is now empty, trigger a cache refill (non-blocking — issue proceeds with `seq` already in hand)
3. Get part type letter `Z` from the part type dropdown
4. Get `source_of_truth` value from the radio buttons
5. Assemble PN by walking the `format` string character by character (see implementation notes)
6. Generate timestamp: UTC now, ISO 8601
7. Append one row to `pn_log.csv`: `pn, who, timestamp, source_of_truth`
8. Write updated cache back to `<username>_cache.json`
9. `git add part_numbers/pn_log.csv part_numbers/<username>_cache.json` → `git commit -m "pn: {pn}"` → `git push`
10. Display the issued PN in the UI and copy it to the clipboard

### Issue part number — Option B (sub PN)
1. Read the base PN root from the text input (e.g. `P-000001`)
2. Get part type letter `Z` from the part type dropdown
3. Get `source_of_truth` value from the radio buttons
4. Count rows in `pn_log.csv` whose `pn` starts with the base root → `count`
5. `S = count + 1`, zero-padded to the width specified in `segments`
6. Assemble PN from the base root: append separator + `Z` + separator + zero-padded `S`
7. Generate timestamp: UTC now, ISO 8601
8. Append one row to `pn_log.csv`: `pn, who, timestamp, source_of_truth`
9. `next_up.json` and cache are NOT modified
10. `git add part_numbers/pn_log.csv` → `git commit -m "pn: {pn}"` → `git push`
11. Display and copy to clipboard

### Error handling
- Missing config files (`projects.csv`, `user.json`): show a modal on startup describing which file is missing and what it should contain
- Project path does not exist or is missing expected files: show inline error in the UI, disable the issue button
- Cache refill fails (push rejected, no remote, network outage): show error in UI, disable the issue button until a refill succeeds
- Git push fails during normal issue (CSV commit): surface the git stderr in the UI; leave the local commit in place for the user to push manually; do not block further issuing since the next base number is already cached
- CSV write fails: abort before any git operations and surface the error

---

## Initializing a new project repo

To add a new project:

1. Create the `part_numbers/` folder in the project repo
2. Create `part_numbers/setup.json` with the project's PN configuration (see setup.json above)
3. Create `part_numbers/pn_log.csv` with the header row:
   ```
   pn,who,timestamp
   ```
4. Create `part_numbers/next_up.json`:
   ```json
   {"next": 1}
   ```
5. Commit these files to the project repo
6. Add a row to `projects.csv` in the service repo:
   ```
   /absolute/path/to/project/part_numbers,#HEXCOLOR
   ```

There is no automated setup for this — it is intentionally a manual, rare process.

---

## Dependencies

```
PyQt6
```

Git operations use `subprocess` calling the system `git` binary — no additional Python packages required.

---

## Implementation notes for agentic use

- Public functions: `load_setup(part_numbers_path) -> dict`, `refill_cache(part_numbers_path, cache_path, cache_size) -> (list[int], error)`, `issue_new_base_pn(part_numbers_path, cache_path, who, setup, type_letter, source_of_truth) -> (pn, error)`, `issue_sub_pn(part_numbers_path, who, setup, base_pn_root, type_letter, source_of_truth) -> (pn, error)`. All return tuples — empty string for error on success, empty/None for data on failure.
- PN assembly walks the `format` string character by character. Maintain a current token type and run-length count. On each character: if it is `P`, `B`, `T`, or `S` and matches the current token type, increment the run; if it differs, flush the current run (resolve and emit), start a new run; if it is any other character, flush the current run then emit the character literally. Flush the final run at end of string. Resolve rules: `P` run → emit `prefix` value regardless of run length; `B` run of length N → emit `str(base_seq).zfill(N)`; `T` run → emit type letter (run length always 1); `S` run of length N → emit `str(sub_seq).zfill(N)`.
- `setup.json` is standard JSON, read on every project selection.
- `projects.csv` and `user.json` are read once at startup. Changing them requires restarting the app.
- The cache file lives at `part_numbers/<username>_cache.json` in the project repo, where `<username>` is the `who` value from `user.json`. It is committed — not gitignored.
- Cache file structure: `{"reserved": [3, 4, 5, ...]}`. Pop from index 0 on each Option A issue. Write back after each pop using atomic write (temp file + `Path.replace()`). Commit alongside `pn_log.csv` in the same commit.
- Cache refill writes both `next_up.json` and `<username>_cache.json` before committing. If the push fails, revert both files to their pre-refill state.
- Git operations run against the **project repo root** (parent of `part_numbers/`). Derive as `Path(part_numbers_path).parent`.
- All file I/O uses `pathlib.Path`. No `os.path`.
- Timestamps use `datetime.now(timezone.utc)` — never naive datetimes.
- The CSV is opened in append mode (`"a"`) with `newline=""` and written with `csv.writer`. Never rewrite the whole file.
- Sub PN derivation scans `pn_log.csv` for rows where `pn.startswith(base_pn_root)`. No separate counter maintained.
- The PyQt6 window accent is applied via `setStyleSheet` on the `QMainWindow`. Text color computed from accent lightness — light accent → dark text, dark accent → white text.
- Git operations and cache refill run in a `QThread` so the UI does not block.
