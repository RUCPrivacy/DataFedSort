# Reproduction steps

## Platform and resources

Run native experiments from Linux with Python 3.10+, Bash, Git, OpenSSH,
rsync, GNU time, and the native dependencies required by each system.
The paper used four Ubuntu 22.04 workers with eight vCPUs and 32 GiB of memory
each. Smaller machines can execute smoke tests, but large circuits and
malicious protocols can require substantial memory. The controller also needs
space for LINEITEM, native sources, and compiler outputs.

Windows can inspect plans and run offline tests directly. Use Linux, WSL2,
or Docker for native execution. Native build recipes are system-specific.
The supplied container image provides common build and network utilities;
it is not a prebuilt image of every upstream MPC stack.

## Four-worker cluster

1. Create a private deployment JSON from `configs/cluster.example.json`.
   Use four distinct reachable hostnames or IPv4 addresses and a dedicated
   remote workspace. Keep the resulting file named `*.local.json` so Git
   ignores it. Configure SSH keys and host verification locally.
2. Enable noninteractive SSH from the controller to every worker and between
   workers. MPI and some two-party launchers initiate worker-to-worker SSH.
   Open only the native experiment ports within your computing-party network.
3. Install common and backend-specific build dependencies on each worker.
   Network control requires root or passwordless sudo. Keep package installation
   and compilation separate from measured sorting calls.
4. Obtain official TPC-H tools and run `data/prepare_tpch.py` on the controller.
   The generated data store is shared by all suites. A case sends the exact
   verified input and sidecar to its participating workers.
5. Run `benchmark.py prepare --systems SYSTEM --config CONFIG`. For ORQ's
   paired WAN/MAL source, add `--profile native_checked`. Execution preparation
   also verifies source pins and builds missing native targets.
6. Measure your LAN using `benchmark.py network measure --config CONFIG`.
   The reported throughput and RTT are properties of your deployment. The
   paper's LAN values are reference conditions, not constants forced by code.
7. Execute one `--scale smoke` selection, inspect its native result and tuple
   validation, then increase to `--scale paper`.

Do not share workers with another concurrent benchmark. Each controller
workspace has a lock, but another controller cannot be detected reliably.

## Single-host containers

Run the Compose commands from the repository root. `containers/init_keys.sh`
generates a new key only inside ignored `workspace/local_cluster`. It refuses
to replace a partial existing key pair. The key is used only for the isolated
local cluster. No existing user or research-server key is included.

After `docker compose run --rm controller`, the repository is mounted at
`/artifact`, read-only except for `workspace/` and `outputs/`. Worker sources
and builds live in each worker's `/work/datafedsort` directory. Use
`configs/local-containers.json` for every command inside the controller.
Prepare data with the official tools stored below the mounted workspace.

An IFB-capable Linux host is needed for ingress WAN shaping. If the host uses
a loadable IFB module, load it on the host before shaping the containers.
The helper refuses unknown pre-existing traffic-control policies rather than
overwriting them. Use a four-worker Linux deployment if your container host
does not expose the required kernel features.

Container CPU and memory limits are deliberately not set to pretend that a
single host is four independent paper machines. Set local resource limits
according to the host's capacity. Run `docker compose down` when finished.

## WAN and security comparisons

`run --suite wan --manage-network` alternates paired LAN and WAN inputs.
WAN shaping limits aggregate outgoing traffic to the computing peers to
100 Mbps and introduces half of the additional RTT at each receiver. The
helper calibrates against the native network and verifies a 50 ms RTT target.
SSH traffic is excluded from shaping. Rules are owned by this artifact and
restored on normal exit or handled interruption. After an abrupt controller
termination, run `network inspect` and `network restore` before another LAN run.
`network measure` can independently inspect throughput and RTT under either
condition.

`run --suite malicious` compares SH and MAL on the LAN using identical inputs.
It performs no WAN shaping. ORQ's party count changes from three to four;
Scape and MP-SPDZ remain at three. Real protocol material and native
verification checks are retained. The explicit public-input flag authorizes
only post-timing validation of benchmark data, not private production inputs.

## Outputs

`outputs/<run-id>/plan.json` records the selection, and `outcomes.json` records
every attempted or unsupported point. Each native case stores its input hash,
source profile, party count, native logs, metric bases, and validation status
under `workspace/runs/<case-run-id>/cases/`.

Native result fields `communication_mib` and `peak_memory_mib` use binary
mebibytes (2^20 bytes). Reference CSVs preserve the values and MB labels shown
in the paper without another conversion. WAN/security reference values are
already decimal MB. Convert a new native MiB measurement by 1.048576 to compare
it with decimal MB, rather than converting an existing display value again.
For legacy LAN display tables, retain the native counter basis when interpreting
their labels. Preserve `memory_basis` and `communication_basis` when comparing
implementations. Native counters and samplers can have different scopes.

`TIMEOUT` requires an actually observed native timeout. `OOM` requires an
out-of-memory failure record. `FAILED` includes missing metrics, protocol or
tuple-check failures, and unexpected exits. No failed or unsupported case is
assigned a synthetic running time, memory value, or communication value.
