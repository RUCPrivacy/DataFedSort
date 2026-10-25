#!/usr/bin/env python3
"""Install the documented benchmark overlays into one pinned source tree."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

try:
    from .common import ARTIFACT_ROOT, contained_path, dependency, source_path, workspace_path
except ImportError:
    from common import ARTIFACT_ROOT, contained_path, dependency, source_path, workspace_path


def apply_overlay(name: str, workspace: Path, profile: str = "base", check: bool = False) -> list[dict]:
    entry = dependency(name, profile)
    source = source_path(entry, workspace)
    if not source.is_dir():
        raise FileNotFoundError(f"Fetch {name} ({profile}) before applying its overlay")
    revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if revision != entry["revision"]:
        raise ValueError(f"{name} source revision does not match lock.json")
    overlay = entry.get("overlay")
    records = []
    if overlay:
        overlay_root = contained_path(ARTIFACT_ROOT, overlay)
        for relative, destination in entry.get("overlay_map", {}).items():
            if destination is None:
                continue  # Deployment helpers are copied by the runtime adapter.
            origin = contained_path(overlay_root, relative)
            target = contained_path(source, destination)
            if check:
                if not target.is_file() or origin.read_bytes() != target.read_bytes():
                    raise ValueError(f"Overlay is not applied: {name}/{destination}")
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(origin, target)
                if target.suffix == ".sh":
                    target.chmod(target.stat().st_mode | 0o111)
            records.append({"file": destination, "sha256": hashlib.sha256(origin.read_bytes()).hexdigest()})
    post_apply = entry.get("post_apply")
    if post_apply:
        command = [sys.executable, str(contained_path(ARTIFACT_ROOT, post_apply)), str(source)]
        if check:
            command.append("--check")
        subprocess.run(command, check=True)
    if not check:
        (source / ".datafedsort-overlay.json").write_text(json.dumps({"dependency": name, "profile": profile, "upstream_revision": revision, "files": records}, indent=2) + "\n", encoding="utf-8")
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("system", help="Dependency ID from lock.json")
    parser.add_argument("--profile", default="base")
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--check", action="store_true", help="Verify an existing overlay without modifying it")
    arguments = parser.parse_args(argv)
    records = apply_overlay(arguments.system, workspace_path(arguments.workspace), arguments.profile, arguments.check)
    print(json.dumps({"dependency": arguments.system, "profile": arguments.profile, "checked": arguments.check, "overlay_files": len(records)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
