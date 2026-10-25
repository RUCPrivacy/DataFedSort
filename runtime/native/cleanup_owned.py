#!/usr/bin/env python3
"""Terminate only this framework workspace's sorting benchmark processes."""
import argparse,json,os,signal,time
from pathlib import Path

def cleanup(workspace,system):
    base=workspace.resolve()/'src'/system
    assert workspace.is_absolute() and workspace.resolve() != Path('/')
    assert (workspace / '.datafedsort-runtime').is_file(), 'Workspace ownership marker is missing'
    markers={'MP-SPDZ':['replicated-field-party.x','malicious-rep-field-party.x'],
             'mpc4j':['PgSortLanParty'],
             'congregation':['sort_operator_runner.js','sorting_micro_test.py'],
             'Secrecy':['exp_sorting_micro'],
             'smcql':['com.oblivm.backend.circuits.BitonicSortLibTest'],
             'MASQUE':['test_sorting_micro'],
             'alchemy':['sorting_micro_test'],
             'orq':['test_sorting_micro'],
             'orq-native':['test_sorting_revision'],
             'spu':['sorting_micro_test'],
             'SMPCache':['obliviousSort_lan_party.py']}[system]
    owned=[]
    for process in Path('/proc').iterdir():
        if not process.name.isdigit():continue
        try:
            arguments=(process/'cmdline').read_bytes().replace(b'\0',b' ').decode(errors='replace')
            cwd=(process/'cwd').resolve()
            if cwd.is_relative_to(base) and any(marker in arguments for marker in markers):
                owned.append((int(process.name),(process/'stat').read_text().split()[21]))
        except (FileNotFoundError,PermissionError,ProcessLookupError):pass
    def current(pid,ticks):
        try:return (Path('/proc')/str(pid)/'stat').read_text().split()[21]==ticks
        except (FileNotFoundError,ProcessLookupError):return False
    for pid,ticks in owned:
        if current(pid,ticks):
            try:os.kill(pid,signal.SIGTERM)
            except ProcessLookupError:pass
    if owned:time.sleep(2)
    for pid,ticks in owned:
        if current(pid,ticks):
            try:os.kill(pid,signal.SIGKILL)
            except ProcessLookupError:pass
    if owned:time.sleep(0.2)
    remaining=[pid for pid,ticks in owned if current(pid,ticks) and (Path('/proc')/str(pid)/'stat').read_text().split()[2]!='Z']
    assert not remaining,remaining
    return {'system':system,'workspace':str(workspace),'terminated_owned_pids':[pid for pid,_ in owned],'remaining':remaining}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('workspace',type=Path);p.add_argument('system',choices=['MP-SPDZ','mpc4j','congregation','Secrecy','smcql','MASQUE','alchemy','orq','orq-native','spu','SMPCache']);a=p.parse_args()
    print(json.dumps(cleanup(a.workspace,a.system)))
