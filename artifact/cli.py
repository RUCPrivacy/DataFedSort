"""Prepare, inspect, and execute explicitly selected paper experiments."""

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timezone

from .cases import PAPER, ROOT, SUITES, SYSTEMS, build_cases, normalize_systems
from .configuration import load_config


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def add_selection(parser):
    parser.add_argument("--suite", nargs="+", choices=["all", *SUITES], default=["algorithms"])
    parser.add_argument("--systems", nargs="+", default=["all"], help="Paper names or adapter IDs")
    parser.add_argument("--algorithms", nargs="+", choices=["bitonic", "odd_even", "radix", "quick"])
    parser.add_argument("--scale", choices=["paper", "smoke"], default="paper")
    parser.add_argument("--network", choices=["LAN", "WAN"])


def selected_cases(args):
    return build_cases(args.suite, args.systems, args.scale, args.network, args.algorithms)


def generate_case_input(case, config):
    from data.tpch_inputs import InputSpec, generate_input
    distribution = case.distribution
    parameters = {"zipf_factor": 2.0, "sorted_fraction": 0.7, "repeat_fraction": 0.5}
    if distribution.startswith("Zipf_z"):
        parameters["zipf_factor"] = float(distribution.split("_z")[1].replace("p", "."))
        distribution = "Zipf"
    elif distribution.startswith("HighRepeat_r"):
        parameters["repeat_fraction"] = int(distribution.split("_r")[1]) / 100
        distribution = "HighRepeat"
    elif distribution.startswith("PartiallySorted_s"):
        parameters["sorted_fraction"] = int(distribution.split("_s")[1]) / 100
        distribution = "PartiallySorted"
    spec = InputSpec(case.power, case.columns, case.keys, case.dtype, distribution,
                     case.input_profile, PAPER["seed"], **parameters)
    store = Path(os.environ.get("FES_TPCH_STORE") or config.get("input_store", "workspace/data/tpch-sf1")).expanduser()
    if not store.is_absolute():
        store = ROOT / store
    output = ROOT / "workspace/data/generated/tpch-v1" / case.input_profile
    source = os.environ.get("FES_TPCH_TABLE")
    result = generate_input(spec, source_table=Path(source) if source else None, tpch_store=store, output_dir=output)
    return Path(result["path"]).resolve().relative_to(ROOT).as_posix(), result["manifest"], store


def case_command(case, args, input_file, run_id):
    return ["bash", str(ROOT / "runtime/run_case.sh"),
            "--system", case.system, "--group", case.group,
            "--algorithm", case.native_algorithm, "--power", str(case.power),
            "--columns", str(case.columns), "--keys", str(case.keys),
            "--dtype", case.dtype, "--distribution", case.distribution_name,
            "--bits", str(case.bits), "--security", case.security,
            "--source-profile", case.source_profile, "--input", input_file,
            "--benchmark-public-input", "--run-id", run_id, "--repeat", "1",
            "--config", str(Path(args.config).resolve())]


