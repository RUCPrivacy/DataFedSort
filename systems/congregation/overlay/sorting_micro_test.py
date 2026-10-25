import sys
import json
import time
import os
import csv
import argparse
import subprocess

try:
    import resource
except ImportError:
    resource = None


def _parse_value(raw, data_type):
    if data_type == 'int':
        return int(raw)
    if data_type == 'float':
        return float(raw)
    raise ValueError(f"Unsupported data type: {data_type}")


def _get_zp(config):
    return int(config.get("jiff", {}).get("zp", 16777729))


def input_shape(input_path, max_rows=None):
    row_count = 0
    num_cols = None
    with open(input_path, 'r', newline='') as infile:
        for line in infile:
            line = line.strip()
            if not line:
                continue
            parts = line.replace(',', ' ').split()
            if num_cols is None:
                num_cols = len(parts)
            elif len(parts) != num_cols:
                raise ValueError(
                    f"Inconsistent column count in {input_path} at row {row_count + 1}: "
                    f"expected {num_cols}, got {len(parts)}"
                )
            row_count += 1
            if max_rows is not None and row_count >= max_rows:
                break

    if row_count == 0:
        raise ValueError(f"No rows found in input file: {input_path}")

    return row_count, num_cols


def row_range_for_party(total, party_id, num_parties):
    base = total // num_parties
    rem = total % num_parties
    start = (party_id - 1) * base + min(party_id - 1, rem)
    count = base + (1 if party_id <= rem else 0)
    return start, start + count


def build_column_rank_maps(input_path, num_cols, data_type, max_rows=None, modulus=None):
    values = [set() for _ in range(num_cols)]
    seen = 0
    with open(input_path, 'r', newline='') as infile:
        for line in infile:
            line = line.strip()
            if not line:
                continue
            if max_rows is not None and seen >= max_rows:
                break
            parts = line.replace(',', ' ').split()
            for col_idx, raw in enumerate(parts):
                values[col_idx].add(_parse_value(raw, data_type))
            seen += 1

    maps = []
    for col_idx, col_values in enumerate(values):
        if modulus is not None and len(col_values) + 1 >= modulus:
            raise ValueError(
                f"Column {col_idx} has {len(col_values)} distinct values, which does not fit in Zp={modulus}"
            )
        maps.append({value: rank + 1 for rank, value in enumerate(sorted(col_values))})
    return maps


def materialize_party_data(input_path, party_id, num_parties, data_type, output_dir,
                           max_rows=None, compress_to_rank=False, modulus=None):
    """
    Read a benchmark input table and materialize the slice owned by one party
    as the CSV layout expected by Congregation/JIFF code generation.
    """
    os.makedirs(output_dir, exist_ok=True)
    filename = os.path.join(output_dir, f"sort_data_{party_id}.csv")

    total_rows, num_cols = input_shape(input_path, max_rows)
    start, end = row_range_for_party(total_rows, party_id, num_parties)
    headers = [f"c{i}" for i in range(num_cols)]
    written = 0
    rank_maps = None
    if compress_to_rank:
        rank_maps = build_column_rank_maps(input_path, num_cols, data_type, max_rows, modulus)

    with open(filename, 'w', newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(headers)
        seen = 0
        with open(input_path, 'r', newline='') as infile:
            for line in infile:
                line = line.strip()
                if not line:
                    continue
                if max_rows is not None and seen >= max_rows:
                    break
                if start <= seen < end:
                    parts = line.replace(',', ' ').split()
                    parsed = [_parse_value(v, data_type) for v in parts]
                    if not compress_to_rank and modulus is not None and any(v < 0 or v >= modulus for v in parsed):
                        raise ValueError('Original input values do not fit the native JIFF field')
                    if rank_maps is not None:
                        parsed = [rank_maps[col_idx][value] for col_idx, value in enumerate(parsed)]
                    writer.writerow(parsed)
                    written += 1
                seen += 1

    print(
        f"Party {party_id}: Read {total_rows} rows from {input_path}; "
        f"wrote {written} rows x {num_cols} cols to {filename}; "
        f"rank_compressed={compress_to_rank}"
    )
    return num_cols


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Sorting Micro Test')
    parser.add_argument('--num-rows', type=int, default=100,
                        help='Maximum total rows to read from --input. Use <=0 to read all rows.')
    parser.add_argument('--data-type', type=str, default='int', choices=['int', 'float'],
                        help='Data type: int or float (default: int)')
    parser.add_argument('--party-id', type=int, choices=[1, 2, 3], required=True,
                        help='Party ID (1, 2, or 3).')
    parser.add_argument('--data-dir', type=str, default='./data',
                        help='Directory to write the party CSV data (default: ./data)')
    parser.add_argument('--input', type=str, required=True,
                        help='Path to the benchmark input table.')
    parser.add_argument('--key-col', type=int, default=0,
                        help='Zero-based sorting key column. Congregation sorts by one key column.')
    parser.add_argument('--no-rank-compress', action='store_true',
                        help='Disable order-preserving rank compression before secret sharing.')
    parser.add_argument('config_file', type=str,
                        help='Path to configuration JSON file')
    parser.add_argument('jiff_lib_path', type=str,
                        help='Path to JIFF library')
    
    args = parser.parse_args()
    cfg = json.loads(open(args.config_file, "r").read())
    cfg["jiff"]["jiff_lib_path"] = args.jiff_lib_path

    max_rows = args.num_rows if args.num_rows and args.num_rows > 0 else None
    num_cols = materialize_party_data(
        args.input,
        args.party_id,
        3,
        args.data_type,
        args.data_dir,
        max_rows,
        not args.no_rank_compress,
        _get_zp(cfg),
    )
    if args.key_col < 0 or args.key_col >= num_cols:
        raise ValueError(f"--key-col must be in [0, {num_cols - 1}], got {args.key_col}")
    
    data_file = os.path.join(args.data_dir, f"sort_data_{args.party_id}.csv")
    runner = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sort_operator_runner.js")
    subprocess.check_call([
        "node",
        runner,
        args.config_file,
        data_file,
        str(args.key_col),
        args.input if args.no_rank_compress else '',
    ])

    if resource is not None:
        self_usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        child_usage = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        peak_kb = child_usage if child_usage > 0 else self_usage
        print(f"Python wrapper peak memory usage: {peak_kb} KB")
