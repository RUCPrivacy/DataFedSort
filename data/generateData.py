#!/usr/bin/env python3
"""Generate or verify deterministic TPC-H LINEITEM projections for the benchmark."""
import argparse
import os
from pathlib import Path
import sys

from tpch_inputs import InputError, InputSpec, generate_input


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("exp_n", type=int, help="Number of rows is 2^exp_n, up to 2^20")
    parser.add_argument("m", type=int, help="Total projected columns, keys followed by payloads")
    parser.add_argument("-k", type=int, default=1, help="Number of sorting columns")
    parser.add_argument("--dtype", choices=["int8", "int32", "int64", "float"], default="int32")
    parser.add_argument("--dist", choices=["Uniform", "Zipf", "PartiallySorted", "HighRepeat"], default="Uniform")
    parser.add_argument("--zipf_factor", type=float, default=2.0)
    parser.add_argument("--sorted_fraction", type=float, default=0.7)
    parser.add_argument("--repeat_fraction", type=float, default=0.5)
    parser.add_argument("--profile", choices=["default", "key-width"], default="default")
    parser.add_argument("--source-table", type=Path, default=os.environ.get("FES_TPCH_TABLE") or None,
                        help="Real lineitem.tbl, or FES_TPCH_TABLE. No synthetic fallback.")
    parser.add_argument("--tpch-store", type=Path, default=os.environ.get("FES_TPCH_STORE") or None,
                        help="Frozen master store, or FES_TPCH_STORE")
    parser.add_argument("--output-dir", type=Path,
                        help="Final output directory. Default workspace/data/generated/tpch-v1/<profile>")
    parser.add_argument("--seed", type=int, default=42)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--verify-existing", action="store_true",
                      help="Read-only verification of existing output, provenance, and deterministic bytes")
    mode.add_argument("--overwrite", action="store_true",
                      help="Explicitly replace an output and its sidecar. Never overwrite source or master.")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        spec = InputSpec(args.exp_n, args.m, args.k, args.dtype, args.dist, args.profile,
                         args.seed, args.zipf_factor, args.sorted_fraction, args.repeat_fraction)
        result = generate_input(spec, source_table=args.source_table, tpch_store=args.tpch_store,
                                output_dir=args.output_dir, verify_existing=args.verify_existing,
                                overwrite=args.overwrite)
    except (InputError, OSError, ValueError) as exc:
        print(f"TPC-H input error: {exc}", file=sys.stderr)
        return 1
    print(f"{result['status']}: {result['path']}")
    print(f"rows={result['manifest']['rows']} cols={result['manifest']['cols']} "
          f"profile={spec.profile} sha256={result['manifest']['sha256']}")
    print(f"Manifest: {result['path']}.manifest.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
