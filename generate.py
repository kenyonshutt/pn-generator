"""
PN generation logic: assembly, CSV append, cache management, git operations.

Public functions:
  load_setup(path) -> dict
  issue_new_base_pn_local(...)  -> (pn, error)   # fast, no git
  issue_sub_pn_local(...)       -> (pn, error)   # fast, no git
  git_commit_and_push(...)      -> (bool, error)  # background git
  refill_cache(...)             -> (list[int], error)

All (result, error) returns: error is "" on success, result is empty on failure.
"""

import csv
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


def load_setup(part_numbers_path: str | Path) -> dict:
    """Read and return setup.json. Returns {} on any failure."""
    try:
        return json.loads(
            (Path(part_numbers_path) / "setup.json").read_text(encoding="utf-8")
        )
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# PN assembly
# ---------------------------------------------------------------------------


def assemble_pn(
    format_str: str, prefix: str, base_seq: int, type_letter: str, sub_seq: int
) -> str:
    """
    Walk format_str character by character, flushing typed-character runs.

    P -> prefix string (run length ignored)
    B -> base_seq zero-padded to run length
    T -> type_letter (run length always 1)
    S -> sub_seq zero-padded to run length
    other -> emitted literally
    """
    TYPED = {"P", "B", "T", "S"}
    result = []
    current: str | None = None
    run = 0

    def flush(t, n):
        if not t or not n:
            return
        if t == "P":
            result.append(prefix)
        elif t == "B":
            result.append(str(base_seq).zfill(n))
        elif t == "T":
            result.append(type_letter)
        elif t == "S":
            result.append(str(sub_seq).zfill(n))

    for ch in format_str:
        if ch in TYPED:
            if ch == current:
                run += 1
            else:
                flush(current, run)
                current, run = ch, 1
        else:
            flush(current, run)
            current, run = None, 0
            result.append(ch)

    flush(current, run)
    return "".join(result)


def _assemble_sub_pn(
    setup: dict, base_pn_root: str, type_letter: str, sub_seq: int
) -> str:
    """
    Build a sub PN by appending the T+S tail (and any literal separators between them)
    to the already-assembled base_pn_root.
    """
    fmt = setup.get("format", "")
    prefix = setup.get("prefix", "")

    last_b = max((i for i, ch in enumerate(fmt) if ch == "B"), default=-1)
    if last_b == -1:
        return assemble_pn(fmt, prefix, 0, type_letter, sub_seq)

    tail = fmt[last_b + 1 :]
    TYPED = {"T", "S"}
    result = [base_pn_root]
    current: str | None = None
    run = 0

    def flush_tail(t, n):
        if not t or not n:
            return
        if t == "T":
            result.append(type_letter)
        elif t == "S":
            result.append(str(sub_seq).zfill(n))

    for ch in tail:
        if ch in TYPED:
            if ch == current:
                run += 1
            else:
                flush_tail(current, run)
                current, run = ch, 1
        else:
            flush_tail(current, run)
            current, run = None, 0
            result.append(ch)

    flush_tail(current, run)
    return "".join(result)


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


def _cache_path(part_numbers_path: Path, who: str) -> Path:
    return part_numbers_path / f"{who}_cache.json"


def _read_cache(cache_file: Path) -> list[int]:
    if not cache_file.exists():
        return []
    try:
        return json.loads(cache_file.read_text(encoding="utf-8")).get("reserved", [])
    except Exception:
        return []


def _write_cache_atomic(cache_file: Path, reserved: list[int]) -> None:
    tmp = cache_file.with_suffix(".tmp")
    tmp.write_text(json.dumps({"reserved": reserved}, indent=2), encoding="utf-8")
    tmp.replace(cache_file)


def _read_next_up(part_numbers_path: Path) -> int:
    return int(
        json.loads((part_numbers_path / "next_up.json").read_text(encoding="utf-8"))[
            "next"
        ]
    )


def _write_next_up_atomic(part_numbers_path: Path, value: int) -> None:
    p = part_numbers_path / "next_up.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"next": value}, indent=2), encoding="utf-8")
    tmp.replace(p)


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _repo_root(part_numbers_path: Path) -> Path:
    return part_numbers_path.parent


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)


def _git_pull(repo: Path) -> tuple[bool, str]:
    r = _run_git(repo, "pull", "--ff-only")
    if r.returncode != 0:
        return False, r.stderr.strip() or r.stdout.strip()
    return True, ""


def git_commit_and_push(
    part_numbers_path: str | Path,
    files: list[Path],
    message: str,
) -> tuple[bool, str]:
    """
    Stage `files`, commit with `message`, and push.
    On push rejection (non-fast-forward or ref-lock), pulls with --rebase and
    retries the push once. This handles both the multi-user race and the case
    where a previous push landed on the remote between our last pull and now.
    Intended to run in a background thread. Returns (success, error_string).
    """
    pnp = Path(part_numbers_path)
    repo = _repo_root(pnp)
    rel = [str(f.relative_to(repo)) for f in files]

    _run_git(repo, "add", *rel)

    r = _run_git(repo, "commit", "-m", message)
    if r.returncode != 0:
        combined = (r.stdout + r.stderr).lower()
        if "nothing to commit" in combined:
            # Another push worker already staged and committed these changes — fine.
            return True, ""
        return False, f"git commit failed: {r.stderr.strip() or r.stdout.strip()}"

    r = _run_git(repo, "push")
    if r.returncode == 0:
        return True, ""

    push_err = r.stderr.strip() or r.stdout.strip()

    # Push was rejected — pull with rebase to get remote changes, then retry once.
    r_pull = _run_git(repo, "pull", "--rebase", "--autostash")
    if r_pull.returncode != 0:
        return False, (
            f"git push failed: {push_err}\n"
            f"git pull --rebase also failed: {r_pull.stderr.strip() or r_pull.stdout.strip()}"
        )

    r = _run_git(repo, "push")
    if r.returncode != 0:
        return (
            False,
            f"git push failed (after rebase): {r.stderr.strip() or r.stdout.strip()}",
        )

    return True, ""


