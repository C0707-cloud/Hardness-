"""Workspace pre/post snapshots and diff for H1/H4 environment evidence.

Read-only: collection never writes into the workspace. Unreadable files are
recorded as ``type=error`` entries, never skipped silently.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import stat
from datetime import datetime
from pathlib import Path

SNAPSHOT_SCHEMA = "workspace-v1"
DIFF_SCHEMA = "workspace-diff-v1"
SNAPSHOT_KINDS = {"pre", "post"}

DEFAULT_IGNORE_DIRS = (".git", "__pycache__")
DEFAULT_EVIDENCE_DIRS = ("runs",)
DEFAULT_IGNORE_PATTERNS = (
    "*.tmp", "*.temp", "*.swp", "*.swo", "*~",
    ".DS_Store", "Thumbs.db", "*.pyc", "*.pyo",
)

CHUNK_SIZE = 1024 * 1024


class WorkspaceError(ValueError):
    pass


def _sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(CHUNK_SIZE)
            if not block:
                break
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _relative(path: str, root: str) -> str:
    return os.path.relpath(path, root).replace(os.sep, "/")


def _normalized(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _error_entry(rel: str, exc: Exception, exists: bool) -> dict:
    return {
        "path": rel,
        "type": "error",
        "size": None,
        "sha256": None,
        "exists": exists,
        "error": f"{type(exc).__name__}: {exc}",
    }


def _file_entry(full: str, rel: str) -> dict:
    try:
        info = os.lstat(full)
    except OSError as exc:
        return _error_entry(rel, exc, not isinstance(exc, FileNotFoundError))

    if stat.S_ISLNK(info.st_mode):
        try:
            target = os.readlink(full)
        except OSError as exc:
            return _error_entry(rel, exc, True)
        return {
            "path": rel,
            "type": "symlink",
            "size": len(target.encode("utf-8")),
            "sha256": _sha256_text(target),
            "exists": True,
            "link_target": target,
        }

    if not stat.S_ISREG(info.st_mode):
        return {
            "path": rel,
            "type": "error",
            "size": None,
            "sha256": None,
            "exists": True,
            "error": f"unsupported file type: {oct(info.st_mode)}",
        }

    try:
        digest = _sha256_file(full)
    except OSError as exc:
        return _error_entry(rel, exc, not isinstance(exc, FileNotFoundError))
    return {
        "path": rel,
        "type": "file",
        "size": info.st_size,
        "sha256": digest,
        "exists": True,
    }


def _matches_pattern(name: str, patterns) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


def collect_workspace(
    workspace: str | Path,
    kind: str = "pre",
    run_id: str = "run",
    ignore_dirs=(),
    evidence_dirs=None,
    ignore_patterns=(),
    exclude_paths=(),
) -> dict:
    """Scan ``workspace`` into a snapshot dict. Does not modify the workspace."""
    if kind not in SNAPSHOT_KINDS:
        raise WorkspaceError(f"kind must be pre or post, got {kind}")
    if not run_id:
        raise WorkspaceError("run_id must not be empty")

    root = os.path.abspath(str(workspace))
    dir_names = set(DEFAULT_IGNORE_DIRS) | set(ignore_dirs)
    if evidence_dirs is None:
        evidence_dirs = DEFAULT_EVIDENCE_DIRS
    evidence_names = set(evidence_dirs)
    patterns = set(DEFAULT_IGNORE_PATTERNS) | set(ignore_patterns)
    excluded = {_normalized(path) for path in exclude_paths}

    entries: list[dict] = []
    errors: list[dict] = []

    def add(entry: dict) -> None:
        if entry["type"] == "error":
            errors.append(entry)
        else:
            entries.append(entry)

    def skip_dir(rel: str, name: str, full: str) -> bool:
        if name in dir_names:
            return True
        if rel == name and name in evidence_names:
            return True
        return _normalized(full) in excluded

    def on_walk_error(exc: OSError) -> None:
        filename = getattr(exc, "filename", None)
        if filename:
            try:
                rel = _relative(filename, root)
            except ValueError:
                rel = str(filename)
        else:
            rel = "."
        errors.append(_error_entry(rel, exc, True))

    root_exists = os.path.exists(root)
    if not root_exists:
        pass
    elif not os.path.isdir(root):
        errors.append({
            "path": ".",
            "type": "error",
            "size": None,
            "sha256": None,
            "exists": True,
            "error": "workspace root is not a directory",
        })
    else:
        for dirpath, dirnames, filenames in os.walk(
            root, topdown=True, onerror=on_walk_error, followlinks=False
        ):
            kept_dirs = []
            for name in dirnames:
                full = os.path.join(dirpath, name)
                rel = _relative(full, root)
                if skip_dir(rel, name, full):
                    continue
                if os.path.islink(full):
                    add(_file_entry(full, rel))
                    continue
                kept_dirs.append(name)
            dirnames[:] = kept_dirs

            for name in filenames:
                full = os.path.join(dirpath, name)
                rel = _relative(full, root)
                if _matches_pattern(name, patterns):
                    continue
                if _normalized(full) in excluded:
                    continue
                add(_file_entry(full, rel))

    entries.sort(key=lambda entry: entry["path"])
    errors.sort(key=lambda entry: entry["path"])

    file_bytes = sum(
        entry["size"] for entry in entries
        if entry["type"] == "file" and entry.get("size") is not None
    )
    snapshot = {
        "schema_version": SNAPSHOT_SCHEMA,
        "kind": kind,
        "run_id": run_id,
        "workspace": root,
        "collected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "root_exists": root_exists,
        "ignore": {
            "dirs": sorted(dir_names),
            "evidence_dirs": sorted(evidence_names),
            "patterns": sorted(patterns),
        },
        "entries": entries,
        "errors": errors,
        "summary": {
            "files": sum(1 for e in entries if e["type"] == "file"),
            "symlinks": sum(1 for e in entries if e["type"] == "symlink"),
            "errors": len(errors),
            "bytes": file_bytes,
        },
    }
    return snapshot


def write_snapshot(path: str | Path, snapshot: dict) -> None:
    _write_json(path, snapshot)


def load_snapshot(path: str | Path) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            snapshot = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkspaceError(f"cannot read snapshot {path}: {exc}") from exc
    if not isinstance(snapshot, dict):
        raise WorkspaceError(f"{path}: snapshot must be a JSON object")
    if snapshot.get("schema_version") != SNAPSHOT_SCHEMA:
        raise WorkspaceError(f"{path}: schema_version must be {SNAPSHOT_SCHEMA}")
    if snapshot.get("kind") not in SNAPSHOT_KINDS:
        raise WorkspaceError(f"{path}: kind must be pre or post")
    if not snapshot.get("run_id"):
        raise WorkspaceError(f"{path}: missing run_id")
    if not snapshot.get("workspace"):
        raise WorkspaceError(f"{path}: missing workspace")
    if not isinstance(snapshot.get("entries"), list):
        raise WorkspaceError(f"{path}: entries must be a list")
    if not isinstance(snapshot.get("errors"), list):
        raise WorkspaceError(f"{path}: errors must be a list")
    if not isinstance(snapshot.get("ignore"), dict):
        raise WorkspaceError(f"{path}: ignore must be an object")
    if not isinstance(snapshot.get("collected_at"), str):
        raise WorkspaceError(f"{path}: missing collected_at")
    return snapshot


def _entry_view(entry: dict, exists: bool) -> dict:
    view = {
        "path": entry["path"],
        "type": entry["type"],
        "size": entry.get("size"),
        "sha256": entry.get("sha256"),
        "exists": exists,
    }
    if entry["type"] == "symlink" and "link_target" in entry:
        view["link_target"] = entry["link_target"]
    return view


def diff_snapshots(pre: dict, post: dict) -> dict:
    if pre.get("kind") != "pre":
        raise WorkspaceError("pre snapshot must have kind=pre")
    if post.get("kind") != "post":
        raise WorkspaceError("post snapshot must have kind=post")
    if pre.get("run_id") != post.get("run_id"):
        raise WorkspaceError(
            f"run_id mismatch: {pre.get('run_id')} vs {post.get('run_id')}"
        )
    if _normalized(pre["workspace"]) != _normalized(post["workspace"]):
        raise WorkspaceError(
            f"workspace mismatch: {pre['workspace']} vs {post['workspace']}"
        )
    if pre.get("ignore") != post.get("ignore"):
        raise WorkspaceError("ignore rules differ between pre and post snapshots")
    try:
        pre_time = datetime.fromisoformat(pre["collected_at"])
        post_time = datetime.fromisoformat(post["collected_at"])
    except ValueError as exc:
        raise WorkspaceError(f"invalid collected_at: {exc}") from exc
    try:
        if post_time < pre_time:
            raise WorkspaceError("post snapshot was collected before pre snapshot")
    except TypeError as exc:
        raise WorkspaceError(f"incomparable collected_at: {exc}") from exc

    pre_entries = {entry["path"]: entry for entry in pre["entries"]}
    post_entries = {entry["path"]: entry for entry in post["entries"]}
    pre_errors = {entry["path"]: entry for entry in pre["errors"]}
    post_errors = {entry["path"]: entry for entry in post["errors"]}

    added, modified, deleted, unchanged, errors = [], [], [], [], []
    for path in sorted(set(pre_entries) | set(post_entries) | set(pre_errors) | set(post_errors)):
        if path in pre_errors or path in post_errors:
            errors.append({
                "path": path,
                "old_error": pre_errors[path]["error"] if path in pre_errors else None,
                "new_error": post_errors[path]["error"] if path in post_errors else None,
            })
            continue
        if path not in pre_entries:
            added.append(_entry_view(post_entries[path], True))
            continue
        if path not in post_entries:
            deleted.append(_entry_view(pre_entries[path], False))
            continue
        old = pre_entries[path]
        new = post_entries[path]
        if (
            old["type"] == new["type"]
            and old["sha256"] == new["sha256"]
            and old.get("size") == new.get("size")
        ):
            unchanged.append(_entry_view(new, True))
            continue
        modified.append({
            "path": path,
            "type": new["type"],
            "old_type": old["type"],
            "new_type": new["type"],
            "old_hash": old["sha256"],
            "new_hash": new["sha256"],
            "old_size": old.get("size"),
            "new_size": new.get("size"),
            "exists": True,
        })

    return {
        "schema_version": DIFF_SCHEMA,
        "run_id": pre["run_id"],
        "workspace": pre["workspace"],
        "pre": {
            "kind": pre["kind"],
            "collected_at": pre["collected_at"],
        },
        "post": {
            "kind": post["kind"],
            "collected_at": post["collected_at"],
        },
        "added": added,
        "modified": modified,
        "deleted": deleted,
        "unchanged": unchanged,
        "errors": errors,
        "summary": {
            "added": len(added),
            "modified": len(modified),
            "deleted": len(deleted),
            "unchanged": len(unchanged),
            "errors": len(errors),
        },
    }


def write_diff(path: str | Path, diff: dict) -> None:
    _write_json(path, diff)


def _write_json(path: str | Path, payload: dict) -> None:
    target = Path(path)
    if target.parent and str(target.parent) not in ("", "."):
        target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
