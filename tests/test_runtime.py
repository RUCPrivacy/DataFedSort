"""Offline runtime contracts using small, explicitly fabricated native-log fixtures."""
import hashlib
import fnmatch
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RESULT = load_module('runtime_result_test', 'runtime/result.py')
CONFIG = load_module('runtime_config_test', 'runtime/config_env.py')
MP_COLLECT = load_module('runtime_mp_collect_test', 'runtime/native/mp_spdz/collect.py')


class NativeCollectorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.raw = Path(self.temporary.name) / 'raw'
        self.repeat = self.raw / 'case' / 'rep1'
        self.repeat.mkdir(parents=True)

    def log(self, rank, filename, text):
        path = self.repeat / f'p{rank}' / filename
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def fixture(self, system):
        case = dict(system=system, source_profile='base', security='SH', parties=3, algorithm='bitonic')
        if system in ('mpc4j', 'MP-SPDZ'):
            record = dict(time_s=2, communication_mib=3, peak_rss_mib=4,
                          full_tuple_correct=True, real_materials=True, sort_checks_completed=True)
            (self.repeat / 'verified-window.json').write_text(json.dumps(record))
        elif system == 'orq':
            self.log(0, 'mpirun.log', 'SORT_TUPLES_VERIFIED rows=2 cols=2\n'
                     'RESULT_CSV,input,2,2,1,bitonic,int32,total,0,2,3,4\n')
        elif system == 'Secrecy':
            self.log(0, 'mpirun.log', 'SORT_TUPLES_VERIFIED rows=2 cols=2\n'
                     'RESULT_SORTING_MICRO_CSV,2,2,1,input,2,2,8,3,6,4\n')
        elif system == 'MASQUE':
            self.log(0, 'server.log', 'SORT_TUPLES_VERIFIED rows=2 cols=2\n'
                     'RESULT_CSV,input,2,2,1,bitonic,int32,2,3,4\n')
        elif system == 'alchemy':
            self.log(0, 'alice.log', 'SORT_TUPLES_VERIFIED rows=2 cols=2\n'
                     'RESULT_CSV,InputFile,NumRows,NumCols,NumKeys,DataType,Stage,Round,Time_s,Comm_MB,PeakRSS_MB\n'
                     'RESULT_CSV,input,2,2,1,int32,total,0,2,3,4,4,10\n')
        elif system == 'smcql':
            case['parties'] = 2
            self.log(0, 'party0.log', 'SORT_TUPLES_VERIFIED rows=2 cols=2\n'
                     'RESULT_CSV,input,2,2,1,32,int32,2,3,-1,4\n')
            self.log(1, 'party1.log', 'RESULT_CSV,input,2,2,1,32,int32,2,3,3,4\n')
        elif system == 'spu':
            for rank in range(3):
                self.log(rank, f'party{rank}.log', 'SORT_TUPLES_VERIFIED rows=2 cols=2\n'
                         f'SORT_PARTY_CSV,{rank},2,2,1,ABY3,FM64,SORT_NETWORK,2,1048576,4096\n')
        elif system == 'SMPCache':
            for rank in range(3):
                self.log(rank, f'party{rank}.log', 'SORT_TUPLES_VERIFIED rows=2 cols=2\n'
                         f'SORT_PARTY_METRICS,rank={rank},time_s=2,sent_bytes=1048576,peak_rss_kib=4096\n')
        elif system == 'congregation':
            for rank in range(3):
                self.log(rank, f'party{rank+1}.log', 'SORT_TUPLES_VERIFIED rows=2 cols=2\n'
                         'Sort-only computation time: 2 seconds\nPeak memory usage: 4096 KB\n'
                         'SORT_LEVEL_PROFILE_CSV,level,2,1,2,1\n')
        else:
            raise AssertionError(system)
        return case

    def checked_orq(self, **changes):
        record = dict(system='orq', algorithm='quick', security='MAL', parties=4,
                      full_tuple_correct=True, real_materials=True, sort_checks_completed=True,
                      time_s=2, communication_mib=3, peak_rss_mib=4)
        record.update(changes)
        self.log(0, 'mpirun.log', 'RESULT_REVISION,' + json.dumps(record) + '\n')
        return dict(system='orq', source_profile='native_checked', algorithm='quick', security='MAL', parties=4)

    def assert_metrics(self, case):
        metrics = RESULT.collect(self.raw, case)
        self.assertEqual([metrics[key] for key in ('time_s', 'communication_mib', 'peak_memory_mib')], [2, 3, 4])
        self.assertTrue(metrics['full_tuple_correct'])
        self.assertIn('memory_basis', metrics)
        self.assertIn('communication_basis', metrics)

    def test_checked_orq_native_format(self):
        self.assert_metrics(self.checked_orq())

    def test_checked_orq_rejects_incomplete_checks(self):
        with self.assertRaises(ValueError):
            RESULT.collect(self.raw, self.checked_orq(sort_checks_completed=False))

    def test_checked_orq_rejects_different_security(self):
        with self.assertRaises(ValueError):
            RESULT.collect(self.raw, self.checked_orq(security='SH'))

    def test_missing_party_cannot_be_successful(self):
        case = self.fixture('spu')
        (self.repeat / 'p2' / 'party2.log').unlink()
        with self.assertRaises(ValueError):
            RESULT.collect(self.raw, case)

    def test_missing_oracle_cannot_be_successful(self):
        case = self.fixture('MASQUE')
        path = self.repeat / 'p0' / 'server.log'
        path.write_text(path.read_text().replace('SORT_TUPLES_VERIFIED', 'NO_VERIFICATION'))
        with self.assertRaises(ValueError):
            RESULT.collect(self.raw, case)

    def test_duplicate_total_is_rejected(self):
        case = self.fixture('orq')
        path = self.repeat / 'p0' / 'mpirun.log'
        path.write_text(path.read_text() * 2)
        with self.assertRaises(ValueError):
            RESULT.collect(self.raw, case)


