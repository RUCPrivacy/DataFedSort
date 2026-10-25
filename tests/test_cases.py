"""Check the paper matrix, public aliases, and security-specific configurations."""

import unittest

from artifact.cases import build_cases, input_parameters, normalize_systems


class PaperCases(unittest.TestCase):
    def test_native_capabilities_are_visible_without_silent_case_removal(self):
        cases = build_cases(["key-count"], ["MP-SPDZ"])
        self.assertEqual(len(cases), 8)
        self.assertEqual(sum(case.unsupported_reason is not None for case in cases), 6)
        self.assertTrue(all(case.as_dict()["supported"] == (case.keys == 1) for case in cases))
        cases = build_cases(["key-width"], ["MASQUE"])
        self.assertTrue(all(case.unsupported_reason for case in cases if case.dtype == "int64"))
        self.assertTrue(all(case.effective_key_bits == 32 for case in cases if case.dtype == "int8"))

    def test_eighteen_implementations_at_two_algorithm_sizes(self):
        cases = build_cases(["algorithms"])
        self.assertEqual(len(cases), 36)
        self.assertEqual({case.power for case in cases}, {8, 20})
        self.assertEqual(len({(case.system, case.algorithm) for case in cases}), 18)

    def test_complete_one_factor_sweeps(self):
        self.assertEqual(len(build_cases(["table-size"])), 90)
        self.assertEqual(len(build_cases(["key-count"])), 72)
        self.assertEqual(len(build_cases(["payload"])), 72)
        self.assertEqual(len(build_cases(["distribution"])), 270)

    def test_secrecy_uses_only_real_fixed_width(self):
        cases = build_cases(["key-width"], ["Secrecy"])
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0].dtype, "int64")
        self.assertEqual(cases[0].input_profile, "key-width")

    def test_wan_is_a_paired_default_workload(self):
        cases = build_cases(["wan"])
        self.assertEqual(len(cases), 36)
        self.assertEqual({case.rows for case in cases}, {65536})
        self.assertEqual({(case.columns, case.keys, case.security) for case in cases}, {(2, 1, "SH")})
        self.assertEqual({case.network for case in cases}, {"LAN", "WAN"})

    def test_malicious_pair_counts_and_native_parties(self):
        cases = build_cases(["malicious"])
        self.assertEqual(len(cases), 16)
        self.assertEqual({case.system for case in cases}, {"orq", "mpc4j", "MP-SPDZ"})
        self.assertEqual({case.parties for case in cases if case.system == "orq" and case.security == "MAL"}, {4})
        self.assertTrue(all(case.network == "LAN" for case in cases))
        self.assertTrue(all(case.parties == 3 for case in cases if case.system != "orq"))

    def test_native_orq_profiles_do_not_mix_overlays(self):
        self.assertEqual({case.source_profile for case in build_cases(["wan", "malicious"], ["orq"])}, {"native_checked"})
        self.assertEqual({case.source_profile for case in build_cases(["table-size"], ["orq"])}, {"base"})

    def test_case_identifiers_unique(self):
        cases = build_cases(["all"])
        self.assertEqual(len({case.identifier for case in cases}), len(cases))

    def test_native_odd_even_alias(self):
        cases = build_cases(["algorithms"], ["MP-SPDZ"], algorithms=["odd_even"])
        self.assertTrue(all(case.native_algorithm == "batcher" for case in cases))

    def test_public_paper_names(self):
        self.assertEqual(normalize_systems(["SCQL", "Scape", "Conclave", "Alchemy"]), ["spu", "mpc4j", "congregation", "alchemy"])

    def test_distribution_parameters_preserve_row_transformations(self):
        cases = build_cases(["distribution"], ["orq"], algorithms=["quick"])
        zipped = next(case for case in cases if case.distribution == "Zipf_z1p2")
        repeated = next(case for case in cases if case.distribution == "HighRepeat_r90")
        self.assertIn("1.2", input_parameters(zipped))
        self.assertIn("0.9", input_parameters(repeated))


if __name__ == "__main__":
    unittest.main()
