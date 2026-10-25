"""Small LINEITEM-format fixtures only. These are not experimental TPC-H results."""
from collections import Counter
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data"))
from tpch_inputs import (InputError, InputSpec, MASTER_NAME, _encode, digest,
                         generate_input, input_filename, manifest_path)


def fixture_line(key, line_number=1, quantity=None):
    fields = [str(key), str(1000 + key + line_number), str(100 + key),
              str(line_number), f"{quantity or (key % 50 + 1)}.00", "100.10", "0.04",
              "0.05", "N", "O", "1996-03-13", "1996-02-12", "1996-03-22",
              "DELIVER IN PERSON", "AIR", "test fixture"]
    return "|".join(fields) + "|\n"


def write_source(path, groups=300, quantity=None):
    lines = []
    starts, counts = [], []
    for group in range(groups):
        starts.append(len(lines) + 1)
        count = group % 3 + 1
        counts.append(count)
        for line_number in range(1, count + 1):
            lines.append(fixture_line(4 * (group + 1), line_number, quantity))
    path.write_text("".join(lines), encoding="ascii", newline="\n")
    return lines, starts, counts


def read_relation(path):
    return [tuple(map(int, line.split())) for line in Path(path).read_text().splitlines()]


class TpchInputTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"FES_TPCH_TABLE": "", "FES_TPCH_STORE": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.base = Path(self.directory.name)
        self.source = self.base / "lineitem.tbl"
        self.lines, self.starts, self.counts = write_source(self.source)
        self.store = self.base / "store"
        self.output = self.base / "output"

    def tearDown(self):
        self.directory.cleanup()

    def generate(self, spec=None, **kwargs):
        return generate_input(spec or InputSpec(8, 2), source_table=self.source,
                              tpch_store=self.store, output_dir=self.output, **kwargs)

    def test_master_matches_prepare_algorithm_and_nested_prefixes(self):
        result = self.generate()
        groups = random.Random(42).sample(range(len(self.starts)), len(self.starts))
        row_rng = random.Random(43)
        selected = [self.starts[g] + row_rng.randrange(self.counts[g]) for g in groups]
        reference_master = "".join(self.lines[line_id - 1] for line_id in selected).encode("ascii")
        self.assertEqual((self.store / MASTER_NAME).read_bytes(), reference_master)
        smaller = self.generate(InputSpec(4, 2))
        self.assertEqual(Path(smaller["path"]).read_bytes().splitlines(),
                         Path(result["path"]).read_bytes().splitlines()[:16])
        relation = read_relation(result["path"])
        self.assertEqual(len({row[0] for row in relation}), 256)
        manifest = result["manifest"]
        self.assertIsNone(manifest["scale_factor"])
        self.assertEqual(manifest["source_role"], "small-source-test-only")
        self.assertEqual(manifest_path(result["path"]).name, Path(result["path"]).name + ".manifest.json")

    def test_key_width_has_identical_values_across_types(self):
        outputs = []
        for dtype in ("int8", "int32", "int64"):
            result = self.generate(InputSpec(8, 2, dtype=dtype, profile="key-width"))
            outputs.append(Path(result["path"]).read_bytes())
            self.assertEqual(result["manifest"]["source_columns"], ["L_QUANTITY", "L_LINENUMBER"])
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[1], outputs[2])
        self.assertTrue(all(1 <= value <= 50 for value, _ in read_relation(result["path"])))

    def test_default_int8_rejected_and_key_width_never_clipped(self):
        with self.assertRaisesRegex(InputError, "key-width"):
            self.generate(InputSpec(8, 2, dtype="int8"))
        self.assertFalse(self.store.exists())
        write_source(self.source, quantity=128)
        with self.assertRaisesRegex(InputError, "outside int8 range"):
            self.generate(InputSpec(8, 2, dtype="int8", profile="key-width"))
        self.assertFalse(self.output.exists())

    def test_numeric_column_map_and_exact_decimal_date_encoding(self):
        result = self.generate(InputSpec(4, 11, k=8))
        row = read_relation(result["path"])[0]
        self.assertEqual(result["manifest"]["sort_key_columns"][-1], "L_TAX")
        self.assertEqual(row[5:8], (10010, 4, 5))
        self.assertEqual(row[8], (date(1996, 3, 13) - date(1970, 1, 1)).days)
        self.assertEqual(row[9], (date(1996, 2, 12) - date(1970, 1, 1)).days)
        fields = self.lines[0].strip().split("|")
        fields[4] = "1.000000000000000000000000001"
        with self.assertRaises(InputError):
            _encode(fields, "L_QUANTITY")
        fields[5] = "100.101"
        with self.assertRaises(InputError):
            _encode(fields, "L_EXTENDEDPRICE")

    def test_distributions_keep_entire_row_associations(self):
        original = read_relation(self.generate(InputSpec(8, 9))["path"])
        for dist in ("Zipf", "HighRepeat", "PartiallySorted"):
            with self.subTest(dist=dist):
                result = self.generate(InputSpec(8, 9, dist=dist, sorted_fraction=0.75))
                rows = read_relation(result["path"])
                self.assertEqual(len(rows), 256)
                self.assertTrue(set(rows).issubset(set(original)))
                if dist == "PartiallySorted":
                    self.assertEqual(Counter(rows), Counter(original))
                    self.assertEqual(rows[:192], sorted(original[:192], key=lambda row: row[:1]))
                    self.assertEqual(rows[192:], original[192:])
                elif dist == "HighRepeat":
                    self.assertEqual(Counter(rows).most_common(1)[0][1], 128)
                else:
                    self.assertGreater(Counter(rows).most_common(1)[0][1], 100)

    def test_cache_and_manifest_tampering_is_rejected_without_overwrite(self):
        result = self.generate()
        path = Path(result["path"])
        good = path.read_bytes()
        path.write_bytes(b"1 2\n")
        sidecar = manifest_path(path)
        forged = json.loads(sidecar.read_text())
        forged["sha256"] = digest(path)
        sidecar.write_text(json.dumps(forged))
        with self.assertRaisesRegex(InputError, "failed verification"):
            self.generate()
        self.assertEqual(path.read_bytes(), b"1 2\n")
        self.generate(overwrite=True)
        self.assertEqual(path.read_bytes(), good)
        sidecar_data = json.loads(sidecar.read_text())
        sidecar_data["cols"] = 9
        sidecar.write_text(json.dumps(sidecar_data))
        with self.assertRaisesRegex(InputError, "cols"):
            self.generate(verify_existing=True)

    def test_verify_is_read_only_and_raw_source_optional_with_store(self):
        result = self.generate()
        self.source.unlink()
        before = {p.relative_to(self.base): (p.read_bytes(), p.stat().st_mtime_ns)
                  for p in self.base.rglob("*") if p.is_file()}
        result = generate_input(InputSpec(8, 2), tpch_store=self.store,
                                output_dir=self.output, verify_existing=True)
        after = {p.relative_to(self.base): (p.read_bytes(), p.stat().st_mtime_ns)
                 for p in self.base.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(result["status"], "EXISTING_INPUT_VERIFIED")

    def test_missing_source_read_only_missing_store_and_small_limit_fail(self):
        absent = self.base / "missing"
        with self.assertRaisesRegex(InputError, "No verified"):
            generate_input(InputSpec(8, 2), tpch_store=absent, output_dir=self.output)
        self.assertFalse(absent.exists())
        with self.assertRaisesRegex(InputError, "Read-only"):
            self.generate(verify_existing=True)
        self.assertFalse(self.store.exists())
        with self.assertRaisesRegex(InputError, "Small fixtures"):
            self.generate(InputSpec(9, 2))
        self.assertFalse(self.output.exists())

    def test_unmanifested_old_cache_and_source_collision_are_preserved(self):
        self.output.mkdir()
        old = self.output / input_filename(InputSpec(4, 2))
        old.write_bytes(b"unverified existing input must stay untouched\n")
        with self.assertRaisesRegex(InputError, "Incomplete existing cache"):
            self.generate(InputSpec(4, 2))
        self.assertEqual(old.read_bytes(), b"unverified existing input must stay untouched\n")
        # Explicit --overwrite may repair cache files, never the original source.
        collision_source = self.base / input_filename(InputSpec(4, 2))
        collision_source.write_bytes(self.source.read_bytes())
        with self.assertRaisesRegex(InputError, "cannot replace"):
            generate_input(InputSpec(4, 2), source_table=collision_source, tpch_store=self.store,
                           output_dir=self.base, overwrite=True)
        self.assertEqual(collision_source.read_bytes(), self.source.read_bytes())

    def test_master_and_explicit_source_tampering_fail_even_with_overwrite(self):
        self.generate()
        self.source.write_text(self.source.read_text().replace("100.10", "100.11", 1), encoding="ascii")
        with self.assertRaisesRegex(InputError, "source hash"):
            self.generate(overwrite=True)
        master = self.store / MASTER_NAME
        master.write_bytes(master.read_bytes() + b"\n")
        with self.assertRaisesRegex(InputError, "master table"):
            self.generate(overwrite=True)

    def test_no_source_fallback_and_default_cli_dtype_and_environment(self):
        cli = Path(__file__).resolve().parents[1] / "data" / "generateData.py"
        with patch.dict("os.environ", {"FES_TPCH_TABLE": str(self.source), "FES_TPCH_STORE": str(self.store)}):
            generated = subprocess.run([sys.executable, str(cli), "4", "2", "--output-dir", str(self.output)],
                                       capture_output=True, text=True)
            self.assertEqual(generated.returncode, 0, generated.stderr)
            expected_path = self.output / "data_2pow4_m2_k1_int32_Uniform.txt"
            self.assertTrue(expected_path.is_file())
            verified = subprocess.run([sys.executable, str(cli), "4", "2", "--output-dir", str(self.output),
                                       "--verify-existing"], capture_output=True, text=True)
            self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertIn("EXISTING_INPUT_VERIFIED", verified.stdout)

    def test_reuse_frozen_store_and_both_default_byte_anchors(self):
        # A hand-built frozen-store TEST fixture, not real dbgen output.
        self.store.mkdir()
        master = self.store / MASTER_NAME
        with master.open("w", encoding="ascii", newline="\n") as out:
            for key in range(1, 65537):
                out.write(fixture_line(key))
        row_ids = self.store / "master-source-row-ids.csv"
        row_ids.write_text("master_index,source_line_id,L_ORDERKEY,L_LINENUMBER\n", encoding="ascii")
        metadata = {
            "schema_version": 1, "source_table": "TPC-H LINEITEM", "source_path": "/not-present/test-fixture.tbl",
            "source_sha256": hashlib.sha256(b"test-fixture-not-dbgen").hexdigest(),
            "scale_factor": None, "source_role": "test-fixture", "source_provenance": "TEST FIXTURE ONLY",
            "key_sampling_seed": 42, "row_selection_seed": 43, "master_rows": 65536,
            "master_sha256": digest(master), "master_row_ids_sha256": digest(row_ids), "inputs": {},
        }
        (self.store / "inputs").mkdir()
        for exponent in (8, 16):
            anchor = self.store / "inputs" / input_filename(InputSpec(exponent, 2))
            with anchor.open("w", encoding="ascii", newline="\n") as out:
                for key in range(1, (1 << exponent) + 1):
                    out.write(f"{key} {1001 + key}\n")
            metadata["inputs"][anchor.name] = {"path": str(anchor), "sha256": digest(anchor)}
        metadata_file = self.store / "manifest.json"
        metadata_file.write_text(json.dumps(metadata), encoding="ascii")
        for exponent in (8, 16):
            result = generate_input(InputSpec(exponent, 2), tpch_store=self.store, output_dir=self.output)
            self.assertEqual(Path(result["path"]).read_bytes(),
                             (self.store / "inputs" / input_filename(InputSpec(exponent, 2))).read_bytes())
            self.assertEqual(result["manifest"]["source_role"], "test-fixture")
        anchor.write_bytes(b"tampered\n")
        with self.assertRaisesRegex(InputError, "modified"):
            generate_input(InputSpec(16, 2), tpch_store=self.store, output_dir=self.output)


if __name__ == "__main__":
    unittest.main()
