#!/usr/bin/env python3
"""Validate a user-owned cluster configuration and emit quoted shell variables."""
import argparse
import json
import re
import shlex
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from artifact.configuration import load_config, ssh_arguments


def environment(path):
    config = load_config(path)
    hosts = config.get('workers', [])
    if len(hosts) != 4 or any(not isinstance(h, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', h) for h in hosts):
        raise ValueError('workers must contain four DNS hostnames or IPv4 addresses')
    if len(set(hosts)) != 4:
        raise ValueError('Each worker must have a separate hostname and network namespace')
    workspace = config.get('remote_workspace', '/work/datafedsort')
    if not isinstance(workspace, str) or not workspace.startswith('/') or not re.fullmatch(r'/[A-Za-z0-9_./-]+', workspace) or '..' in Path(workspace).parts or workspace.rstrip('/') in ('', '/tmp', '/home', '/root', '/work'):
        raise ValueError('remote_workspace must be a dedicated absolute path without spaces or dot-dot segments')
    user = config.get('ssh_user', '')
    if not re.fullmatch(r'[A-Za-z0-9_-]*', user):
        raise ValueError('ssh_user contains unsupported characters')
    options = ssh_arguments(config, hosts[0])[2:-1]
    timeout = int(config.get('timeout_seconds', 3600))
    if not 1 <= timeout <= 3600:
        raise ValueError('timeout_seconds must be between one and 3600')
    interface = config.get('interface', 'eth0')
    if not re.fullmatch(r'[A-Za-z0-9_.:-]+', interface):
        raise ValueError('interface contains unsupported characters')
    values = {f'P{i}_IP': host for i, host in enumerate(hosts)}
    values.update(SSH_USER=user, BENCH_REMOTE_WORK_DIR=workspace.rstrip('/'), TIMEOUT_S=str(timeout), BENCH_INTERFACE=interface,
                  BENCH_SSH_OPTIONS_JSON=json.dumps(options), BENCH_MPI_SSH_OPTIONS=shlex.join(options))
    for name, value in values.items():
        yield 'export ' + name + '=' + shlex.quote(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', type=Path)
    args = parser.parse_args()
    try:
        print('\n'.join(environment(args.config)))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        parser.exit(2, 'Invalid cluster configuration: ' + str(exc) + '\n')


if __name__ == '__main__':
    main()