def run_experiments(args):
    cases = selected_cases(args)
    if not cases:
        raise ValueError("The selection contains no supported cases")
    if args.dry_run:
        print(json.dumps({"executed": False, "cases": [case.as_dict() for case in cases]}, indent=2))
        return 0
    if sys.platform == "win32":
        raise ValueError("Run native benchmarks in Linux, WSL2, or the supplied Linux containers; plan works on every platform")
    config = load_config(args.config)
    if len({case.network for case in cases}) > 1 and not args.manage_network:
        raise ValueError("A mixed LAN/WAN selection requires --manage-network; otherwise select --network LAN or WAN and configure it explicitly")
    identifier = args.run_id or "run-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}", identifier):
        raise ValueError("Invalid run ID")
    output = ROOT / "outputs" / identifier
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "plan.json", [case.as_dict() for case in cases])
    controller = None
    current_network = None
    outcomes = []
    try:
        for index, case in enumerate(cases):
            if case.unsupported_reason:
                outcomes.append({**case.as_dict(), "status": "UNSUPPORTED", "metrics": None})
                write_json(output / "outcomes.json", outcomes)
                print(f"[{index+1}/{len(cases)}] UNSUPPORTED {case.identifier}: {case.unsupported_reason}", flush=True)
                continue
            if controller is None:
                from .network import NetworkController
                controller = NetworkController(config)
            if current_network != case.network:
                if args.manage_network:
                    snapshot = controller.set_condition(case.network)
                else:
                    snapshot = controller.require_condition(case.network)
                write_json(output / f"network-{index:04d}.json", snapshot)
                current_network = case.network
            input_file, manifest, store = generate_case_input(case, config)
            case_run = f"{identifier}-{index:04d}"
            command = case_command(case, args, input_file, case_run)
            env = os.environ.copy()
            env.update(FES_TPCH_STORE=str(store), FES_NETWORK_CONDITION=case.network,
                       FES_INPUT_SHA256=manifest["sha256"], FES_PUBLIC_BENCHMARK="1")
            print(f"[{index+1}/{len(cases)}] {case.identifier}", flush=True)
            with (output / f"{index:04d}.log").open("w", encoding="utf-8") as stream:
                process = subprocess.run(command, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT)
            native_results = list((ROOT / "workspace/runs" / case_run / "cases").glob("*/result.json"))
            native = json.loads(native_results[0].read_text()) if len(native_results) == 1 else {}
            status = native.get("status", "FAILED")
            if process.returncode and status not in ("TIMEOUT", "OOM"):
                status = "FAILED"
            outcome = {**case.as_dict(), "status": status, "exit_code": process.returncode,
                       "input_sha256": manifest["sha256"], "native_run_id": case_run,
                       "metrics": native.get("metrics")}
            outcomes.append(outcome)
            write_json(output / "outcomes.json", outcomes)
            if status not in ("OK", "TIMEOUT", "OOM") and not args.keep_going:
                raise RuntimeError(f"Case failed; inspect outputs/{identifier}/{index:04d}.log")
    finally:
        if args.manage_network and controller is not None:
            write_json(output / "network-restored.json", controller.set_condition("LAN"))
    return 0 if all(outcome["status"] in ("OK", "TIMEOUT", "OOM") for outcome in outcomes) else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="List paper suites and supported native implementations")
    plan = commands.add_parser("plan", help="Print the complete selected case matrix without executing anything")
    add_selection(plan)
    plan.add_argument("--output", type=Path)
    run = commands.add_parser("run", help="Run the selected native sorting operators")
    add_selection(run)
    run.add_argument("--config", default="configs/cluster.local.json")
    run.add_argument("--run-id")
    run.add_argument("--manage-network", action="store_true", help="Apply and restore owned WAN rules for the selected cases")
    run.add_argument("--keep-going", action="store_true", help="Record failures and continue; never infer unexecuted timeouts")
    run.add_argument("--dry-run", action="store_true")
    prepare = commands.add_parser("prepare", help="Build explicitly selected backend profiles outside timed runs")
    prepare.add_argument("--systems", nargs="+", required=True)
    prepare.add_argument("--profile", choices=["base", "native_checked"], default="base")
    prepare.add_argument("--config", default="configs/cluster.local.json")
    network = commands.add_parser("network", help="Inspect, measure, apply, or restore the user-owned worker network")
    network.add_argument("action", choices=["inspect", "measure", "apply", "restore"])
    network.add_argument("--config", default="configs/cluster.local.json")
    check = commands.add_parser("check", help="Validate the public experiment matrix and an optional deployment file")
    check.add_argument("--config")
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            print(json.dumps({"suites": PAPER["suites"], "systems": SYSTEMS}, indent=2))
        elif args.command == "plan":
            cases = selected_cases(args)
            value = {"case_count": len(cases), "supported_case_count": sum(case.unsupported_reason is None for case in cases), "by_suite": dict(Counter(case.suite for case in cases)),
                     "cases": [case.as_dict() for case in cases]}
            if args.output:
                write_json(args.output, value)
            else:
                print(json.dumps(value, indent=2))
        elif args.command == "run":
            return run_experiments(args)
        elif args.command == "prepare":
            load_config(args.config)
            for system in normalize_systems(args.systems):
                subprocess.run(["bash", str(ROOT / "runtime/run_case.sh"), "--prepare", "--config", str(Path(args.config).resolve()),
                                "--system", system, "--source-profile", args.profile], check=True, cwd=ROOT)
        elif args.command == "network":
            from .network import NetworkController
            controller = NetworkController(load_config(args.config))
            action = {"inspect": controller.inspect, "measure": controller.measure,
                      "apply": lambda: controller.set_condition("WAN"), "restore": lambda: controller.set_condition("LAN")}[args.action]
            print(json.dumps(action(), indent=2))
        elif args.command == "check":
            cases = build_cases(["all"])
            assert len({case.identifier for case in cases}) == len(cases)
            assert len(build_cases(["wan"])) == 36 and len(build_cases(["malicious"])) == 16
            if args.config:
                load_config(args.config)
            print(json.dumps({"schema_valid": True, "paper_cases": len(cases), "native_execution_performed": False}))
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(f"DataFedSort error: {exc}", file=sys.stderr)
        return 1
    return 0
