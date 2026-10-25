#!/usr/bin/env python3
"""Worker-side network calibration and owned WAN emulation. Standard library only."""
import base64
import ipaddress
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import time

WORKERS = set()
STATE = Path('/var/lib/datafedsort/network.json')
IFB = 'ifb_dfs_wan'
ROOT_HANDLE = '7a00:'
ALIAS = 'datafedsort-wan'

def command(args, timeout=30, check=True, input_text=None):
    try:
        proc = subprocess.run(args, input=input_text, text=True, capture_output=True, timeout=timeout)
        result = {'command': args, 'returncode': proc.returncode, 'stdout': proc.stdout, 'stderr': proc.stderr}
    except (OSError, subprocess.TimeoutExpired) as exc:
        result = {'command': args, 'returncode': 124 if isinstance(exc, subprocess.TimeoutExpired) else 127, 'stdout': '', 'stderr': str(exc)}
    if check and result['returncode']:
        raise RuntimeError(json.dumps(result))
    return result

def json_command(args):
    return json.loads(command(args)['stdout'])

def route_device(own, peers):
    assert own in WORKERS and set(peers) == WORKERS - {own}, 'Unexpected worker addresses'
    routes = [json_command(['ip', '-j', 'route', 'get', peer])[0] for peer in peers]
    devices = {r['dev'] for r in routes}
    assert len(devices) == 1, 'Worker peer routes must use one interface'
    assert all(r.get('prefsrc', r.get('src')) == own for r in routes), 'Unexpected private source address'
    return devices.pop(), routes

def qdisc_signature(qdiscs):
    return sorted([{'kind': q['kind'], 'handle': q.get('handle'), 'root': q.get('root', False), 'parent': q.get('parent'), 'options': q.get('options', {})} for q in qdiscs], key=lambda q: (str(q['parent']), str(q['handle']), q['kind']))

def safe_native_qdiscs(qdiscs):
    # Only kernel-created defaults may be temporarily replaced. Unknown/custom
    # rules are reported, not reset. Deleting our root must reproduce this snapshot.
    roots = [q for q in qdiscs if q.get('root')]
    return len(roots) == 1 and all(q.get('handle') == '0:' and q['kind'] in {'mq', 'fq_codel', 'pfifo_fast', 'fq', 'noqueue'} for q in qdiscs)

def inspect(own, peers):
    result = {'hostname': socket.gethostname(), 'kernel': os.uname().release, 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(), 'own_ip': own, 'cpu_count': os.cpu_count(), 'os_release': Path('/etc/os-release').read_text(), 'mem_total_kib': int(re.search(r'MemTotal:\s+(\d+)', Path('/proc/meminfo').read_text()).group(1)), 'tools': {name: shutil.which(name) for name in ['ip', 'tc', 'ping', 'iperf3', 'ethtool', 'modprobe']}, 'wan_state': json.loads(STATE.read_text()) if STATE.exists() else None}
    if shutil.which('ip'):
        dev, routes = route_device(own, peers)
        result.update(device=dev, routes=routes, addresses=json_command(['ip', '-j', 'address', 'show', 'dev', dev]))
        if shutil.which('tc'):
            result['qdiscs'] = json_command(['tc', '-j', '-s', 'qdisc', 'show', 'dev', dev])
            # Ubuntu's mq class dumper can emit text even with -j. Preserve the
            # complete command output rather than assume this field is JSON.
            result['classes'] = command(['tc', '-j', '-s', 'class', 'show', 'dev', dev])
            result['filters'] = command(['tc', '-j', 'filter', 'show', 'dev', dev, 'ingress'], check=False)
            links = command(['ip', '-j', 'link', 'show', 'dev', IFB], check=False)
            result['ifb_links'] = json.loads(links['stdout']) if links['returncode'] == 0 else []
            result['ifb_qdiscs'] = json_command(['tc', '-j', 'qdisc', 'show', 'dev', IFB]) if result['ifb_links'] else []
        if shutil.which('ethtool'):
            result['offloads'] = command(['ethtool', '-k', dev], check=False)
    return result

