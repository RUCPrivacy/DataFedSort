# Paper reference values

These small tables provide numeric reference values for the paper figures:

- `lan_rounds.csv` contains the displayed cumulative running-time and
  communication curves for Figures 3-6. Round progress is the percentage of
  algorithmic stages or passes, not a count of MPC communication rounds.
- `lan_inputs.csv` contains the displayed input-characteristic measurements
  for Figures 8-12. Its parameter columns identify the varied dimension.
- `wan.csv` contains the LAN and WAN running times for the eighteen
  semi-honest system/algorithm combinations in Figure 7.
- `security.csv` contains the running time, communication cost, and peak memory
  for the eight paired SH/MAL combinations in Figure 13.

The WAN and security comparisons use 65,536 rows, one sorting key, and one payload column
derived from TPC-H `LINEITEM`. The WAN configuration is 100 Mbps and 50 ms
round-trip time. SH and MAL mean semi-honest and malicious
security. The party-count column states the actual configuration, including
ORQ's three-party SH and four-party MAL configurations.

Time is in seconds. Communication and memory retain the paper's MB labels.
Display numbers are copied without rescaling or reinterpreting the underlying
backend counters. In particular, the WAN and security display values must not
be converted from MiB a second time. Native measurements from a new run retain
their explicit units in the runtime output.

The two LAN tables use one row per displayed metric and parameter value. They
include only points shown in the corresponding figures, without fitted slopes,
private run metadata, or inferred failure records. Numerical precision is
retained as available in those display tables. Blank metric cells in the WAN
or security tables are not zero. The WAN table
reports only the time metric shown in that figure. `TIMEOUT` means the
one-hour limit was exceeded, and `OOM` means an out-of-memory failure.

These are reference results, not input tables or a substitute for running the
artifact. Results on other hardware or networks may differ. Run the benchmark
with locally generated TPC-H inputs to obtain measurements for your own
environment.
