"""Validate preparation control flow using small, explicitly labeled fixtures."""
import argparse
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data"))
from prepare_tpch import materialize_defaults, main, release_version, resolve_generator
from tpch_inputs import InputError, _build_master, digest
from test_data_inputs import write_source


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)

    def test_release_header_records_actual_version_or_none(self):
        header = self.root / "release.h"
        self.assertIsNone(release_version(header))
        header.write_text("#define VERSION 3\n#define RELEASE 0\n#define PATCH 1\n#define BUILD 2\n")
        self.assertEqual(release_version(header), "3.0.1.2")
        header.write_text("#define VERSION 3\n")
        self.assertIsNone(release_version(header))

    def test_external_generator_requires_distribution_and_hashes_both_files(self):
        executable = self.root / "dbgen"
        executable.write_bytes(b"unit-test-executable-placeholder")
        args = argparse.Namespace(dbgen_dir=None, dbgen=executable,
                                  dists=None, release_header=None)
        with self.assertRaisesRegex(InputError, "both exist"):
            resolve_generator(args, self.root)
        distribution = self.root / "dists.dss"
        distribution.write_bytes(b"unit-test-distribution-placeholder")
        actual, dist, metadata = resolve_generator(args, self.root)
        self.assertEqual(actual, executable.resolve())
        self.assertEqual(dist, distribution.resolve())
        self.assertEqual(metadata["binary_sha256"], digest(executable))
        self.assertEqual(metadata["dists_sha256"], digest(distribution))
        self.assertIsNone(metadata["release"])

    def test_preparation_without_official_tools_has_no_random_fallback(self):
        store = self.root / "store"
        self.assertEqual(main(["--store", str(store), "--powers", "4"]), 1)
        self.assertFalse((store / "manifest.json").exists())
        self.assertFalse((store / "source/lineitem.tbl").exists())

    def test_default_anchors_and_reference_are_derived_from_complete_rows(self):
        source = self.root / "lineitem.tbl"
        write_source(source)
        store = self.root / "store"
        _build_master(source, store, 42)
        metadata = materialize_defaults(store, [4, 8], 42)
        self.assertIsNone(metadata["scale_factor"])
        self.assertEqual(len(metadata["inputs"]), 2)
        small = metadata["inputs"]["data_2pow4_m2_k1_int32_Uniform.txt"]
        large = metadata["inputs"]["data_2pow8_m2_k1_int32_Uniform.txt"]
        self.assertEqual(Path(small["path"]).read_bytes().splitlines(),
                         Path(large["path"]).read_bytes().splitlines()[:16])
        rows = [tuple(map(int, line.split())) for line in Path(small["path"]).read_text().splitlines()]
        reference = [tuple(map(int, line.split())) for line in Path(small["reference_path"]).read_text().splitlines()]
        self.assertEqual(reference, sorted(rows))
        self.assertEqual(small["source_profile"], "U32")
        self.assertEqual(small["source_columns"], ["L_ORDERKEY", "L_PARTKEY"])
        self.assertEqual(small["reference_sha256"], digest(Path(small["reference_path"])))
        Path(small["reference_path"]).write_bytes(b"tampered\n")
        with self.assertRaisesRegex(InputError, "differs"):
            materialize_defaults(store, [4], 42)


if __name__ == "__main__":
    unittest.main()