def wan_commands(dev, peers, delay_ms):
    assert re.fullmatch(r'[A-Za-z0-9_.:-]+', dev)
    assert set(peers) <= WORKERS and len(peers) == 3
    assert 0 <= delay_ms <= 25
    commands = [
        ['modprobe', 'ifb'], ['ip', 'link', 'add', IFB, 'type', 'ifb'],
        ['ip', 'link', 'set', 'dev', IFB, 'alias', ALIAS], ['ip', 'link', 'set', 'dev', IFB, 'up'],
        ['tc', 'qdisc', 'replace', 'dev', dev, 'root', 'handle', ROOT_HANDLE, 'htb', 'default', '30'],
        ['tc', 'class', 'add', 'dev', dev, 'parent', ROOT_HANDLE, 'classid', '7a00:1', 'htb', 'rate', '100gbit', 'ceil', '100gbit', 'quantum', '1514'],
        ['tc', 'class', 'add', 'dev', dev, 'parent', '7a00:1', 'classid', '7a00:10', 'htb', 'rate', '100mbit', 'ceil', '100mbit', 'burst', '64k', 'cburst', '64k', 'quantum', '1514'],
        ['tc', 'class', 'add', 'dev', dev, 'parent', '7a00:1', 'classid', '7a00:30', 'htb', 'rate', '100gbit', 'ceil', '100gbit', 'quantum', '1514'],
        ['tc', 'qdisc', 'add', 'dev', dev, 'parent', '7a00:10', 'handle', '7a10:', 'bfifo', 'limit', '16777216'],
        ['tc', 'qdisc', 'add', 'dev', IFB, 'root', 'handle', '7a20:', 'netem', 'delay', f'{delay_ms:.6f}ms', 'limit', '100000'],
        ['tc', 'qdisc', 'add', 'dev', dev, 'handle', 'ffff:', 'ingress'],
    ]
    for priority, port_match in [(10, 'dst_port'), (11, 'src_port')]:
        commands.append(['tc', 'filter', 'add', 'dev', dev, 'parent', ROOT_HANDLE, 'protocol', 'ip', 'pref', str(priority), 'flower', 'skip_hw', 'ip_proto', 'tcp', port_match, '22', 'classid', '7a00:30'])
        commands.append(['tc', 'filter', 'add', 'dev', dev, 'parent', 'ffff:', 'protocol', 'ip', 'pref', str(priority), 'flower', 'skip_hw', 'ip_proto', 'tcp', port_match, '22', 'action', 'pass'])
    for index, peer in enumerate(sorted(peers), 100):
        commands.append(['tc', 'filter', 'add', 'dev', dev, 'parent', ROOT_HANDLE, 'protocol', 'ip', 'pref', str(index), 'flower', 'skip_hw', 'dst_ip', peer + '/32', 'classid', '7a00:10'])
        commands.append(['tc', 'filter', 'add', 'dev', dev, 'parent', 'ffff:', 'protocol', 'ip', 'pref', str(index), 'flower', 'skip_hw', 'src_ip', peer + '/32', 'action', 'mirred', 'egress', 'redirect', 'dev', IFB])
    return commands

def save_state(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, indent=2))
    temporary.replace(STATE)

