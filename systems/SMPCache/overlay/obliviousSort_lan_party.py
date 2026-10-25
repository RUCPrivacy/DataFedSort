#!/usr/bin/env python3

# ==============================================================================
# Configure warning filters before importing third-party libraries.
# ==============================================================================
import warnings
import os
import sys
import hashlib

# Suppress TensorFlow C++ INFO and WARNING messages.
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

# Suppress Python FutureWarning and DeprecationWarning messages.
warnings.filterwarnings('ignore', category=FutureWarning)
warnings.filterwarnings('ignore', category=DeprecationWarning)
warnings.simplefilter('ignore', FutureWarning)


import multiprocessing
import queue
import time
import numpy as np
import tensorflow as tf
import random
import json
import threading

# Configure TensorFlow Python logging.
tf.compat.v1.logging.set_verbosity(tf.compat.v1.logging.ERROR)


sessruntotal = 0


class PeakMemoryMonitor:
    def __init__(self, interval=0.02):
        self.interval = interval
        self.peak_kb = self._current_kb()
        self._running = False
        self._thread = None

    def _current_kb(self):
        try:
            import psutil
            return psutil.Process(os.getpid()).memory_info().rss // 1024
        except Exception:
            try:
                import resource
                value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                if sys.platform == "darwin":
                    value //= 1024
                return int(value)
            except Exception:
                return 0

    def _sample(self):
        while self._running:
            self.peak_kb = max(self.peak_kb, self._current_kb())
            time.sleep(self.interval)

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._sample)
        self._thread.daemon = True
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread is not None:
            self._thread.join()
        self.peak_kb = max(self.peak_kb, self._current_kb())


def parse_perf_stats(perf_stats_json):
    stats = json.loads(perf_stats_json)
    comm = stats.get('communication(B)', {})
    return {
        'bytes_sent': int(comm.get('bytes-sent', 0)),
        'bytes_recv': int(comm.get('bytes-recv', 0)),
        'msg_sent': int(comm.get('msg-sent', 0)),
        'msg_recv': int(comm.get('msg-recv', 0)),
    }


def comm_delta(before, after):
    return {key: after.get(key, 0) - before.get(key, 0) for key in after}


def load_plaintext_data(data_path, data_type):
    if not data_path:
        raise ValueError("--input is required; SMPCache benchmark now reads data from a file.")
    if not os.path.isfile(data_path):
        raise FileNotFoundError("Input data file not found: {}".format(data_path))

    dtype = np.float32
    data = np.loadtxt(data_path, dtype=dtype)
    if data.ndim == 1:
        data = data.reshape(1, -1)

    if data_type == 'bool':
        data = (data != 0).astype(np.float32)
    elif data_type in ('int', 'float'):
        data = data.astype(np.float32)
    else:
        raise ValueError("Unsupported data type: {}. Use 'bool', 'int', or 'float'.".format(data_type))

    return data


