"""Public-input guard and output-oracle tests without MPC runtime dependencies."""

import ast
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
SMPCACHE = ROOT / "systems/SMPCache/overlay/obliviousSort_lan_party.py"
ALCHEMY = ROOT / "systems/alchemy/overlay/sorting_micro_test.cpp"
MASQUE = ROOT / "systems/MASQUE/overlay/test_sorting_micro.cpp"


def load_python_helpers():
    """Load pure helpers without importing TensorFlow or Rosetta."""
    parsed = ast.parse(SMPCACHE.read_text(encoding="utf-8"))
    wanted = {"permit_public_input_verification", "validate_public_output"}
    parsed.body = [node for node in parsed.body
                   if isinstance(node, ast.FunctionDef) and node.name in wanted]
    namespace = {"os": os, "hashlib": hashlib, "np": np}
    exec(compile(parsed, str(SMPCACHE), "exec"), namespace)
    return namespace


class PublicInputGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helpers = load_python_helpers()

    def test_opening_requires_explicit_public_flag(self):
        with patch.dict(os.environ, {"FES_REVISION_BENCH": "1"}, clear=True):
            self.assertFalse(self.helpers["permit_public_input_verification"]("missing"))

    def test_public_flag_requires_verified_digest(self):
        for digest in ("", "0" * 63, "g" * 64):
            with self.subTest(digest=digest), patch.dict(
                    os.environ, {"FES_PUBLIC_BENCHMARK": "1", "FES_INPUT_SHA256": digest},
                    clear=True):
                with self.assertRaisesRegex(ValueError, "FES_INPUT_SHA256"):
                    self.helpers["permit_public_input_verification"]("missing")

    def test_guard_accepts_any_exact_authorized_input_and_rejects_changes(self):
        # This tiny table tests the guard only. It is not a production TPC-H source.
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "table.txt"
            source.write_bytes(b"2 7 90\n1 8 30\n2 3 10\n")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            with patch.dict(os.environ, {
                    "FES_PUBLIC_BENCHMARK": "1", "FES_INPUT_SHA256": digest.upper()},
                    clear=True):
                self.assertTrue(self.helpers["permit_public_input_verification"](source))
                source.write_bytes(source.read_bytes() + b"9 0 1\n")
                with self.assertRaisesRegex(ValueError, "does not match"):
                    self.helpers["permit_public_input_verification"](source)

    def test_oracle_allows_equal_key_permutation_and_checks_payloads(self):
        original = np.array([[2, 7, 90], [1, 8, 30], [2, 3, 10], [2, 7, 90]])
        output = original[[1, 2, 3, 0]]
        oracle = self.helpers["validate_public_output"]
        oracle(output, original, 0)
        damaged = output.copy()
        damaged[1, 2] += 1
        with self.assertRaisesRegex(ValueError, "Full tuples"):
            oracle(damaged, original, 0)
        with self.assertRaisesRegex(ValueError, "not sorted"):
            oracle(original, original, 0)
        with self.assertRaisesRegex(ValueError, "shape"):
            oracle(output[:-1], original, 0)

    def test_oracle_supports_a_different_sort_column(self):
        original = np.array([[12, 4], [8, 2], [9, 4], [1, 1]], dtype=np.float32)
        self.helpers["validate_public_output"](original[[3, 1, 2, 0]], original, 1)

    def test_public_guards_are_independent_of_fixed_fixture_hashes(self):
        for path in (MASQUE, ALCHEMY, SMPCACHE):
            with self.subTest(path=path.name):
                source = path.read_text(encoding="utf-8")
                self.assertIn("FES_PUBLIC_BENCHMARK", source)
                self.assertIn("FES_INPUT_SHA256", source)
                self.assertNotRegex(source, r'(?<![A-Za-z0-9])[a-f0-9]{64}(?![A-Za-z0-9])')

    def test_validation_stays_after_native_timing_and_counters(self):
        alchemy = ALCHEMY.read_text(encoding="utf-8")
        self.assertLess(alchemy.index("auto end_gates"), alchemy.index("validatePublicResult(manager_"))
        self.assertIn("manager_->andGateCount() != end_gates", alchemy)
        smpcache = SMPCACHE.read_text(encoding="utf-8")
        self.assertLess(smpcache.index("memory_monitor.stop()"), smpcache.index("rtt.SecureReveal(sorted_rtxdata)"))
        self.assertIn("sent_bytes={bytes_sent}", smpcache)
        masque = MASQUE.read_text(encoding="utf-8")
        self.assertLess(masque.index("exchange_plain_metrics(context.role"), masque.index("bc->PutOUTGate("))
        self.assertIn("MASQUE native sort supports exactly one 64-bit sort key", masque)


class AlchemyNativeRepresentationTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("g++"), "g++ is needed for native representation tests")
    def test_actual_cpp_encoding_and_comparison_helpers(self):
        source = ALCHEMY.read_text(encoding="utf-8")
        helpers = source[source.index("static int fieldBitCount("):
                         source.index("// Reveal only authorized public output labels")]
        test_main = r'''
#include <cassert>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <stdexcept>
#include <string>
enum class FieldType { BOOL, INT, LONG, FLOAT };
HELPERS
int main() {
    assert(fieldBitCount(FieldType::BOOL) == 1);
    assert(fieldBitCount(FieldType::INT) == 32);
    assert(fieldBitCount(FieldType::LONG) == 64);
    const auto i = FieldType::INT, l = FieldType::LONG, f = FieldType::FLOAT;
    assert(fieldLess(encodedPlainField("-2147483648", i), encodedPlainField("1", i), i));
    assert(!fieldLess(encodedPlainField("1", i), encodedPlainField("-1", i), i));
    assert(fieldLess(encodedPlainField("9223372036854775806", l),
                     encodedPlainField("9223372036854775807", l), l));
    assert(fieldLess(encodedPlainField("-9223372036854775808", l), encodedPlainField("0", l), l));
    assert(fieldLess(encodedPlainField("-1.25", f), encodedPlainField("0.5", f), f));
    assert(encodedPlainField("1", FieldType::BOOL) == 1);
    assert(encodedPlainField("0", FieldType::BOOL) == 0);
    bool rejected = false;
    try { encodedPlainField("nan", f); } catch (const std::runtime_error&) { rejected = true; }
    assert(rejected);
}
'''.replace("HELPERS", helpers)
        with tempfile.TemporaryDirectory() as directory:
            source_file = Path(directory) / "native_guard_test.cpp"
            executable = Path(directory) / ("native_guard_test.exe" if os.name == "nt" else "native_guard_test")
            source_file.write_text(test_main, encoding="utf-8")
            subprocess.run([shutil.which("g++"), "-std=c++17", str(source_file), "-o", str(executable)],
                           check=True, capture_output=True, text=True, timeout=30)
            subprocess.run([str(executable)], check=True, capture_output=True, timeout=10)


if __name__ == "__main__":
    unittest.main()
