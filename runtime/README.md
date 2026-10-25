# Native execution runtime

Use the repository's unified command-line interface for the paper experiments.
`run_case.sh` is its lower-level interface for one native operator invocation.
It does not generate synthetic input, plot figures, extrapolate running times,
or turn unexecuted cases into timeout results.

## Deployment requirements

Run the controller on Linux with Bash 4+, Python 3.10+, OpenSSH, rsync,
`flock`, GNU `timeout`, and GNU `time`. Configure four separate Linux workers
with the same dedicated absolute workspace. A single machine can host the
four isolated worker containers supplied by the repository. Four processes
sharing one network namespace are not an equivalent deployment.

The controller needs noninteractive SSH access to every worker. ORQ and Secrecy
also launch MPI workers from worker zero, so worker-to-worker SSH must work
with the configured identity and host verification. Identity files and known
host files referenced by MPI must exist at the same configured paths on that
worker. No identity or host address is bundled with this runtime.

Install each upstream project's compiler and library prerequisites before
preparation. Typical requirements include CMake and a C++ compiler, OpenMPI,
OpenSSL and Boost, Java/Maven, Node/npm, and Bazelisk. Scape requires Java 21
and native FourQ/MPC4J libraries. SMPCache requires a separate Conda environment
with Python 3.7, TensorFlow 1.14, and a source build of the locked Rosetta
backend. Alchemy builds its locked EMP dependencies through its setup scripts.
These older projects have different toolchains and are intentionally isolated
from the controller's Python environment.

Native setup can install libraries under `/usr/local` and invoke upstream
package installation scripts. The supplied worker containers use root.
For an unprivileged remote SSH account, provision those dependencies and
permissions first. The configuration's `sudo` option for network management
does not automatically elevate native build commands. Preparation fails if
a required build or setup step fails. Successful preparation is never inferred
from the mere existence of a source directory.

## Input and execution contract

Pass an input created by `data/generateData.py`, its matching `.manifest.json`,
and `--benchmark-public-input`. The latter explicitly authorizes opening this
public benchmark relation after the measured sort call to verify key order
and the complete tuple multiset. It is unsuitable for confidential inputs.
The runtime checks the requested shape/type/distribution and SHA-256, stages
the same bytes on each worker, and verifies the worker copy against that hash.
Native malicious-protocol consistency checks remain enabled.

The `--prepare` mode fetches exact source revisions from
`dependencies/lock.json`, applies the maintained overlays, and builds the
selected backend. It can omit `--input` and `--algorithm`. Executing a case
also prepares the backend before running it. The `base` and `native_checked`
ORQ profiles have separate source and build directories, so incompatible
headers cannot overwrite one another. The checked ORQ profile supports the
paired network/security experiments with 256 or 65,536 rows, two integer
columns, and one sorting key.

The runtime serializes native cases within a controller workspace because
upstream programs reuse fixed ports, generated compiler outputs, and input
paths. Use distinct controller and worker workspaces for independent runs.
`--repeat` is the positive repetition index supplied by the unified driver.

## Native capabilities

| Adapter | Sorting families | Native harness restrictions |
| --- | --- | --- |
| SMCQL | Bitonic | Two parties, SH |
| Secrecy | Bitonic | Three parties, SH, integer values represented in 64 bits |
| Alchemy | Bitonic | Two parties, SH, native integer/float types, int8 input uses native int32 |
| MASQUE | Bitonic, Quick | Two parties, SH, one key, 32-bit integer tuple words |
| ORQ | Bitonic, Radix, Quick | Base SH uses three parties, checked SH/MAL uses three/four parties |
| Scape/MPC4J | Bitonic, Radix, Quick | Three parties, SH/MAL, integer inputs |
| Conclave/Congregation | Odd-Even Merge | Three parties, SH, one integer key |
| SMPCache | Odd-Even Merge | Three parties, SH, one integer key |
| MP-SPDZ | Odd-Even Merge, Radix | Three parties, SH/MAL, one integer key |
| SCQL/SPU | Odd-Even Merge, Radix, Quick | Three parties, SH |

The unified plan records unsupported combinations explicitly. The runtime
rejects an unsupported native combination instead of substituting a different
algorithm, security protocol, width, or empty result. An input's numeric range
and its native comparison representation are distinct, so results also record
the effective representation width.

## Results and measurement bases

Generated records are local to the ignored `workspace/` directory. Each case
has `case.json`, native worker output under `raw/`, and `result.json` under
`workspace/runs/<run-id>/cases/<case-key>/`. The latter records the case,
input hash, execution status, and verified native metrics. It uses seconds and
MiB (2^20 bytes), with peak RSS converted from KiB where applicable. No
decimal-MB conversion is silently applied to native counters.

Running time is the maximum participating party's native sort-call time.
Input sharing and the public correctness oracle are outside that timer.
Scape and checked ORQ include the native completion of deferred security
checks. MP-SPDZ uses native Timer 1 with exact sent-byte and RSS-window
instrumentation. SPU and base ORQ use per-party native sort-window records.
Other backends retain their native RSS high-water or sampling scope, which
is stated in the result's `memory_basis` field.

Communication counters are collected from native records. The
`communication_basis` field describes their scope. SMCQL's client sent plus
received bytes covers both endpoints of its two-party connection. Secrecy's
MPI hooks count both sends and receives across all parties, so the collector
divides their sum by two and retains the raw sum as a separate field.
Conclave retains its native JIFF emitted-message profiler. Scape, ORQ,
MP-SPDZ, SPU, and SMPCache aggregate the available per-party native sent-byte
counters. MASQUE and Alchemy retain their native aggregated counters.

`OK` requires native completion and a successful complete-tuple oracle.
`TIMEOUT` requires an observed GNU timeout exit status 124. `OOM` requires an
actual allocation-failure diagnostic. Missing output, unsupported collectors,
failed checks, and other execution errors remain failures. The timeout is
configured per case and cannot exceed 3,600 seconds. No communication-round
estimate is used to skip execution. Worker cleanup is restricted to benchmark
processes whose current directory lies inside this marked runtime workspace.

`run_case.sh` returns zero for `OK`, executed `TIMEOUT`, or observed `OOM`,
and nonzero for invalid options, preparation failures, verification failures,
or other errors. Inspect `result.json` to distinguish the recorded outcomes.
