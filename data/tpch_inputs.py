"""Deterministic TPC-H LINEITEM projections and complete-row variations.

Use prepare_tpch.py to generate a scale-factor-1 master with the official
dbgen tools, or supply an existing LINEITEM table explicitly.
"""
from array import array
from bisect import bisect_left
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import random
import tempfile

DATA_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = DATA_DIR.parent
DEFAULT_STORE = PROJECT_ROOT / "workspace" / "data" / "tpch-sf1"
DEFAULT_OUTPUT = PROJECT_ROOT / "workspace" / "data" / "generated" / "tpch-v1"
MASTER_NAME = "LINEITEM-U32-master.tbl"
MAX_ROWS = 1 << 20
SCHEMA_VERSION = 1
GENERATOR = "datafedsort-tpch-input-v1"
# Keys are the first k columns. Additional payload columns belong to the same
# source row. This supports sweeps of eight keys and eight payloads.
DEFAULT_COLUMNS = (
    "L_ORDERKEY", "L_PARTKEY", "L_SUPPKEY", "L_LINENUMBER", "L_QUANTITY",
    "L_EXTENDEDPRICE", "L_DISCOUNT", "L_TAX", "L_SHIPDATE", "L_COMMITDATE",
    "L_RECEIPTDATE",
)
KEY_WIDTH_COLUMNS = ("L_QUANTITY", "L_LINENUMBER")
COLUMN_INDEX = dict(zip(DEFAULT_COLUMNS, (0, 1, 2, 3, 4, 5, 6, 7, 10, 11, 12)))
ENCODINGS = {
    "L_ORDERKEY": "integer", "L_PARTKEY": "integer", "L_SUPPKEY": "integer",
    "L_LINENUMBER": "integer", "L_QUANTITY": "exact decimal integer",
    "L_EXTENDEDPRICE": "decimal multiplied by 100, exact integer cents",
    "L_DISCOUNT": "decimal multiplied by 100, exact integer",
    "L_TAX": "decimal multiplied by 100, exact integer",
    "L_SHIPDATE": "integer days since 1970-01-01",
    "L_COMMITDATE": "integer days since 1970-01-01",
    "L_RECEIPTDATE": "integer days since 1970-01-01",
}


class InputError(ValueError):
    """An input or its provenance does not satisfy the requested configuration."""


@dataclass(frozen=True)
class InputSpec:
    exp_n: int
    m: int
    k: int = 1
    dtype: str = "int32"
    dist: str = "Uniform"
    profile: str = "default"
    seed: int = 42
    zipf_factor: float = 2.0
    sorted_fraction: float = 0.7
    repeat_fraction: float = 0.5

    @property
    def rows(self):
        return 1 << self.exp_n

    def validate(self):
        if not 0 <= self.exp_n <= 20:
            raise InputError("exp_n must be between 0 and 20")
        if not 1 <= self.k <= self.m <= len(DEFAULT_COLUMNS):
            raise InputError("require 1 <= k <= m <= 11 LINEITEM numeric/date columns")
        if self.dtype not in ("int8", "int32", "int64", "float"):
            raise InputError(f"Unsupported dtype {self.dtype!r}")
        if self.profile not in ("default", "key-width"):
            raise InputError(f"Unsupported profile {self.profile!r}")
        if self.profile == "default" and self.dtype == "int8":
            raise InputError("default L_ORDERKEY values cannot be int8. Use --profile key-width.")
        if self.profile == "key-width" and (self.m != 2 or self.k != 1):
            raise InputError("key-width requires m=2 and k=1 (L_QUANTITY, L_LINENUMBER)")
        if self.dist not in ("Uniform", "Zipf", "PartiallySorted", "HighRepeat"):
            raise InputError(f"Unsupported distribution {self.dist!r}")
        if not math.isfinite(self.zipf_factor) or self.zipf_factor <= 0:
            raise InputError("zipf_factor must be finite and positive")
        if not math.isfinite(self.sorted_fraction) or not 0 <= self.sorted_fraction <= 1:
            raise InputError("sorted_fraction must be between 0 and 1")
        if not math.isfinite(self.repeat_fraction) or not 0 <= self.repeat_fraction <= 1:
            raise InputError("repeat_fraction must be between 0 and 1")

    def configuration(self):
        data = asdict(self)
        for field, required in (("zipf_factor", "Zipf"), ("sorted_fraction", "PartiallySorted"),
                                ("repeat_fraction", "HighRepeat")):
            if self.dist != required:
                data.pop(field)
        return data


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def input_filename(spec):
    suffix = ""
    if spec.dist == "Zipf":
        suffix = "_z" + str(float(spec.zipf_factor)).replace(".", "p")
    elif spec.dist == "PartiallySorted":
        suffix = f"_s{int(round(spec.sorted_fraction * 100))}"
    elif spec.dist == "HighRepeat":
        suffix = f"_r{int(round(spec.repeat_fraction * 100))}"
    return f"data_2pow{spec.exp_n}_m{spec.m}_k{spec.k}_{spec.dtype}_{spec.dist}{suffix}.txt"