def permit_public_input_verification(data_path):
    """Authorize output opening only for a sidecar-verified public input."""
    if os.environ.get('FES_PUBLIC_BENCHMARK') != '1':
        return False
    expected = os.environ.get('FES_INPUT_SHA256', '').lower()
    if len(expected) != 64 or any(ch not in '0123456789abcdef' for ch in expected):
        raise ValueError('Public benchmark verification requires FES_INPUT_SHA256')
    digest = hashlib.sha256()
    with open(data_path, 'rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    if digest.hexdigest() != expected:
        raise ValueError('Public benchmark input does not match FES_INPUT_SHA256')
    return True


def validate_public_output(opened, plaintext, sort_col):
    """Check key order and all key/payload tuples without requiring stability."""
    actual = np.asarray(opened, dtype=np.float64)
    expected = np.asarray(plaintext, dtype=np.float64)
    if actual.shape != expected.shape or actual.ndim != 2:
        raise ValueError('Public benchmark output has the wrong table shape')
    if not 0 <= sort_col < actual.shape[1]:
        raise ValueError('Invalid public benchmark sorting column')
    if not np.all(np.isfinite(actual)) or not np.all(np.isfinite(expected)):
        raise ValueError('Public benchmark tables must contain finite values')
    if np.any(actual[1:, sort_col] < actual[:-1, sort_col]):
        raise ValueError('Public benchmark output keys are not sorted')
    columns = range(actual.shape[1] - 1, -1, -1)
    actual_order = np.lexsort(tuple(actual[:, col] for col in columns))
    expected_order = np.lexsort(tuple(expected[:, col] for col in columns))
    if not np.array_equal(actual[actual_order], expected[expected_order]):
        raise ValueError('Full tuples differ from the public TPC-H input')


def oblivious_cond_swap_bit(cond, x, y):
    '''
    Conditional private swap for single attribute

    Param-cond: compare result(SS) for x and y

    Formula: return cond * x + y - cond * y, cond * y + x - cond * x
    '''
    import latticex.rosetta as rtt

    # sess = tf.Session()
    # cond = sess.run(cond)

    # cx = sess.run(rtt.SecureMul(cond, x))
    # cy = sess.run(rtt.SecureMul(cond, y))

    # tempAdd_1 = sess.run(rtt.SecureAdd(cx, y))
    # tempAdd_2 = sess.run(rtt.SecureAdd(cy, x))

    # res_1 = rtt.SecureSub(tempAdd_1, cy)
    # res_2 = rtt.SecureSub(tempAdd_2, cx)
    res_1 = rtt.SecureAdd(rtt.SecureMul(cond, rtt.SecureSub(x, y)), y)
    res_2 = rtt.SecureAdd(rtt.SecureMul(cond, rtt.SecureSub(y, x)), x)
    # res_1 = rtt.SecureSub(rtt.SecureAdd(rtt.SecureMul(cond, x), y), rtt.SecureMul(cond, y))
    # res_2 = rtt.SecureSub(rtt.SecureAdd(rtt.SecureMul(cond, y), x), rtt.SecureMul(cond, x))
    # res_1 = tf.subtract(tf.add(tf.multiply(cond, x), y), tf.multiply(cond, y))
    # res_2 = tf.subtract(tf.add(tf.multiply(cond, y), x), tf.multiply(cond, x))

    return res_1, res_2

def oblivious_cond_swap_record(attr, InputVector, index_1, index_2, ifDummy, mode):
    '''
    Conditional private swap between records

    e.g. If we want to swap two record according to AGE in ascending order.
    ID    AGE   DEPOSIT             ID    AGE    DEPOSIT
    ......                          ......
    ......                          ......
    1003  38    1500000    --->     1015  25     80000
    ......                          ......
    ......                          ......
    ......                          ......
    1015  25    80000               1003  38     1500000

    attr: The column attribute to sort by. e.g. If we want to sort a table(ID, AGE, DEPOSIT) according to AGE, then attr is 1
    InputVector: The vector to be sort (SS) Note. Should transfer the ciphertext from tf.Tensor to ndarray
    index_1 / index_2: Two objects' indexs to be swapped (plaintext)
    ifDummy: Marking whether each element is dummy -> 0: Real record; 1: Dummy (plaintext)
    mode: Sorting mode -> 0: Ascending; 1: Descending (plaintext)

    '''
    import latticex.rosetta as rtt

    x_value = InputVector[index_1]
    y_value = InputVector[index_2]

    x_flag = ifDummy[index_1]
    y_flag = ifDummy[index_2]

    '''
    Swap strategy: move the dummy to the end of vector and shrink
    '''
    if (x_flag == 1) & (y_flag == 0):
        '''
        If the front element is real value while backend element is dummy, then do swap
        '''
        InputVector[index_1] = y_value
        ifDummy[index_1] = y_flag

        InputVector[index_2] = x_value
        ifDummy[index_2] = x_flag
        
    else:
        if y_flag == 1:
            '''
            If the backend element is dummy, no need to swap
            '''
            pass
        else:
            '''
            If both of x_value and y_value are real value, then do conditional private compare(obliviously swap)
            '''
            x_attr = InputVector[index_1][attr]
            y_attr = InputVector[index_2][attr]
            
            if mode == 0:
                sig = rtt.SecureLess(np.array(x_attr), np.array(y_attr))
            else:
                sig = rtt.SecureGreater(np.array(x_attr), np.array(y_attr))
            
            sess = tf.Session()

            # x_value_tensor_new, y_value_tensor_new = oblivious_cond_swap_bit(sig, np.array(x_value), np.array(y_value))
            # TIME_START = time.time()
            x_value_tensor_new, y_value_tensor_new = oblivious_cond_swap_bit(sig, x_value, y_value)
            # TIME_END = time.time()
            # print('Each obviously swap time:', TIME_END - TIME_START)

            # TIME_START = time.time()
            tf.reset_default_graph()
            x_value_new = sess.run(x_value_tensor_new)
            y_value_new = sess.run(y_value_tensor_new)
            # TIME_END = time.time()
            # global sessruntotal
            # sessruntotal += (TIME_END - TIME_START)
            # print('Each sess.run time:', TIME_END - TIME_START)

            InputVector[index_1] = x_value_new
            InputVector[index_2] = y_value_new

def oblivious_odd_even_merge_sort(attr, InputVector, mode, sorted_length = 1, party_id=None):
    '''
    This module will sort the InputVector according to attr and mode

    Step1. Pads the vector to power of 2 with dummy elements
    '''
    original_length = len(InputVector)
    padding_length = len(InputVector)
    while padding_length & (padding_length - 1) != 0:
        padding_length += 1
    
    record_len = len(InputVector[0])
    
    rd = np.random.RandomState(1487)
    DataSort = []
    DataFlag = []
    for idx in range(padding_length):
        if idx < original_length:
            '''
            Push the real value
            '''
            DataSort.append(InputVector[idx])
            DataFlag.append(0)
        else:
            '''
            Padding with dummy
            '''
            DataSort.append(InputVector[rd.randint(0, original_length)])
            DataFlag.append(1)
    
    DataSort = np.array(DataSort)
    
    '''
    Step2. Execute odd-even merge sort obviously
    '''
    l = sorted_length
    num_keys = len(DataSort)
    round_no = 0
    while l < num_keys:
        l *= 2
        k = 1
        while k < l:
            round_no += 1
            round_start = time.time()
            round_comm_before = None
            try:
                import latticex.rosetta as rtt
                round_comm_before = parse_perf_stats(rtt.get_perf_stats(pretty=True))
            except Exception:
                round_comm_before = None

            k *= 2
            n_outer = num_keys // l
            n_inner = l // k
            n_innermost = 1 if k == 2 else k // 2 - 1

            for i in range(n_outer):
                for j in range(n_inner):
                    base = i * l + j
                    step = l // k
                    if k == 2:
                        index1 = base
                        index2 = base + step
                        oblivious_cond_swap_record(attr, DataSort, index1, index2, DataFlag, mode)
                    else:
                        for i_inner in range(n_innermost):
                            m1 = step + i_inner * 2 * step
                            m2 = m1 + base
                            index1 = m2
                            index2 = m2 + step
                            oblivious_cond_swap_record(attr, DataSort, index1, index2, DataFlag, mode)

            round_time = time.time() - round_start
            try:
                import latticex.rosetta as rtt
                round_comm_after = parse_perf_stats(rtt.get_perf_stats(pretty=True))
                round_comm = comm_delta(round_comm_before, round_comm_after) if round_comm_before else round_comm_after
                round_total_bytes = round_comm['bytes_sent'] + round_comm['bytes_recv']
                print(
                    "SORT_ROUND_PROFILE_CSV,{},{},{},{},{},{:.6f},{}".
                    format(party_id, round_no, l, k, n_outer * n_inner, round_time, round_total_bytes)
                )
            except Exception:
                print(
                    "SORT_ROUND_PROFILE_CSV,{},{},{},{},{},{:.6f},{}".
                    format(party_id, round_no, l, k, n_outer * n_inner, round_time, 0)
                )
    
    DataSort = DataSort[0 : original_length, 0 : record_len]
    # global sessruntotal
    # print('sess.run total time:', sessruntotal)
    return DataSort

def test(id, data_path, sort_col=1, data_type='int', result_queue=None):
    """
    Execute the secure sorting benchmark.
    
    Arguments:
        id: computing party ID (0, 1, or 2)
        data_path: input data file
        sort_col: zero-based sorting-key column
        data_type: input representation ('bool', 'int', or 'float')
    """
    verify_public_input = permit_public_input_verification(data_path)
    sys.argv.extend(["--node_id", "P{}".format(id)])
    import latticex.rosetta as rtt
    rtt.backend_log_to_stdout(False)
    rtt.activate("SecureNN")

    rd = np.random.RandomState(42 + id)

    plaintext = load_plaintext_data(data_path, data_type)
    num_rows, num_cols = plaintext.shape
    if not 0 <= sort_col < num_cols:
        raise ValueError("sort-col ({}) must be between zero and input cols ({})".format(sort_col, num_cols))

    if id == 0:
        rtx = rtt.controller.PrivateDataset(["P0"]).load_X(plaintext)
        rty = rtt.controller.PrivateDataset(["P1"]).load_X(None)
        rtz = rtt.controller.PrivateDataset(["P2"]).load_X(None)
    elif id == 1:
        dummy_plaintext = rd.randint(0, 1, (1, 1)).astype(np.float32)
        rtx = rtt.controller.PrivateDataset(["P0"]).load_X(None)
        rty = rtt.controller.PrivateDataset(["P1"]).load_X(dummy_plaintext)
        rtz = rtt.controller.PrivateDataset(["P2"]).load_X(None)
    else:
        dummy_plaintext = rd.randint(0, 1, (1, 1)).astype(np.float32)
        rtx = rtt.controller.PrivateDataset(["P0"]).load_X(None)
        rty = rtt.controller.PrivateDataset(["P1"]).load_X(None)
        rtz = rtt.controller.PrivateDataset(["P2"]).load_X(dummy_plaintext)
    
    session = tf.Session()
    
    # Start resource measurement.
    rtt.start_perf_stats()
    
    memory_monitor = PeakMemoryMonitor()
    memory_monitor.start()
    TIME_START = time.time()
    sorted_rtxdata = oblivious_odd_even_merge_sort(sort_col, rtx, 0, party_id=id)
    TIME_END = time.time()
    memory_monitor.stop()
    
    # Read resource measurements.
    perf_stats = parse_perf_stats(rtt.get_perf_stats(pretty=True))
    
    # Calculate elapsed time in seconds.
    execution_time = TIME_END - TIME_START
    
    # Extract communication bytes.
    bytes_sent = perf_stats['bytes_sent']
    bytes_recv = perf_stats['bytes_recv']
    total_bytes = bytes_sent + bytes_recv
    # Capture native sort counters before opening authorized public output.
    if verify_public_input:
        opened = np.asarray(session.run(rtt.SecureReveal(sorted_rtxdata))).astype(np.float64)
        validate_public_output(opened, plaintext, sort_col)
        print('SORT_TUPLES_VERIFIED rows={} cols={}'.format(num_rows, num_cols), flush=True)
    
    # Convert to MiB.
    comm_mb = total_bytes / (1024 * 1024)
    
    if id == 0:
        print(f'\n{"="*60}')
        print(f'Input: {data_path}')
        print(f'Data Type: {data_type}, Rows: {num_rows}, Cols: {num_cols}, Sort Column: {sort_col}')
        print(f'{"="*60}')
        print(f'Total sorting execution time: {execution_time:.6f} seconds')
        print(f'Peak memory usage: {memory_monitor.peak_kb} KB ({memory_monitor.peak_kb / 1024:.4f} MB)')
        print(f'Communication statistics:')
        print(f'  - Bytes sent:     {bytes_sent:>15,} bytes ({bytes_sent/(1024*1024):.4f} MB)')
        print(f'  - Bytes received: {bytes_recv:>15,} bytes ({bytes_recv/(1024*1024):.4f} MB)')
        print(f'  - Total:          {total_bytes:>15,} bytes ({comm_mb:.4f} MB)')
        print(f'  - Messages sent:  {perf_stats["msg_sent"]:>15,}')
        print(f'  - Messages recv:  {perf_stats["msg_recv"]:>15,}')
        print(f'{"="*60}')
        
    tf.get_default_graph().finalize()
    
    if id == 0:
        print()

    party_result = {
        'party': id,
        'time_s': execution_time,
        'bytes_sent': bytes_sent,
        'bytes_recv': bytes_recv,
        'total_bytes': total_bytes,
        'peak_memory_kb': memory_monitor.peak_kb,
    }
    print(
        "PARTY_RESULT_CSV,{party},{time_s:.6f},{total_bytes},{peak_memory_kb}".
        format(**party_result)
    )
    print(
        "SORT_PARTY_METRICS,rank={party},time_s={time_s:.6f},"
        "sent_bytes={bytes_sent},peak_rss_kib={peak_memory_kb}".
        format(**party_result), flush=True
    )

    if result_queue is not None:
        result_queue.put(party_result)


if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='Oblivious Sort Micro Benchmark')
    parser.add_argument('--input', required=True, help='Input data file path')
    parser.add_argument('--sort-col', type=int, default=1, help='Column index to sort by (default: 1)')
    parser.add_argument('--dtype', type=str, default='int', choices=['bool', 'int', 'float'],
                        help='Data type: bool, int, or float (default: int)')
    parser.add_argument('--party-id', type=int, choices=[0, 1, 2],
                        help='Run one Rosetta party process. Omit to run all parties locally.')
    
    args = parser.parse_args()

    try:
        input_shape = load_plaintext_data(args.input, args.dtype).shape
    except Exception as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    if not 0 <= args.sort_col < input_shape[1]:
        print(f"Error: sort-col ({args.sort_col}) must be between zero and cols ({input_shape[1]})")
        sys.exit(1)
    
    print(f"Starting oblivious sort benchmark...")
    print(f"Configuration: input={args.input}, rows={input_shape[0]}, cols={input_shape[1]}, sort_col={args.sort_col}, dtype={args.dtype}")
    print("SORT_ROUND_PROFILE_CSV header: tag,party,round,merge_size,k,compare_groups,time_s,comm_B")

    if args.party_id is not None:
        test(args.party_id, args.input, args.sort_col, args.dtype, None)
        print("\nParty {} completed.".format(args.party_id))
        sys.exit(0)

    result_queue = multiprocessing.Queue()
    p0 = multiprocessing.Process(target=test, args=(0, args.input, args.sort_col, args.dtype, result_queue))
    p1 = multiprocessing.Process(target=test, args=(1, args.input, args.sort_col, args.dtype, result_queue))
    p2 = multiprocessing.Process(target=test, args=(2, args.input, args.sort_col, args.dtype, result_queue))

    p0.daemon = True
    p0.start()
    p1.daemon = True
    p1.start()
    p2.daemon = True
    p2.start()

    p0.join()
    p1.join()
    p2.join()

    results = []
    while len(results) < 3:
        try:
            results.append(result_queue.get_nowait())
        except queue.Empty:
            break

    failed_parties = [
        (idx, proc.exitcode)
        for idx, proc in enumerate((p0, p1, p2))
        if proc.exitcode != 0
    ]

    if len(results) == 3:
        total_comm = sum(item['total_bytes'] for item in results)
        max_time = max(item['time_s'] for item in results)
        peak_memory = max(item['peak_memory_kb'] for item in results)
        print("RESULT,total_time_s,{:.6f}".format(max_time))
        print("RESULT,total_comm_B,{}".format(total_comm))
        print("RESULT,total_comm_MB,{:.6f}".format(total_comm / (1024 * 1024)))
        print("RESULT,peak_memory_KB,{}".format(peak_memory))
    else:
        print("WARNING: expected 3 party results, got {}".format(len(results)))
    
    if failed_parties or len(results) != 3:
        print("ERROR: party failures: {}".format(failed_parties))
        sys.exit(1)
    
    print("\nAll processes completed.")
