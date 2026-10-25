#!/usr/bin/env python3
"""Generate real TPC-H SF=1 LINEITEM and a deterministic benchmark master.

The official TPC-H tools are an external dependency. This script does not
download or redistribute their source code and never substitutes random rows.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from tpch_inputs import (DEFAULT_STORE, InputError, InputSpec, MAX_ROWS,
                         _build_master, digest, generate_input, load_master)


def atomic_json(path: Path, value: dict) -> None:
    """Replace a manifest only after a complete temporary file is written."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".manifest-", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            json.dump(value, output, indent=2, sort_keys=True)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def release_version(path: Path | None) -> str | None:
    """Read a release header when available, without inventing a version."""
    if path is None or not path.is_file():
        return None
    text = path.read_text(encoding="ascii", errors="replace")
    values = []
    for name in ("VERSION", "RELEASE", "PATCH", "BUILD"):
        match = re.search(r"^\s*#\s*define\s+" + name + r"\s+(\d+)\b", text, re.M)
        if match is None:
            return None
        values.append(match.group(1))
    return ".".join(values)


def run_logged(command: list[str], directory: Path, log: Path) -> None:
    with log.open("wb") as output:
        subprocess.run(command, cwd=directory, stdout=output,
                       stderr=subprocess.STDOUT, check=True)


def resolve_generator(args, store: Path) -> tuple[Path, Path, dict]:
    """Use a supplied executable or build a local copy of official sources."""
    if args.dbgen_dir is not None:
        source = args.dbgen_dir.resolve()
        if not (source / "makefile.suite").is_file() or not (source / "dists.dss").is_file():
            raise InputError("--dbgen-dir must contain makefile.suite and dists.dss")
        build = store / "generator-build"
        build.mkdir(exist_ok=True)
        files = [p for p in source.iterdir() if p.is_file()
                 and (p.suffix.lower() in (".c", ".h") or p.name in ("makefile.suite", "dists.dss"))]
        for path in files:
            shutil.copyfile(path, build / path.name)
        command = ["make", "-f", "makefile.suite", "CC=gcc", "DATABASE=ORACLE",
                   "MACHINE=LINUX", "WORKLOAD=TPCH", "dbgen"]
        run_logged(command, build, store / "dbgen-build.log")
        executable = build / "dbgen"
        distribution = build / "dists.dss"
        metadata = {
            "release": release_version(source / "release.h"),
            "source_files_sha256": {path.name: digest(path) for path in sorted(files)},
            "build_command": command,
        }
    else:
        if args.dbgen is None:
            raise InputError("Supply --dbgen-dir, or --dbgen with its dists.dss file")
        executable = args.dbgen.resolve()
        distribution = (args.dists or executable.parent / "dists.dss").resolve()
        metadata = {"release": release_version(args.release_header),
                    "source_files_sha256": {}, "build_command": None}
    if not executable.is_file() or not distribution.is_file():
        raise InputError("The dbgen executable and dists.dss must both exist")
    metadata["binary_sha256"] = digest(executable)
    metadata["dists_sha256"] = digest(distribution)
    return executable, distribution, metadata


def generate_source(args, store: Path) -> tuple[Path, dict]:
    executable, distribution, metadata = resolve_generator(args, store)
    source_directory = store / "source"
    source_directory.mkdir(exist_ok=True)
    table = source_directory / "lineitem.tbl"
    if table.exists():
        raise InputError("Unmanifested lineitem.tbl already exists in the store; use a new store")
    shutil.copyfile(distribution, source_directory / "dists.dss")
    command = [str(executable), "-s", "1", "-T", "L"]
    run_logged(command, source_directory, store / "dbgen-generate.log")
    if not table.is_file():
        raise InputError("dbgen did not create lineitem.tbl")
    metadata["generate_command"] = ["dbgen", "-s", "1", "-T", "L"]
    return table, metadata


def materialize_defaults(store: Path, powers: list[int], seed: int) -> dict:
    """Create the paired default inputs and plaintext validation references."""
    manifest_path = store / "manifest.json"
    metadata = json.loads(manifest_path.read_text(encoding="utf-8"))
    for exponent in sorted(set(powers)):
        result = generate_input(InputSpec(exponent, 2, seed=seed), tpch_store=store,
                                output_dir=store / "inputs")
        path = Path(result["path"])
        rows = [tuple(map(int, line.split())) for line in path.read_text(encoding="ascii").splitlines()]
        reference = path.with_suffix(".sorted.txt")
        reference_bytes = "".join(f"{key} {payload}\n" for key, payload in sorted(rows)).encode("ascii")
        if reference.exists():
            if reference.read_bytes() != reference_bytes:
                raise InputError("Existing validation reference differs from the frozen input")
        else:
            reference.write_bytes(reference_bytes)
        count = 1 << exponent
        line_ids = []
        with (store / "master-source-row-ids.csv").open(encoding="ascii") as stream:
            next(stream)
            for line in stream:
                if len(line_ids) == count:
                    break
                line_ids.append(int(line.split(",")[1]))
        metadata["inputs"][path.name] = {
            "path": str(path), "sha256": digest(path), "rows": count,
            "cols": 2, "keys": 1, "payloads": 1, "dtype": "int32",
            "distribution": "Uniform", "source_profile": "U32",
            "source_columns": ["L_ORDERKEY", "L_PARTKEY"],
            "source_row_id_hash": hashlib.sha256(json.dumps(line_ids).encode("ascii")).hexdigest(),
            "reference_path": str(reference), "reference_sha256": digest(reference),
            "distinct_keys": count, "sort_order": "ASC", "stable_sort_required": False,
        }
        atomic_json(manifest_path, metadata)
    return metadata


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=DEFAULT_STORE)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--dbgen-dir", type=Path, help="Directory containing the official dbgen sources")
    source.add_argument("--dbgen", type=Path, help="Existing official dbgen executable")
    parser.add_argument("--dists", type=Path, help="dists.dss for an existing dbgen executable")
    parser.add_argument("--release-header", type=Path, help="Optional release.h accompanying the executable")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--powers", type=int, nargs="+", default=[8, 16],
                        help="Default input row exponents to materialize, between 0 and 20")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    store = args.store.resolve()
    try:
        if any(not 0 <= power <= 20 for power in args.powers):
            raise InputError("Each --powers value must be between 0 and 20")
        store.mkdir(parents=True, exist_ok=True)
        manifest_path = store / "manifest.json"
        if manifest_path.is_file():
            _, _, metadata = load_master(InputSpec(20, 2, seed=args.seed), tpch_store=store,
                                          read_only=True)
            if metadata.get("scale_factor") != 1 or metadata.get("source_role") != "dbgen-sf1":
                raise InputError("Existing store was not prepared as a dbgen SF=1 source")
        else:
            source, generator_metadata = generate_source(args, store)
            metadata = _build_master(source, store, args.seed)
            if metadata["master_rows"] != MAX_ROWS:
                raise InputError("SF=1 LINEITEM must supply at least 2^20 distinct order keys")
            metadata.update(scale_factor=1, source_role="dbgen-sf1",
                            source_provenance="TPC-H dbgen LINEITEM generated at scale factor 1",
                            dbgen=generator_metadata)
            atomic_json(manifest_path, metadata)
        metadata = materialize_defaults(store, args.powers, args.seed)
    except (InputError, OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"TPC-H preparation error: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"status": "TPC_H_SF1_READY", "manifest": str(manifest_path),
                      "master_rows": metadata["master_rows"], "inputs": list(metadata["inputs"])}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
