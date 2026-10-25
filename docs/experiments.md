# Experiment matrix

The JSON files in `experiments/` are the machine-readable parameters.
`python3 benchmark.py plan --suite all` expands them into individual cases.
Paper names and dependency IDs are interchangeable in `--systems`.

| Suite | Paper location | Variable settings |
| --- | --- | --- |
| `algorithms` | V-B.1 to V-B.4, Figures 3-6 | 2^8 and 2^20 rows, native algorithm progress profiles |
| `wan` | V-B.5, Figure 7 | Paired LAN and WAN at the default workload |
| `table-size` | V-C.1, Figure 8 | 2^12, 2^14, 2^16, 2^18, 2^20 rows |
| `key-count` | V-C.2, Figure 9 | 1, 2, 4, 8 keys, no payload columns |
| `key-width` | V-C.3, Figure 10 | int8, int32, int64 where supported |
| `payload` | V-C.4, Figure 11 | 1, 2, 4, 8 payload columns, one key |
| `distribution` | V-C.5, Figure 12 and Table III | Duplicates, partial order, and skew |
| `malicious` | V-D, Figure 13 | Paired SH and MAL on the LAN |

Unless varied, settings are 2^16 rows, one key, one payload column, int32,
the uniform-distribution input variant, and semi-honest security on the LAN.
Secrecy uses its native fixed 64-bit representation. The key-width suite
projects small-valued LINEITEM columns so logical values match across widths.
The other suites use the default column projection described in `data/README.md`.

Distribution parameters are duplicate fractions of 10%, 30%, 50%, 70%, 90%,
sorted fractions of 0%, 25%, 50%, 75%, 100%, and Zipf exponents of 0.5, 1.0,
1.2, 1.5, 2.0. Each transformation preserves complete source rows.

`--scale smoke` uses 256-row inputs and shorter sweeps. It verifies the same
native paths and TPC-H preparation contract, but is not a paper-scale result.
`--algorithms bitonic radix`, `--systems ORQ`, and `--network LAN` can narrow
the selected matrix. An empty selection is an error.

## Native implementation coverage

| Paper name | Sorting families | SH parties | MAL parties |
| --- | --- | --- | --- |
| SMCQL | Bitonic | 2 | - |
| Secrecy | Bitonic | 3 | - |
| Alchemy | Bitonic | 2 | - |
| ORQ | Bitonic, Radix, Quick | 3 | 4 |
| Scape | Bitonic, Radix, Quick | 3 | 3 |
| MASQUE | Bitonic, Quick | 2 | - |
| Conclave | Odd-Even Merge | 3 | - |
| SMPCache | Odd-Even Merge | 3 | - |
| MP-SPDZ | Odd-Even Merge, Radix | 3 | 3 |
| SCQL | Odd-Even Merge, Radix, Quick | 3 | - |

The public wrappers for MASQUE, Conclave, SMPCache, and MP-SPDZ currently expose
one sorting key. Their multi-key sweep points remain visible in the plan as
`UNSUPPORTED`. MASQUE uses 32-bit column words and has no int64 input path in
the supplied wrapper. Alchemy represents int8 input values using its native
32-bit integer type. Plans and native records distinguish requested input
width from effective representation width. No narrowing, clipping, or
single-key substitution is used to fabricate unsupported points.

The full requested matrix contains 644 cases, including unsupported points.
The paired WAN and security suites contain 36 and 16 runnable cases,
respectively. `plan` reports supported case counts for every selection.
`run` records unsupported cases without launching them and returns a nonzero
status when a selection is only partially executable. Execution errors also
return a nonzero status. Use `--keep-going` to retain subsequent independent
cases after an execution failure.

ORQ has two pinned source profiles. Main input sweeps use `base`, while paired
network and security experiments use `native_checked`. The latter accepts the
default or smoke workload and retains the native real-material protocols.
It is never combined with the base profile's operator overlays.