def collector_test(system):
    def test(self):
        self.assert_metrics(self.fixture(system))
    test.__doc__ = f'Parse the {system} native metric format and its units.'
    return test


for _system in ('mpc4j', 'MP-SPDZ', 'orq', 'Secrecy', 'MASQUE', 'alchemy', 'smcql', 'spu', 'SMPCache', 'congregation'):
    setattr(NativeCollectorTests, 'test_format_' + _system.replace('-', '_'), collector_test(_system))


class RuntimeConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'cluster.json'
        self.setting = dict(workers=['party0', 'party1', 'party2', 'party3'], ssh_user='root',
                            remote_workspace='/work/datafedsort', timeout_seconds=3600,
                            ssh_options=['BatchMode=yes', 'ConnectTimeout=10'])

    def environment(self):
        self.path.write_text(json.dumps(self.setting))
        return list(CONFIG.environment(self.path))

    def test_shared_ssh_option_schema(self):
        emitted = self.environment()
        command = CONFIG.ssh_arguments(CONFIG.load_config(self.path), 'party0')
        self.assertEqual(command, ['ssh', '-n', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', 'root@party0'])
        self.assertTrue(any('BENCH_SSH_OPTIONS_JSON' in line and '-o' in line for line in emitted))

    def test_rejects_unsafe_worker(self):
        self.setting['workers'][0] = '-oProxyCommand=anything'
        with self.assertRaises(ValueError):
            self.environment()

    def test_rejects_shared_worker_namespace(self):
        self.setting['workers'][3] = 'party0'
        with self.assertRaises(ValueError):
            self.environment()

    def test_rejects_nondedicated_workspace(self):
        self.setting['remote_workspace'] = '/'
        with self.assertRaises(ValueError):
            self.environment()

    def test_rejects_excess_timeout(self):
        self.setting['timeout_seconds'] = 3601
        with self.assertRaises(ValueError):
            self.environment()

    def test_orq_forwards_public_verification_to_mpi_ranks(self):
        # Every MPI rank must enter the collective post-sort oracle consistently.
        adapter = (ROOT / 'runtime/adapters/orq.sh').read_text()
        self.assertIn('-x FES_PUBLIC_BENCHMARK', adapter)
        self.assertIn('-x FES_INPUT_SHA256', adapter)

    def test_spu_selects_exact_distribution_variant(self):
        shell = shutil.which('bash')
        if os.name == 'nt':
            git = shutil.which('git')
            candidate = Path(git).resolve().parents[1] / 'bin/bash.exe' if git else None
            shell = str(candidate) if candidate and candidate.is_file() else None
        if not shell:
            self.skipTest('Bash is unavailable for the adapter dispatch contract')
        adapter = str(ROOT / 'runtime/adapters/spu.sh').replace('\\', '/')
        variants = {'Zipf': ['z0p5', 'z1p0', 'z1p2', 'z1p5', 'z2p0'],
                    'PartiallySorted': ['s0', 's25', 's50', 's75', 's100'],
                    'HighRepeat': ['r10', 'r30', 'r50', 'r70', 'r90']}
        for distribution, suffixes in variants.items():
            with self.subTest(distribution=distribution):
                requested = f'data_2pow16_m2_k1_int32_{distribution}_{suffixes[2]}.txt'
                command = ['bash', '-c', 'source "$1"; bench_spu_gtest_filter R9_zipf quick 16 2 1 int32 "$2" "$3"',
                           'dispatch-test', adapter, distribution, 'workspace/data/' + requested]
                command[0] = shell
                output = subprocess.run(command, capture_output=True, text=True, check=True).stdout
                names = [f'DistributionInstances/DistributionSortTest.Run/R9_Distribution_data_2pow16_m2_k1_int32_{distribution}_{suffix}_txt_SORT_QUICK'
                         for suffix in suffixes]
                matches = [name for name in names if fnmatch.fnmatchcase(name, output)]
                self.assertEqual(len(matches), 1, output)
                self.assertIn(suffixes[2] + '_txt_SORT_QUICK', matches[0])


class PublicInputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.input = self.root / 'input.txt'
        # Toy values are parser fixtures, not published benchmark measurements.
        self.input.write_text('1 9\n1 3\n')
        self.manifest = dict(generator='datafedsort-tpch-input-v1', rows=2, cols=2,
                             config=dict(exp_n=1, m=2, k=1, dtype='int32', dist='Uniform'),
                             sha256=hashlib.sha256(self.input.read_bytes()).hexdigest())
        self.sidecar = Path(str(self.input) + '.manifest.json')
        self.sidecar.write_text(json.dumps(self.manifest))

    def verify(self, keys=1):
        command = [sys.executable, str(ROOT / 'runtime/input_env.py'), str(self.root),
                   'input.txt', '1', '2', str(keys), 'int32', 'Uniform']
        return subprocess.run(command, capture_output=True, text=True,
                              env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})

    def test_authorizes_only_matching_public_input(self):
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('FES_PUBLIC_BENCHMARK=1', result.stdout)
        self.assertIn(self.manifest['sha256'], result.stdout)

    def test_rejects_tampered_bytes(self):
        self.input.write_text('1 8\n1 3\n')
        result = self.verify()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('input hash differs', result.stderr)

    def test_rejects_different_case_shape(self):
        self.assertNotEqual(self.verify(keys=2).returncode, 0)


class MPSPDZOracleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.input = self.folder / 'input.txt'
        self.input.write_text('1 9\n1 3\n')
        for rank in range(3):
            target = self.folder / f'p{rank}'
            target.mkdir()
            text = ('Time1 = 2 seconds (3 MB\nFES_SORT_WINDOW_RSS_KIB=4096\n'
                    'FES_SORT_WINDOW_SENT_BYTES=1048576\n')
            if rank == 0:
                text += 'SORT_ROW 1 9\nSORT_ROW 1 3\n'
            (target / f'party{rank}.log').write_text(text)

    def test_allows_equal_key_tuple_permutations(self):
        self.assertTrue(MP_COLLECT.collect(self.folder, self.input)['full_tuple_correct'])

    def test_rejects_changed_tuple(self):
        path = self.folder / 'p0/party0.log'
        path.write_text(path.read_text().replace('SORT_ROW 1 9', 'SORT_ROW 2 9'))
        with self.assertRaises(AssertionError):
            MP_COLLECT.collect(self.folder, self.input)

    def test_rejects_missing_native_counter(self):
        path = self.folder / 'p1/party1.log'
        path.write_text(path.read_text().replace('FES_SORT_WINDOW_SENT_BYTES=', 'MISSING='))
        with self.assertRaises(AssertionError):
            MP_COLLECT.collect(self.folder, self.input)


if __name__ == '__main__':
    unittest.main()