def manifest_path(path):
    return Path(str(path) + ".manifest.json")


def fields_from_line(line, context):
    fields = line.rstrip("\r\n").split("|")
    if len(fields) != 17 or fields[-1] != "":
        raise InputError(f"{context}: expected the 16 LINEITEM fields and trailing '|'")
    return fields


def _read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise InputError(f"Cannot read JSON manifest {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise InputError(f"Manifest must be a JSON object: {path}")
    return value


def _required_digest(value, field):
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise InputError(f"Missing or invalid {field}")
    return value


def _install_file(temporary, destination, overwrite=False):
    if overwrite:
        os.replace(temporary, destination)
    else:
        try:
            os.link(temporary, destination)
        except FileExistsError as exc:
            raise InputError(f"Refusing to overwrite existing file {destination}") from exc
        Path(temporary).unlink()


def _temporary(path, binary=False):
    return tempfile.NamedTemporaryFile(mode="wb" if binary else "w", delete=False,
                                       dir=Path(path).parent, prefix="." + Path(path).name + ".",
                                       suffix=".tmp", **({} if binary else {"encoding": "ascii", "newline": "\n"}))


def _build_master(source, store, seed):
    """Sample distinct order-key groups and retain one complete row per group."""
    source = Path(source).resolve()
    if not source.is_file():
        raise InputError(f"Source table does not exist: {source}")
    store.mkdir(parents=True, exist_ok=True)
    for name in (MASTER_NAME, "master-source-row-ids.csv", "manifest.json"):
        if (store / name).exists():
            raise InputError(f"Unverified or existing master artifact {store / name}. Preserve it and inspect it.")
    lock = store / ".building.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise InputError(f"Master build lock exists: {lock}. Check the other build before retrying.") from exc
    os.close(fd)
    temporary, created = [], []
    try:
        source_sha = digest(source)
        keys, starts, counts = array("Q"), array("Q"), array("I")
        last, source_rows = None, 0
        with source.open(encoding="ascii") as stream:
            for source_rows, line in enumerate(stream, 1):
                fields = fields_from_line(line, f"{source}:{source_rows}")
                key = int(fields[0])
                if not 0 <= key < (1 << 64):
                    raise InputError("L_ORDERKEY must be a nonnegative 64-bit integer")
                if key != last:
                    if last is not None and key <= last:
                        raise InputError("Source order-key groups must be contiguous and strictly increasing")
                    keys.append(key)
                    starts.append(source_rows)
                    counts.append(1)
                    last = key
                else:
                    counts[-1] += 1
        if not keys:
            raise InputError("Source table is empty")
        size = min(len(keys), MAX_ROWS)
        groups = random.Random(seed).sample(range(len(keys)), size)
        row_rng = random.Random(seed + 1)
        selected_lines = [starts[g] + row_rng.randrange(counts[g]) for g in groups]
        selected = {line_id: index for index, line_id in enumerate(selected_lines)}
        master = [None] * size
        with source.open(encoding="ascii") as stream:
            for line_id, line in enumerate(stream, 1):
                if line_id in selected:
                    index = selected[line_id]
                    fields = fields_from_line(line, f"{source}:{line_id}")
                    if int(fields[0]) != keys[groups[index]]:
                        raise InputError("Source changed while selecting master rows")
                    master[index] = line.rstrip("\r\n") + "\n"
        if any(line is None for line in master):
            raise InputError("Source changed or selected rows could not be located")
        if digest(source) != source_sha:
            raise InputError("Source table changed while building the frozen master")
        master_file = store / MASTER_NAME
        with _temporary(master_file) as output:
            temporary.append(Path(output.name))
            output.writelines(master)
        row_ids = store / "master-source-row-ids.csv"
        with _temporary(row_ids) as output:
            temporary.append(Path(output.name))
            output.write("master_index,source_line_id,L_ORDERKEY,L_LINENUMBER\n")
            for index, line in enumerate(master):
                fields = line.split("|")
                output.write(f"{index},{selected_lines[index]},{fields[0]},{fields[3]}\n")
        metadata = {
            "schema_version": SCHEMA_VERSION, "store_kind": GENERATOR,
            "source_table": "TPC-H LINEITEM", "source_path": str(source),
            "source_sha256": source_sha, "source_rows": source_rows,
            "source_distinct_orderkeys": len(keys), "scale_factor": None,
            "source_provenance": "User-supplied TPC-H LINEITEM source; dbgen release and scale not recorded.",
            "source_role": "small-source-test-only" if size < MAX_ROWS else "lineitem-source",
            "key_sampling_seed": seed, "row_selection_seed": seed + 1,
            "master_rows": size, "master_sha256": digest(temporary[0]),
            "master_row_ids_sha256": digest(temporary[1]), "inputs": {},
        }
        metadata_file = store / "manifest.json"
        with _temporary(metadata_file) as output:
            temporary.append(Path(output.name))
            json.dump(metadata, output, indent=2, sort_keys=True)
            output.write("\n")
        for temp, final in zip(temporary, (master_file, row_ids, metadata_file)):
            _install_file(temp, final)
            created.append(final)
        return metadata
    except Exception:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    finally:
        for path in temporary:
            path.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)