def restore(own, peers):
    if not STATE.exists():
        return {'status': 'no_owned_rules', 'inspection': inspect(own, peers)}
    state = json.loads(STATE.read_text())
    assert state['own_ip'] == own and state['peers'] == sorted(peers)
    dev = state['device']
    now = json_command(['tc', '-j', 'qdisc', 'show', 'dev', dev])
    current_boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if state.get('boot_id') and state['boot_id'] != current_boot:
        # Reboot removes our kernel objects but leaves the durable state file.
        # Archive that file only if the native qdiscs are back and IFB is absent.
        assert qdisc_signature(now) == qdisc_signature(state['before_qdiscs']), 'Rebooted worker has unexpected qdiscs, inspect before recovery'
        assert command(['ip', 'link', 'show', 'dev', IFB], check=False)['returncode'] != 0, 'Rebooted worker still has an IFB, inspect ownership'
        assert re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', state['boot_id']), 'Invalid saved boot identifier'
        archived = STATE.with_name('wan-before-reboot-' + state['boot_id'] + '.json')
        assert not archived.exists(), 'Do not overwrite an earlier reboot record'
        STATE.rename(archived)
        return {'status': 'stale_state_archived_after_reboot', 'archived_state': str(archived), 'old_boot_id': state['boot_id'], 'current_boot_id': current_boot, 'after_qdiscs': now}
    errors = []
    # Delete only objects this invocation successfully created.
    if 'ingress' in state['created']:
        ingress = [q for q in now if q['kind'] in {'ingress', 'clsact'}]
        if len(ingress) != 1 or ingress[0]['kind'] != 'ingress':
            errors.append('Ingress ownership changed; left untouched')
        else:
            result = command(['tc', 'qdisc', 'del', 'dev', dev, 'ingress'], check=False)
            if result['returncode']: errors.append(result)
            else:
                state['created'].remove('ingress')
                save_state(state)
    if 'root' in state['created']:
        roots = [q for q in now if q.get('root')]
        if len(roots) != 1 or roots[0].get('handle') != ROOT_HANDLE or roots[0]['kind'] != 'htb':
            errors.append('Root ownership changed; left untouched')
        else:
            result = command(['tc', 'qdisc', 'del', 'dev', dev, 'root'], check=False)
            if result['returncode']: errors.append(result)
            else:
                state['created'].remove('root')
                save_state(state)
    if 'ifb' in state['created'] and not errors:
        links = json_command(['ip', '-j', 'link', 'show', 'dev', IFB])
        if links[0].get('ifindex') != state.get('ifb_ifindex') or links[0].get('ifalias') not in {None, ALIAS}:
            errors.append('IFB ownership changed; left untouched')
        else:
            result = command(['ip', 'link', 'del', IFB], check=False)
            if result['returncode']: errors.append(result)
            else:
                state['created'].remove('ifb')
                save_state(state)
    restored = json_command(['tc', '-j', 'qdisc', 'show', 'dev', dev])
    if qdisc_signature(restored) != qdisc_signature(state['before_qdiscs']):
        errors.append('Native qdisc signature differs from saved snapshot; inspect before LAN runs')
    if errors:
        state.update(phase='restore_failed', restore_errors=errors)
        save_state(state)
        raise RuntimeError(json.dumps(state))
    STATE.unlink()
    return {'status': 'restored', 'before_qdiscs': state['before_qdiscs'], 'after_qdiscs': restored}

def apply(own, peers, delay_ms):
    assert not STATE.exists(), 'Already shaped or pending recovery; restore first'
    dev, _ = route_device(own, peers)
    before = json_command(['tc', '-j', 'qdisc', 'show', 'dev', dev])
    assert safe_native_qdiscs(before), 'Custom qdiscs detected; report them rather than overwrite'
    assert command(['ip', 'link', 'show', 'dev', IFB], check=False)['returncode'] != 0, 'IFB name is already in use'
    state = {'own_ip': own, 'peers': sorted(peers), 'device': dev, 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(), 'before_qdiscs': before, 'delay_ms': delay_ms, 'bandwidth_mbps': 100, 'created': [], 'phase': 'applying'}
    save_state(state)
    try:
        for args in wan_commands(dev, peers, delay_ms):
            # A loaded or built-in IFB driver does not require modprobe access
            # inside a container. The following link creation still verifies it.
            command(args, check=args[0] != 'modprobe')
            if args[:4] == ['ip', 'link', 'add', IFB]:
                state['created'].append('ifb')
                state['ifb_ifindex'] = json_command(['ip', '-j', 'link', 'show', 'dev', IFB])[0]['ifindex']
            if args[:3] == ['tc', 'qdisc', 'replace']: state['created'].append('root')
            if args[:3] == ['tc', 'qdisc', 'add'] and args[-1] == 'ingress': state['created'].append('ingress')
            save_state(state)
        state['phase'] = 'applied_unvalidated'
        save_state(state)
        return inspect(own, peers)
    except Exception:
        restore(own, peers)
        raise

def install_tools():
    package_map = {'ip': 'iproute2', 'tc': 'iproute2', 'ping': 'iputils-ping', 'iperf3': 'iperf3', 'ethtool': 'ethtool', 'modprobe': 'kmod'}
    packages = sorted({package for tool, package in package_map.items() if not shutil.which(tool)})
    log = []
    if packages:
        assert shutil.which('apt-get'), 'This bootstrap supports Debian/Ubuntu only'
        if 'iperf3' in packages and shutil.which('debconf-set-selections'):
            log.append(command(['debconf-set-selections'], input_text='iperf3 iperf3/start_daemon boolean false\n'))
        log.append(command(['apt-get', 'update'], timeout=600))
        log.append(command(['env', 'DEBIAN_FRONTEND=noninteractive', 'apt-get', 'install', '-y'] + packages, timeout=900))
    return {'installed_or_already_present': packages, 'log': log}