# ---------------------------------------------------------------------------
# Local issue — Option A (new base PN)
# ---------------------------------------------------------------------------


def issue_new_base_pn_local(
    part_numbers_path: str | Path,
    who: str,
    setup: dict,
    type_letter: str,
    source_of_truth: str,
) -> tuple[str, str]:
    """
    Pop the next base number from the user's cache, assemble the PN, append to
    pn_log.csv, and write the updated cache — all local, no git.

    Returns (pn, error). Call git_commit_and_push() afterwards in a background thread.
    """
    pnp = Path(part_numbers_path)
    cache_file = _cache_path(pnp, who)

    reserved = _read_cache(cache_file)
    if not reserved:
        return "", "Cache is empty — refill required."

    seq, remaining = reserved[0], reserved[1:]
    pn = assemble_pn(
        setup.get("format", ""), setup.get("prefix", ""), seq, type_letter, 1
    )
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    log_file = pnp / "pn_log.csv"
    try:
        with log_file.open("a", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow([pn, who, timestamp, source_of_truth])
    except Exception as exc:
        return "", f"CSV write failed: {exc}"

    try:
        _write_cache_atomic(cache_file, remaining)
    except Exception as exc:
        return "", f"Cache write failed: {exc}"

    return pn, ""


# ---------------------------------------------------------------------------
# Local issue — Option B (sub PN)
# ---------------------------------------------------------------------------


def issue_sub_pn_local(
    part_numbers_path: str | Path,
    who: str,
    setup: dict,
    base_pn_root: str,
    type_letter: str,
    source_of_truth: str,
) -> tuple[str, str]:
    """
    Issue a sub PN for an existing base root. Scans pn_log.csv for the sub count.
    Does NOT touch next_up.json or the cache. Local only — call git_commit_and_push() after.
    """
    pnp = Path(part_numbers_path)
    log_file = pnp / "pn_log.csv"

    count = 0
    try:
        with log_file.open("r", newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("pn", "").startswith(base_pn_root):
                    count += 1
    except FileNotFoundError:
        pass
    except Exception as exc:
        return "", f"Could not read pn_log.csv: {exc}"

    pn = _assemble_sub_pn(setup, base_pn_root, type_letter, count + 1)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        with log_file.open("a", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow([pn, who, timestamp, source_of_truth])
    except Exception as exc:
        return "", f"CSV write failed: {exc}"

    return pn, ""


# ---------------------------------------------------------------------------
# Cache refill
# ---------------------------------------------------------------------------


def refill_cache(
    part_numbers_path: str | Path,
    who: str,
    cache_size: int,
) -> tuple[list[int], str]:
    """
    Reserve the next `cache_size` base sequence numbers and merge them with any
    numbers already sitting in the user's cache (so existing reserved numbers are
    not lost if a proactive top-up runs mid-session).

    Steps:
      1. git pull --ff-only
      2. Read next_up.json
      3. Reserve [next, ..., next+cache_size-1], merge with existing cache
      4. Atomically write next_up.json and <who>_cache.json
      5. git commit + push both files
      6. On push failure: git reset --hard HEAD~1 (fully undoes commit + disk state)
         On commit failure: restore files from saved text

    Returns (newly_reserved_list, error).
    """
    pnp = Path(part_numbers_path)
    repo = _repo_root(pnp)
    cache_file = _cache_path(pnp, who)

    ok, err = _git_pull(repo)
    if not ok:
        return [], f"git pull failed: {err}"

    try:
        nxt = _read_next_up(pnp)
    except Exception as exc:
        return [], f"Could not read next_up.json: {exc}"

    # Only reserve as many as needed to bring cache up to cache_size.
    existing_reserved = _read_cache(cache_file)
    needed = cache_size - len(existing_reserved)
    if needed <= 0:
        return [], ""

    new_reserved = list(range(nxt, nxt + needed))
    new_next = nxt + needed
    merged_cache = existing_reserved + new_reserved

    old_next_text = (pnp / "next_up.json").read_text(encoding="utf-8")
    old_cache_text = (
        cache_file.read_text(encoding="utf-8") if cache_file.exists() else None
    )

    try:
        _write_next_up_atomic(pnp, new_next)
        _write_cache_atomic(cache_file, merged_cache)
    except Exception as exc:
        (pnp / "next_up.json").write_text(old_next_text, encoding="utf-8")
        if old_cache_text is not None:
            cache_file.write_text(old_cache_text, encoding="utf-8")
        elif cache_file.exists():
            cache_file.unlink()
        return [], f"File write failed during refill: {exc}"

    commit_msg = f"reserve: {nxt}–{nxt + needed - 1} ({who})"
    ok, err = git_commit_and_push(pnp, [pnp / "next_up.json", cache_file], commit_msg)
    if not ok:
        if "push failed" in err:
            # Undo the commit so HEAD + disk both return to pre-refill state cleanly.
            _run_git(repo, "reset", "--hard", "HEAD~1")
        else:
            # Commit failed — files on disk are new but uncommitted; restore manually.
            (pnp / "next_up.json").write_text(old_next_text, encoding="utf-8")
            if old_cache_text is not None:
                cache_file.write_text(old_cache_text, encoding="utf-8")
            elif cache_file.exists():
                cache_file.unlink()
        return [], err

    return new_reserved, ""