def _choose_store(tpch_store):
    if tpch_store is not None:
        return Path(tpch_store).resolve()
    from_env = os.environ.get("FES_TPCH_STORE")
    if from_env:
        return Path(from_env).resolve()
    return DEFAULT_STORE


def load_master(spec, source_table=None, tpch_store=None, read_only=False):
    store = _choose_store(tpch_store)
    source_table = source_table or os.environ.get("FES_TPCH_TABLE") or None
    source = Path(source_table).resolve() if source_table is not None else None
    metadata_file = store / "manifest.json"
    if not metadata_file.is_file():
        if read_only:
            raise InputError(f"Read-only verification requires an existing master manifest: {metadata_file}")
        if source is None:
            raise InputError("No verified TPC-H master is available. Supply --source-table/FES_TPCH_TABLE "
                             "or --tpch-store/FES_TPCH_STORE. No random-data fallback is permitted.")
        metadata = _build_master(source, store, spec.seed)
    else:
        metadata = _read_json(metadata_file)
    if metadata.get("schema_version") != SCHEMA_VERSION or metadata.get("source_table") != "TPC-H LINEITEM":
        raise InputError(f"Store is not identified as TPC-H LINEITEM: {metadata_file}")
    if not isinstance(metadata.get("inputs", {}), dict):
        raise InputError("Store inputs must be a JSON object")
    if metadata.get("key_sampling_seed") != spec.seed or metadata.get("row_selection_seed") != spec.seed + 1:
        raise InputError("Requested seed does not match the frozen master's sampling seeds. Use a separate store.")
    master = store / MASTER_NAME
    expected = _required_digest(metadata.get("master_sha256"), "master_sha256")
    if not master.is_file() or digest(master) != expected:
        raise InputError(f"Missing or hash-mismatched master table: {master}")
    source_sha = _required_digest(metadata.get("source_sha256"), "source_sha256")
    if source is not None and (not source.is_file() or digest(source) != source_sha):
        raise InputError("Requested source table is missing or does not match the frozen source hash")
    if not isinstance(metadata.get("master_rows"), int) or not 1 <= metadata["master_rows"] <= MAX_ROWS:
        raise InputError("Invalid master_rows in store manifest")
    if spec.rows > metadata["master_rows"]:
        raise InputError(f"Requested {spec.rows} distinct master rows, but source supplies only "
                         f"{metadata['master_rows']}. Small fixtures are not expanded into fabricated rows.")
    row_ids = store / "master-source-row-ids.csv"
    row_id_sha = _required_digest(metadata.get("master_row_ids_sha256"), "master_row_ids_sha256")
    if not row_ids.is_file() or digest(row_ids) != row_id_sha:
        raise InputError(f"Missing or hash-mismatched master source-row index: {row_ids}")
    return store, master, metadata


