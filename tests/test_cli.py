"""Offline commands must not require a dataset, SSH connection, or native build."""

from contextlib import redirect_stdout, redirect_stderr
import io
import json
import unittest
from argparse import Namespace

from artifact.cli import main, case_command
from artifact.configuration import load_config
from artifact.cases import ROOT, build_cases


class OfflineCLI(unittest.TestCase):
    def test_parameterized_distributions_use_the_native_canonical_name(self):
        for case in build_cases(["distribution"], ["Scape"]):
            command = case_command(case, Namespace(config="configs/cluster.example.json"), "workspace/example.txt", "example")
            self.assertEqual(command[command.index("--distribution") + 1], case.distribution_name)
            self.assertNotIn("_", command[command.index("--distribution") + 1])

    def test_plan_reports_requested_and_supported_cases_separately(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["plan", "--suite", "key-count", "--systems", "MP-SPDZ"]), 0)
        result = json.loads(out.getvalue())
        self.assertEqual((result["case_count"], result["supported_case_count"]), (8, 2))

    def test_dry_run_does_not_resolve_example_hosts(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["run", "--dry-run", "--suite", "malicious", "--systems", "ORQ"])
        self.assertEqual(code, 0)
        result = json.loads(out.getvalue())
        self.assertFalse(result["executed"])
        self.assertEqual(len(result["cases"]), 6)

    def test_paper_plan_contains_no_operational_shortcuts(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["plan", "--suite", "table-size", "--systems", "MP-SPDZ"])
        self.assertEqual(code, 0)
        result = json.loads(out.getvalue())
        self.assertEqual(result["case_count"], 10)
        self.assertEqual({case["power"] for case in result["cases"]}, {12, 14, 16, 18, 20})

    def test_machine_examples_have_valid_schema(self):
        for name in ("cluster.example.json", "local-containers.json"):
            self.assertEqual(len(load_config(ROOT / "configs" / name)["workers"]), 4)

    def test_unknown_system_returns_error(self):
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(["plan", "--systems", "unknown-system"]), 1)


if __name__ == "__main__":
    unittest.main()
