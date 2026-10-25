"""Shared helpers for pinned, user-owned dependency workspaces."""

from __future__ import annotations

import json
from pathlib import Path

ARTIFACT_ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = Path(__file__).with_name("lock.json")


def load_lock() -> dict:
    """Read the public upstream references without consulting Git history."""
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    if lock.get("schema_version") != 1:
        raise ValueError("Unsupported dependency lock format")
    return lock


def dependency(name: str, profile: str = "base") -> dict:
    """Resolve a source profile while preserving its pinned upstream identity."""
    sources = load_lock()["dependencies"]
    if name not in sources:
        raise ValueError(f"Unknown dependency: {name}")
    entry = dict(sources[name])
    profiles = entry.pop("profiles", {})
    if profile != "base":
        if profile not in profiles:
            raise ValueError(f"Dependency {name} has no {profile!r} profile")
        entry.update(profiles[profile])
    entry.update(name=name, profile=profile)
    revision = entry["revision"]
    if len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision):
        raise ValueError(f"Invalid pinned revision for {name}")
    if not entry["url"].startswith("https://github.com/"):
        raise ValueError("Only the public HTTPS upstream URLs in lock.json are supported")
    return entry


def workspace_path(value: str | Path | None = None) -> Path:
    return Path(value).expanduser().resolve() if value else ARTIFACT_ROOT / "workspace"


def source_path(entry: dict, workspace: Path) -> Path:
    """Keep configurable source directories inside the selected workspace."""
    name = Path(entry["source_directory"]).name
    if name in ("", ".", ".."):
        raise ValueError("Invalid source directory")
    base = (workspace / "src").resolve()
    result = (base / name).resolve()
    if result.parent != base:
        raise ValueError("Dependency path escapes the workspace")
    return result


def contained_path(base: Path, relative: str) -> Path:
    """Reject absolute paths, traversal, and symlinks escaping a source tree."""
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"Unsafe relative path: {relative}")
    resolved_base = base.resolve()
    result = (resolved_base / candidate).resolve()
    if not result.is_relative_to(resolved_base):
        raise ValueError(f"Path escapes its source tree: {relative}")
    return result