def _encode(fields, name):
    value = fields[COLUMN_INDEX[name]]
    try:
        if name.endswith("DATE"):
            return (date.fromisoformat(value) - date(1970, 1, 1)).days
        if name in ("L_QUANTITY", "L_EXTENDEDPRICE", "L_DISCOUNT", "L_TAX"):
            decimal = Decimal(value)
            if not decimal.is_finite():
                raise InputError(f"{name} must be finite")
            numerator, denominator = decimal.as_integer_ratio()
            encoded, remainder = divmod(numerator * (1 if name == "L_QUANTITY" else 100), denominator)
            if remainder:
                raise InputError(f"{name} cannot be encoded exactly as an integer: {value!r}")
            return encoded
        return int(value)
    except (ValueError, InvalidOperation, OverflowError) as exc:
        raise InputError(f"Invalid {name} value {value!r}") from exc


def project_master(spec, master):
    columns = KEY_WIDTH_COLUMNS if spec.profile == "key-width" else DEFAULT_COLUMNS[:spec.m]
    limits = {"int8": (-(1 << 7), (1 << 7) - 1), "int32": (-(1 << 31), (1 << 31) - 1),
              "int64": (-(1 << 63), (1 << 63) - 1), "float": (-(1 << 53), (1 << 53))}
    lo, hi = limits[spec.dtype]
    rows = []
    with master.open(encoding="ascii") as stream:
        for index, line in enumerate(stream):
            if index == spec.rows:
                break
            fields = fields_from_line(line, f"{master}:{index + 1}")
            row = tuple(_encode(fields, name) for name in columns)
            for name, value in zip(columns, row):
                if not lo <= value <= hi:
                    raise InputError(f"{name}={value} is outside {spec.dtype} range. Values are never clipped or wrapped.")
            rows.append(row)
    if len(rows) != spec.rows:
        raise InputError("Master table is shorter than its declared prefix")
    return columns, rows


def transform_rows(spec, rows):
    """Return complete-row indices, never independently alter keys or payloads."""
    count = len(rows)
    indices = list(range(count))
    rng = random.Random(spec.seed)
    details = {"kind": spec.dist, "unit": "complete source row", "seed": spec.seed}
    if spec.dist == "Zipf":
        ranked = sorted(indices, key=lambda i: rows[i][:spec.k])
        cumulative, total = [], 0.0
        for rank in range(1, count + 1):
            total += rank ** (-spec.zipf_factor)
            cumulative.append(total)
        indices = [ranked[min(bisect_left(cumulative, rng.random() * total), count - 1)]
                   for _ in range(count)]
        details.update(zipf_factor=spec.zipf_factor, rank_order="ascending lexicographic sorting keys")
    elif spec.dist == "HighRepeat":
        repeated = int(round(count * spec.repeat_fraction))
        if repeated > 1:
            hot = rng.randrange(count)
            for position in rng.sample(range(count - 1), repeated - 1):
                indices[position + (position >= hot)] = hot
        details.update(repeat_fraction=spec.repeat_fraction, target_hot_row_count=repeated)
    elif spec.dist == "PartiallySorted":
        prefix = int(count * spec.sorted_fraction)
        indices[:prefix] = sorted(indices[:prefix], key=lambda i: rows[i][:spec.k])
        details.update(sorted_fraction=spec.sorted_fraction, sorted_prefix_rows=prefix,
                       ordering="ascending lexicographic sorting keys, stable")
    return indices, details


def _lines(rows, indices):
    for index in indices:
        yield (" ".join(map(str, rows[index])) + "\n").encode("ascii")


