"""One experiment matrix for every execution environment."""

from dataclasses import asdict, dataclass
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAPER = json.loads((ROOT / "experiments/paper.json").read_text())
SYSTEMS = json.loads((ROOT / "experiments/systems.json").read_text())
SUITES = tuple(PAPER["suites"])
GROUPS = {"bitonic": "R1_algorithm_bitonic", "odd_even": "R2_algorithm_odd_even", "radix": "R3_algorithm_radix", "quick": "R4_algorithm_quick"}


@dataclass(frozen=True)
class Case:
    suite: str
    system: str
    algorithm: str
    power: int = 16
    columns: int = 2
    keys: int = 1
    dtype: str = "int32"
    distribution: str = "Uniform"
    security: str = "SH"
    network: str = "LAN"
    source_profile: str = "base"

    @property
    def rows(self):
        return 1 << self.power

    @property
    def parties(self):
        return SYSTEMS[self.system][self.security]

    @property
    def bits(self):
        return {"int8": 8, "int32": 32, "int64": 64}[self.dtype]

    @property
    def effective_key_bits(self):
        return max(self.bits, SYSTEMS[self.system].get("minimum_key_bits", self.bits))

    @property
    def distribution_name(self):
        return self.distribution.split("_", 1)[0]

    @property
    def unsupported_reason(self):
        info = SYSTEMS[self.system]
        if self.keys > info.get("max_keys", self.keys):
            return "The supplied native harness exposes one sorting key"
        if self.dtype not in info.get("input_types", [self.dtype]):
            return "The supplied native harness represents each column as a 32-bit integer"
        return None

    @property
    def input_profile(self):
        return "key-width" if self.suite == "key-width" else "default"

    @property
    def native_algorithm(self):
        return "batcher" if self.system == "MP-SPDZ" and self.algorithm == "odd_even" else self.algorithm

    @property
    def group(self):
        if self.suite in ("algorithms", "wan"):
            return GROUPS[self.algorithm]
        if self.suite == "malicious":
            return "R10_protocol_malicious" if self.security == "MAL" else "R10_protocol_semi_honest"
        if self.suite == "distribution":
            if self.distribution.startswith("Zipf"):
                return "R9_zipf"
            if self.distribution.startswith("HighRepeat"):
                return "R9_high_repeat"
            return "R9_partially_sorted"
        return {"table-size": "R5_rows", "key-count": "R6_keys", "payload": "R7_payloads", "key-width": "R8_key_type"}[self.suite]

    @property
    def identifier(self):
        return f"{self.suite}-{self.system}-{self.algorithm}-n{self.rows}-m{self.columns}-k{self.keys}-{self.dtype}-{self.distribution}-{self.security}-{self.network}"

    def as_dict(self):
        return {**asdict(self), "id": self.identifier, "rows": self.rows, "parties": self.parties, "bits": self.bits, "effective_key_bits": self.effective_key_bits, "group": self.group, "native_algorithm": self.native_algorithm, "input_profile": self.input_profile, "supported": self.unsupported_reason is None, "unsupported_reason": self.unsupported_reason}


def normalize_systems(names):
    if not names or names == ["all"]:
        return list(SYSTEMS)
    aliases = {key.lower(): key for key in SYSTEMS}
    aliases.update({value["name"].lower(): key for key, value in SYSTEMS.items()})
    result = []
    for name in names:
        if name.lower() not in aliases:
            raise ValueError(f"Unknown system: {name}")
        canonical = aliases[name.lower()]
        if canonical not in result:
            result.append(canonical)
    return result


def build_cases(suites=None, systems=None, scale="paper", network=None, algorithms=None):
    if scale not in ("paper", "smoke"):
        raise ValueError("Scale must be paper or smoke")
    selected = list(SUITES) if not suites or suites == ["all"] else suites
    if any(suite not in SUITES for suite in selected):
        raise ValueError("Unknown experiment suite")
    result = []
    power = PAPER["default_power"] if scale == "paper" else 8
    for suite in selected:
        for system in normalize_systems(systems):
            info = SYSTEMS[system]
            for algorithm in info["algorithms"]:
                if algorithms and algorithm not in algorithms:
                    continue
                default_type = info.get("fixed_key_type", "int32")
                options = [{}]
                if suite == "algorithms":
                    options = [{"power": p} for p in (PAPER["algorithm_powers"] if scale == "paper" else [8])]
                elif suite == "table-size":
                    options = [{"power": p} for p in (PAPER["table_size_powers"] if scale == "paper" else [8, 10])]
                elif suite == "key-count":
                    options = [{"columns": k, "keys": k} for k in (PAPER["key_counts"] if scale == "paper" else [1, 2])]
                elif suite == "payload":
                    options = [{"columns": 1 + p} for p in (PAPER["payload_counts"] if scale == "paper" else [1, 2])]
                elif suite == "key-width":
                    types = [info["fixed_key_type"]] if "fixed_key_type" in info else PAPER["key_types"]
                    options = [{"dtype": dtype} for dtype in types]
                elif suite == "distribution":
                    if scale == "paper":
                        distributions = [f"HighRepeat_r{x}" for x in PAPER["repeat_percentages"]]
                        distributions += [f"PartiallySorted_s{x}" for x in PAPER["sorted_percentages"]]
                        distributions += ["Zipf_z" + str(x).replace(".", "p") for x in PAPER["zipf_exponents"]]
                    else:
                        distributions = ["HighRepeat_r50", "PartiallySorted_s50", "Zipf_z1p2"]
                    options = [{"distribution": distribution} for distribution in distributions]
                elif suite == "wan":
                    options = [{"network": condition, "source_profile": "native_checked" if system == "orq" else "base"} for condition in ("LAN", "WAN")]
                elif suite == "malicious":
                    if "MAL" not in info:
                        continue
                    options = [{"security": security, "source_profile": "native_checked" if system == "orq" else "base"} for security in ("SH", "MAL")]
                for option in options:
                    case = Case(**{"suite": suite, "system": system, "algorithm": algorithm, "power": power, "dtype": default_type, **option})
                    if network is None or case.network == network:
                        result.append(case)
    return result


def input_parameters(case):
    distribution = case.distribution
    extra = []
    if distribution.startswith("HighRepeat_r"):
        extra = ["--repeat_fraction", str(int(distribution.split("_r")[1]) / 100)]
        distribution = "HighRepeat"
    elif distribution.startswith("PartiallySorted_s"):
        extra = ["--sorted_fraction", str(int(distribution.split("_s")[1]) / 100)]
        distribution = "PartiallySorted"
    elif distribution.startswith("Zipf_z"):
        extra = ["--zipf_factor", distribution.split("_z")[1].replace("p", ".")]
        distribution = "Zipf"
    return [str(case.power), str(case.columns), "-k", str(case.keys), "--dtype", case.dtype, "--dist", distribution, "--profile", case.input_profile, "--seed", str(PAPER["seed"]), *extra]
