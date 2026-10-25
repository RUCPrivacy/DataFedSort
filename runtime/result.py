#!/usr/bin/env python3
"""Collect native sort-call records and write one machine-readable case result.

This module does not plot, rank, extrapolate, or reconstruct historical results.
Only an executed native timeout is reported as TIMEOUT.
"""
import argparse
import csv
import json
import math
from pathlib import Path
import re
import sys

MIB = 1024 * 1024


def csv_records(text, marker):
    return [next(csv.reader([line])) for line in text.splitlines()
            if line.startswith(marker + ',')]


def one(records, label):
    if len(records) != 1:
        raise ValueError(f'{label}: expected one native record, got {len(records)}')
    return records[0]


def text_at(raw, rank, filename):
    return one(list(raw.glob(f'*/rep*/p{rank}/{filename}')), filename).read_text(errors='replace')


def verified(text):
    if 'SORT_TUPLES_VERIFIED ' not in text:
        raise ValueError('Native public-input full-tuple verification marker is missing')


def collect(raw, case):
    system = case['system']
    all_text = '\n'.join(p.read_text(errors='replace') for p in raw.rglob('*.log'))
    if system in ('mpc4j', 'MP-SPDZ'):
        native = json.loads(one(list(raw.glob('*/rep*/verified-window.json')), 'verified-window.json').read_text())
        if native.get('full_tuple_correct') is not True:
            raise ValueError('Native complete-tuple verifier did not succeed')
        if system == 'mpc4j' and (native.get('real_materials') is not True or native.get('sort_checks_completed') is not True):
            raise ValueError('Native protocol checks or real material generation did not complete')
        result = dict(time_s=native['time_s'], communication_mib=native['communication_mib'],
                      peak_memory_mib=native['peak_rss_mib'], memory_basis='sort-window process RSS',
                      communication_basis='sum of native per-party sent bytes')
    elif system == 'orq' and case['source_profile'] == 'native_checked':
        records = [json.loads(line.partition(',')[2]) for line in all_text.splitlines()
                   if line.startswith('RESULT_REVISION,')]
        native = one(records, 'RESULT_REVISION')
        for flag in ('full_tuple_correct', 'real_materials', 'sort_checks_completed'):
            if native.get(flag) is not True:
                raise ValueError(f'ORQ native completion flag missing: {flag}')
        if native['security'] != case['security'] or native['parties'] != case['parties'] or native['algorithm'] != case['algorithm']:
            raise ValueError('ORQ native result and requested configuration differ')
        result = dict(time_s=native['time_s'], communication_mib=native['communication_mib'],
                      peak_memory_mib=native['peak_rss_mib'], memory_basis='sort-window process RSS',
                      communication_basis='sum of native per-party sent bytes')
    elif system == 'orq':
        text = text_at(raw, 0, 'mpirun.log')
        verified(text)
        record = one(csv_records(text, 'RESULT_CSV'), 'ORQ total')
        result = dict(time_s=float(record[9]), communication_mib=float(record[10]),
                      peak_memory_mib=float(record[11]), memory_basis='sort-window process RSS',
                      communication_basis='sum of native per-party sent bytes')
    elif system == 'Secrecy':
        text = text_at(raw, 0, 'mpirun.log')
        verified(text)
        record = one(csv_records(text, 'RESULT_SORTING_MICRO_CSV'), 'Secrecy total')
        result = dict(time_s=float(record[6]), communication_mib=float(record[9]) / 2,
                      peak_memory_mib=float(record[10]), memory_basis='native maximum process RSS',
                      communication_basis='sum of native MPI sent-plus-received bytes divided by two',
                      native_sent_plus_received_mib=float(record[9]))
    elif system == 'MASQUE':
        text = text_at(raw, 0, 'server.log')
        verified(all_text)
        record = one(csv_records(text, 'RESULT_CSV'), 'MASQUE total')
        result = dict(time_s=float(record[7]), communication_mib=float(record[8]),
                      peak_memory_mib=float(record[9]), memory_basis='native maximum process RSS',
                      communication_basis='native sum of per-party communication counters')
    elif system == 'alchemy':
        text = text_at(raw, 0, 'alice.log')
        verified(all_text)
        records = [r for r in csv_records(text, 'RESULT_CSV') if len(r) > 8 and r[7] == '0']
        record = one(records, 'Alchemy total')
        result = dict(time_s=float(record[8]), communication_mib=float(record[9]),
                      peak_memory_mib=float(record[10]), memory_basis='native maximum process RSS high-water mark',
                      communication_basis='native global two-party communication counter')
    elif system == 'smcql':
        verified(all_text)
        records = [one(csv_records(text_at(raw, rank, f'party{rank}.log'), 'RESULT_CSV'), f'SMCQL rank {rank}') for rank in range(2)]
        counters = [float(r[9]) for r in records if float(r[9]) >= 0]
        if len(counters) != 1:
            raise ValueError('SMCQL requires one client sent-plus-received counter')
        result = dict(time_s=max(float(r[7]) for r in records), communication_mib=counters[0],
                      peak_memory_mib=max(float(r[10]) for r in records), memory_basis='native maximum process RSS',
                      communication_basis='client sent plus received bytes, covering both parties')
    elif system == 'spu':
        verified(all_text)
        records = [one(csv_records(text_at(raw, rank, f'party{rank}.log'), 'SORT_PARTY_CSV'), f'SPU rank {rank}') for rank in range(case['parties'])]
        result = dict(time_s=max(float(r[8]) for r in records),
                      communication_mib=sum(int(r[9]) for r in records) / MIB,
                      peak_memory_mib=max(int(r[10]) for r in records) / 1024,
                      memory_basis='sort-window process RSS', communication_basis='sum of native per-party sent bytes')
    elif system == 'SMPCache':
        verified(all_text)
        native = []
        for rank in range(3):
            text = text_at(raw, rank, f'party{rank}.log')
            record = one(csv_records(text, 'SORT_PARTY_METRICS'), f'SMPCache rank {rank}')
            fields = dict(item.split('=', 1) for item in record[1:])
            if int(fields['rank']) != rank:
                raise ValueError('SMPCache party rank mismatch')
            native.append(fields)
        result = dict(time_s=max(float(r['time_s']) for r in native),
                      communication_mib=sum(int(r['sent_bytes']) for r in native) / MIB,
                      peak_memory_mib=max(int(r['peak_rss_kib']) for r in native) / 1024,
                      memory_basis='sort-window process RSS', communication_basis='sum of native per-party sent bytes')
    elif system == 'congregation':
        verified(all_text)
        durations = []
        memories = []
        communication = 0.0
        for rank in range(3):
            text = text_at(raw, rank, f'party{rank+1}.log')
            durations.append(float(one(re.findall(r'Sort-only computation time:\s*([0-9.eE+-]+)\s+seconds', text), 'Conclave time')))
            memories.append(float(one(re.findall(r'Peak memory usage:\s*([0-9.eE+-]+)\s+KB', text), 'Conclave memory')))
            levels = csv_records(text, 'SORT_LEVEL_PROFILE_CSV')
            if not levels:
                raise ValueError('Conclave native communication profile is missing')
            communication += sum(float(r[5]) for r in levels)
        result = dict(time_s=max(durations), communication_mib=communication,
                      peak_memory_mib=max(memories) / 1024, memory_basis='native maximum process RSS',
                      communication_basis='sum of native JIFF emitted-message profiler bytes')
    else:
        raise ValueError(f'No native metric collector for {system}')
    for key in ('time_s', 'communication_mib', 'peak_memory_mib'):
        if not math.isfinite(result[key]) or result[key] < 0 or (key != 'communication_mib' and result[key] == 0):
            raise ValueError(f'Invalid native metric: {key}')
    result['full_tuple_correct'] = True
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--metadata', type=Path)
    parser.add_argument('--case-directory', type=Path)
    parser.add_argument('--exit-code', type=int, default=0)
    parser.add_argument('--status')
    for key in ('system', 'group', 'algorithm', 'dtype', 'distribution', 'security', 'profile', 'network', 'input', 'input-sha256'):
        parser.add_argument('--' + key)
    for key in ('power', 'columns', 'keys', 'bits', 'parties', 'repeat'):
        parser.add_argument('--' + key, type=int)
    args = parser.parse_args()
    if args.metadata:
        result = {key: getattr(args, key) for key in ('system', 'group', 'algorithm', 'power', 'columns', 'keys', 'dtype', 'distribution', 'bits', 'security', 'network', 'input', 'input_sha256', 'parties', 'repeat')}
        result['source_profile'] = args.profile
        result['effective_representation_bits'] = 64 if args.system == 'Secrecy' else (32 if args.system in ('alchemy', 'MASQUE') and args.dtype == 'int8' else args.bits)
        args.metadata.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
        return 0
    folder = args.case_directory
    case = json.loads((folder / 'case.json').read_text())
    result = dict(case, status=args.status or 'FAILED', exit_code=args.exit_code, metrics={})
    if args.status is None:
        raw = folder / 'raw' / case['system']
        try:
            if args.exit_code:
                raise ValueError('Native adapter exited before verified completion')
            result['metrics'] = collect(raw, case)
            result['status'] = 'OK'
        except (ValueError, KeyError, IndexError, OSError, AssertionError) as exc:
            logs = '\n'.join(p.read_text(errors='replace') for p in raw.rglob('*') if p.is_file() and p.suffix in ('.log', '.time'))
            if (folder / '.timeout-observed').is_file() or re.search(r'Exit status:\s*124\b', logs):
                result['status'] = 'TIMEOUT'
            elif re.search(r'OutOfMemoryError|std::bad_alloc|Cannot allocate memory|MemoryError|out of memory', logs, re.I):
                result['status'] = 'OOM'
            else:
                result['status'] = 'FAILED'
            result['message'] = str(exc)
    (folder / 'result.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result))
    return 0 if result['status'] in ('OK', 'TIMEOUT', 'OOM') else 1


if __name__ == '__main__':
    raise SystemExit(main())