def generate_input(spec, source_table=None, tpch_store=None, output_dir=None,
                   verify_existing=False, overwrite=False):
    """Return {path, manifest, status}. Existing caches are verified, not trusted."""
    spec.validate()
    if verify_existing and overwrite:
        raise InputError("verify_existing and overwrite are mutually exclusive")
    store, master, metadata = load_master(spec, source_table, tpch_store, read_only=verify_existing)
    columns, rows = project_master(spec, master)
    indices, transformation = transform_rows(spec, rows)
    expected_hash = hashlib.sha256()
    for line in _lines(rows, indices):
        expected_hash.update(line)
    expected_sha = expected_hash.hexdigest()
    if spec.profile == "default" and spec.m == 2 and spec.k == 1 and spec.dtype == "int32" and spec.dist == "Uniform":
        anchor = metadata.get("inputs", {}).get(input_filename(spec))
        if anchor is not None:
            if not isinstance(anchor, dict):
                raise InputError("Invalid frozen input entry")
            anchor_sha = _required_digest(anchor.get("sha256"), "frozen input sha256")
            if anchor_sha != expected_sha:
                raise InputError("Default projection does not match the frozen input bytes")
            anchor_file = store / "inputs" / input_filename(spec)
            if anchor_file.exists() and digest(anchor_file) != anchor_sha:
                raise InputError("Frozen input file has been modified")
    output_dir = Path(output_dir) if output_dir is not None else DEFAULT_OUTPUT / spec.profile
    output = output_dir.resolve() / input_filename(spec)
    sidecar = manifest_path(output)
    protected = {master.resolve(), (store / "manifest.json").resolve(),
                 (store / "master-source-row-ids.csv").resolve()}
    if metadata.get("source_path"):
        protected.add(Path(metadata["source_path"]).resolve())
    if source_table is not None:
        protected.add(Path(source_table).resolve())
    if output.resolve() in protected or sidecar.resolve() in protected:
        raise InputError("An output path cannot replace the source table or frozen master artifacts")
    expected = {
        "schema_version": SCHEMA_VERSION, "generator": GENERATOR, "source_table": "TPC-H LINEITEM",
        "profile": spec.profile, "rows": spec.rows, "cols": spec.m, "keys": spec.k,
        "dtype": spec.dtype, "distribution": spec.dist, "seed": spec.seed,
        "config": spec.configuration(), "source_sha256": metadata["source_sha256"],
        "master_sha256": metadata["master_sha256"], "master_rows": metadata["master_rows"],
        "key_sampling_seed": metadata["key_sampling_seed"], "row_selection_seed": metadata["row_selection_seed"],
        "source_columns": list(columns), "sort_key_columns": list(columns[:spec.k]),
        "payload_columns": list(columns[spec.k:]), "column_encodings": {c: ENCODINGS[c] for c in columns},
        "transformation": transformation, "scale_factor": metadata.get("scale_factor"),
        "source_provenance": metadata.get("source_provenance", "Frozen dbgen master manifest"),
        "source_role": metadata.get("source_role", "frozen-dbgen-master"),
        "master_indices_sha256": hashlib.sha256(json.dumps(indices).encode("ascii")).hexdigest(),
        "sha256": expected_sha,
    }
    if output.exists() or sidecar.exists():
        if not overwrite:
            if not output.is_file() or not sidecar.is_file():
                raise InputError(f"Incomplete existing cache at {output}. Preserve it or explicitly use --overwrite.")
            existing = _read_json(sidecar)
            mismatched = [name for name, value in expected.items() if existing.get(name) != value]
            if mismatched or digest(output) != expected_sha:
                raise InputError(f"Existing cache failed verification ({', '.join(mismatched) or 'data hash'}): "
                                 f"{output}. Refusing to overwrite without --overwrite.")
            return {"path": str(output), "manifest": existing, "status": "EXISTING_INPUT_VERIFIED"}
    elif verify_existing:
        raise InputError(f"Requested existing input does not exist: {output}")
    if verify_existing:
        raise InputError("Read-only verification cannot replace an existing cache")
    output.parent.mkdir(parents=True, exist_ok=True)
    expected.update(source_path=metadata.get("source_path"), master_path=str(master),
                    tpch_store=str(store), output_path=str(output))
    temporary, output_created = [], False
    try:
        with _temporary(output, binary=True) as stream:
            temporary.append(Path(stream.name))
            for line in _lines(rows, indices):
                stream.write(line)
        with _temporary(sidecar) as stream:
            temporary.append(Path(stream.name))
            json.dump(expected, stream, indent=2, sort_keys=True)
            stream.write("\n")
        _install_file(temporary[0], output, overwrite)
        output_created = not overwrite
        _install_file(temporary[1], sidecar, overwrite)
    except Exception:
        if output_created:
            output.unlink(missing_ok=True)
        raise
    finally:
        for path in temporary:
            path.unlink(missing_ok=True)
    return {"path": str(output), "manifest": expected, "status": "INPUT_GENERATED"}