def job_dir(token):
    assert re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', token)
    path = Path('/var/tmp/datafedsort-network-runs') / token
    path.mkdir(parents=True, exist_ok=True)
    return path

def server_start(own, token, port):
    folder = job_dir(token)
    with socket.socket() as sock:
        # Consecutive tests use the same port. Ignore TCP TIME_WAIT, but still
        # refuse a live listener; iperf3 itself also enables SO_REUSEADDR.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((own, port))
    log_path = folder / 'server.json'
    args = ['timeout', '--kill-after=5s', '120s', 'iperf3', '-s', '-1', '-B', own, '-p', str(port), '-J', '--logfile', str(log_path)]
    with (folder / 'launch.log').open('w') as log:
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    (folder / 'server_pid.json').write_text(json.dumps({'pid': process.pid, 'log_path': str(log_path)}))
    for _ in range(30):
        if process.poll() is not None:
            raise RuntimeError((folder / 'launch.log').read_text())
        listeners = command(['ss', '-ltn', 'sport', '=', f':{port}'])['stdout']
        if re.search(rf':{port}\s', listeners):
            return {'pid': process.pid, 'port': port, 'token': token}
        time.sleep(0.1)
    raise RuntimeError('iperf3 server did not start listening')

def server_stop(token):
    folder = job_dir(token)
    data = json.loads((folder / 'server_pid.json').read_text())
    proc = Path(f'/proc/{data["pid"]}/cmdline')
    if proc.exists():
        argv = proc.read_bytes().split(b'\0')
        if not any(argv):
            return {'token': token, 'server_log': (folder / 'server.json').read_text() if (folder / 'server.json').exists() else ''}
        if b'iperf3' not in argv or data['log_path'].encode() not in argv:
            raise RuntimeError('PID no longer belongs to this test; leave it untouched')
        os.killpg(data['pid'], signal.SIGTERM)
    log = folder / 'server.json'
    return {'token': token, 'server_log': log.read_text() if log.exists() else ''}

def client(own, peer, port, seconds, omit, parallel=1):
    assert peer in WORKERS - {own}
    assert parallel in {1, 4}
    args = ['iperf3', '-c', peer, '-B', own, '-p', str(port), '-t', str(seconds), '-O', str(omit), '-P', str(parallel), '-J']
    return command(args, timeout=seconds + omit + 30, check=False)

def main():
    os.environ['LC_ALL'] = 'C'
    action = sys.argv[1]
    payload = json.loads(base64.b64decode(sys.argv[2]))
    global WORKERS
    own, peers = payload['own'], payload['peers']
    WORKERS = {own, *peers}
    assert len(WORKERS) == 4 and len(peers) == 3, 'Configure four distinct workers'
    assert all(ipaddress.ip_address(ip).version == 4 for ip in WORKERS), 'IPv4 worker addresses are required'
    assert own in WORKERS and set(peers) == WORKERS - {own}
    assert os.geteuid() == 0, 'Run as root on a configured worker'
    if action == 'inspect': result = inspect(own, peers)
    elif action == 'install': result = install_tools()
    elif action in {'apply', 'restore'}:
        import fcntl
        with Path('/var/lock/datafedsort-network.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = apply(own, peers, payload['delay_ms']) if action == 'apply' else restore(own, peers)
    elif action == 'ping':
        assert payload['peer'] in WORKERS - {own}
        result = command(['ping', '-n', '-q', '-c', str(payload['count']), '-i', '0.2', '-W', '2', payload['peer']], timeout=payload['count'] * 0.2 + 30, check=False)
    elif action == 'server_start': result = server_start(own, payload['token'], payload['port'])
    elif action == 'server_stop': result = server_stop(payload['token'])
    elif action == 'client': result = client(own, payload['peer'], payload['port'], payload['seconds'], payload['omit'], payload.get('parallel', 1))
    elif action == 'aggregate_client':
        with ThreadPoolExecutor(max_workers=3) as pool:
            result = list(pool.map(lambda peer: {'peer': peer, **client(own, peer, payload['port'], payload['seconds'], payload['omit'], payload.get('parallel', 1))}, sorted(peers)))
    else: raise ValueError(action)
    print(json.dumps(result))

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'error': str(exc)}))
        sys.exit(1)
