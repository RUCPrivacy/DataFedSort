#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path


def main():
    if len(sys.argv) not in (7, 8):
        raise SystemExit("usage: write_config.py OUT PID P0 P1 P2 JIFF_LIB [JIFF_SERVER_PORT]")

    out = Path(sys.argv[1])
    pid = int(sys.argv[2])
    p0, p1, p2 = sys.argv[3], sys.argv[4], sys.argv[5]
    jiff_lib = sys.argv[6]
    server_port = int(sys.argv[7]) if len(sys.argv) == 8 else int(os.environ.get("CONGREGATION_JIFF_SERVER_PORT", "9004"))

    party_names = {1: "party_one", 2: "party_two", 3: "party_three"}
    config = {
        "general": {
            "workflow_name": "demo",
            "pid": pid,
            "all_pids": [1, 2, 3],
            "data_path": f"{party_names[pid]}/data/",
            "code_path": f"{party_names[pid]}/code/",
            "delimiter": ",",
            "use_floats": False,
        },
        "network": {
            "parties": [
                f"1:{p0}:9001",
                f"2:{p1}:9002",
                f"3:{p2}:9003",
            ]
        },
        "jiff": {
            "jiff_lib_path": jiff_lib,
            "server_ip": p0,
            "server_port": server_port,
            "server_pid": 1,
            "zp": 16777729,
            "extensions": {
                "fixed_point": {
                    "use": False,
                    "decimal_digits": 1,
                    "integer_digits": 1,
                },
                "negative_number": {"use": False},
                "big_number": {"use": False},
            },
        },
    }

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(config, indent=2) + "\n")


if __name__ == "__main__":
    main()
