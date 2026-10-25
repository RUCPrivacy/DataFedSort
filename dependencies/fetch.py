#!/usr/bin/env python3
"""Fetch exact public upstream revisions into an ignored workspace."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess

try:
    from .apply import apply_overlay
    from .common import contained_path, dependency, load_lock, source_path, workspace_path
except ImportError:
    from apply import apply_overlay
    from common import contained_path, dependency, load_lock, source_path, workspace_path


def git(source: Path, *arguments: str) -> None:
    environment = dict(os.environ)
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        environment.pop(key, None)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    subprocess.run(["git", "-C", str(source), *arguments], env=environment, check=True)


def mount_source(destination: Path, relative: str, name: str, source: Path) -> None:
    """Copy a pinned dependency into an upstream include path without Git history."""
    target = contained_path(destination, relative)
    revision = dependency(name)["revision"]
    marker = {"dependency": name, "revision": revision}
    marker_path = target / ".datafedsort-dependency.json"
    if target.exists() and any(target.iterdir()):
        if (target / ".git").exists():
            actual = subprocess.check_output(
                ["git", "-C", str(target), "rev-parse", "HEAD"], text=True
            ).strip()
            if actual == revision:
                return
        elif marker_path.is_file() and json.loads(marker_path.read_text(encoding="utf-8")) == marker:
            return
        raise ValueError(f"Refusing to replace an unverified dependency mount: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git"))
    marker_path.write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")


def fetch(name: str, workspace: Path, profile: str, submodules: bool, overlay: bool,
          ancestry: tuple[str, ...] = ()) -> Path:
    if name in ancestry:
        raise ValueError("Cyclic source dependency: " + " -> ".join((*ancestry, name)))
    entry = dependency(name, profile)
    required = {}
    for requirement in entry.get("requires", []):
        required[requirement] = fetch(requirement, workspace, "base", submodules, overlay, (*ancestry, name))
    destination = source_path(entry, workspace)
    destination.mkdir(parents=True, exist_ok=True)
    if not (destination / ".git").exists():
        if any(destination.iterdir()):
            raise ValueError(f"Refusing to initialize a nonempty source directory: {destination}")
        git(destination, "init", "--quiet")
        git(destination, "remote", "add", "origin", entry["url"])
        git(destination, "fetch", "--depth=1", "origin", entry["revision"])
        git(destination, "checkout", "--detach", "FETCH_HEAD")
    actual = subprocess.check_output(["git", "-C", str(destination), "rev-parse", "HEAD"], text=True).strip()
    if actual != entry["revision"]:
        raise ValueError(f"Existing {name} checkout has another revision; use another workspace")
    if submodules and (destination / ".gitmodules").is_file():
        git(destination, "submodule", "update", "--init", "--recursive", "--depth=1")
    for relative, requirement in entry.get("source_mounts", {}).items():
        if requirement not in required:
            raise ValueError(f"Mount {relative} must name an explicitly required dependency")
        mount_source(destination, relative, requirement, required[requirement])
    if overlay:
        apply_overlay(name, workspace, profile)
    print(f"Ready: {name} ({profile}) at {entry['revision']}")
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("systems", nargs="*", help="Dependency IDs; omit to fetch every locked source")
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--profile", default="base", help="Use native_checked only with the explicitly selected orq dependency")
    parser.add_argument("--skip-submodules", action="store_true")
    parser.add_argument("--no-overlay", action="store_true", help="Fetch an unmodified upstream checkout")
    parser.add_argument("--list", action="store_true", help="List locked dependencies without accessing the network")
    arguments = parser.parse_args(argv)
    sources = load_lock()["dependencies"]
    if arguments.list:
        for name, entry in sources.items():
            print(name, entry["revision"], entry["url"])
        return 0
    names = arguments.systems or list(sources)
    if arguments.profile != "base" and names != ["orq"]:
        parser.error("A non-base profile requires selecting only orq")
    workspace = workspace_path(arguments.workspace)
    for name in names:
        fetch(name, workspace, arguments.profile, not arguments.skip_submodules, not arguments.no_overlay)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
