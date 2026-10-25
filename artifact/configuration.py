"""User-owned deployment settings and SSH command construction."""

import json
import os
from pathlib import Path
import re
import shlex
import subprocess


def load_config(path):
    path = Path(path).expanduser().resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    workers = data.get("workers", [])
    if len(workers) != 4 or len(set(workers)) != 4:
        raise ValueError("Configure four distinct worker hosts")
    if any(not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", host) for host in workers):
        raise ValueError("Worker hosts must be DNS hostnames or IPv4 addresses")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*", data.get("ssh_user", "")):
        raise ValueError("Configure an SSH user")
    remote = data.get("remote_workspace", "")
    if not remote.startswith("/") or ".." in Path(remote).parts or not re.fullmatch(r"/[A-Za-z0-9_./-]+", remote) or remote.rstrip("/") in ("", "/tmp", "/home", "/root", "/work"):
        raise ValueError("remote_workspace must be a dedicated absolute Linux path")
    options = data.get("ssh_options", ["BatchMode=yes", "ConnectTimeout=10"])
    if not isinstance(options, list) or not all(isinstance(option, str) and "\n" not in option for option in options):
        raise ValueError("ssh_options must be a list")
    timeout = data.get("timeout_seconds", 3600)
    if not isinstance(timeout, int) or not 1 <= timeout <= 3600:
        raise ValueError("timeout_seconds must be between 1 and 3600")
    data.update(ssh_options=options, timeout_seconds=timeout, config_path=str(path))
    return data


def ssh_arguments(config, host):
    command = ["ssh", "-n"]
    if config.get("ssh_key"):
        command += ["-i", os.path.expanduser(config["ssh_key"])]
    for option in config["ssh_options"]:
        command += ["-o", option]
    return [*command, f"{config['ssh_user']}@{host}"]


def remote_python(config, host, source, arguments=(), timeout=120):
    prefix = "sudo -n " if config.get("sudo", False) else ""
    command = prefix + "python3 - " + shlex.join(list(arguments))
    args = ssh_arguments(config, host)
    args.remove("-n")
    return subprocess.run([*args, command], input=source, text=True, capture_output=True, timeout=timeout, check=True)
