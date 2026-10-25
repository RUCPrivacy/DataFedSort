# DataFedSort

Reproduction artifact for *An Experimental Study of Privacy-Preserving Sorting
in Private Data Federation*.

The artifact provides one experiment matrix for ten systems and eighteen
system/algorithm implementations. It covers algorithm progress, input
characteristics, LAN/WAN conditions, and semi-honest/malicious security.
The same entry point runs on a Linux cluster or isolated computing-party
containers on one computer. Every experiment input comes from TPC-H LINEITEM.

## Contents

| Directory | Purpose |
| --- | --- |
| `artifact/`, `benchmark.py` | Experiment selection, execution, and network control |
| `experiments/` | Paper parameters and native implementation capabilities |
| `data/` | Official dbgen integration, LINEITEM projections, and input validation |
| `runtime/` | Configurable SSH transport, native adapters, and metric collection |
| `systems/` | Native benchmark entry points and necessary upstream modifications |
| `dependencies/` | Public upstream URLs, exact revisions, overlays, and licenses |
| `configs/`, `containers/` | Generic deployment examples and single-host containers |
| `reference_results/` | Small numeric reference tables for comparison with the paper |
| `tests/` | Offline matrix, data, input-validation, and runtime checks |

There are no preconfigured research servers. Create your own deployment file.
Downloaded sources, generated tables, builds, keys, and execution logs go in
ignored `workspace/` and `outputs/` directories. Plotting and manuscript
production code are outside this artifact.

## Inspect the experiment plan

Python 3.10 or later is required. These commands need no SSH, native builds,
network access, or generated dataset:

```bash
python3 benchmark.py list
python3 benchmark.py check
python3 benchmark.py plan --suite wan --systems all
python3 benchmark.py run --suite malicious --systems ORQ --dry-run
```

`plan` includes native harness limits explicitly through `supported` and
`unsupported_reason`. Unsupported configurations are never counted as
successful measurements or inferred timeouts. See [the experiment matrix](docs/experiments.md).

## Prepare data and sources

Obtain the official TPC-H tools under their own license, then generate the
LINEITEM source and frozen input store:

```bash
python3 data/prepare_tpch.py --dbgen-dir /path/to/official/tpch/dbgen
python3 dependencies/fetch.py --list
python3 dependencies/fetch.py Secrecy mpc4j spu
python3 dependencies/fetch.py orq --profile native_checked
```

Select the sources needed for your experiment. The adapters also fetch their
locked sources when preparing workers. No dbgen source is redistributed here.
See [data preparation](data/README.md) and [dependency profiles](dependencies/README.md).

## Choose a deployment

For four Linux workers, copy `configs/cluster.example.json` to
`configs/cluster.local.json`, replace the example hostnames, SSH user, key,
and dedicated workspace path, and enable noninteractive SSH between the
controller and workers and between workers. Start with one system:

```bash
python3 benchmark.py prepare --systems Scape --config configs/cluster.local.json
python3 benchmark.py network measure --config configs/cluster.local.json
python3 benchmark.py run --suite algorithms --systems Scape --scale smoke \
  --config configs/cluster.local.json --run-id scape-smoke
```

Native builds require each upstream project's toolchain. Consult
[runtime instructions](runtime/README.md) before preparing a system.
Four distinct worker addresses are required even if a selected protocol uses
only two or three of them. They let the same deployment run ORQ's four-party
malicious configuration.

For a single Linux computer, Docker Compose provides isolated workers:

```bash
bash containers/init_keys.sh
mkdir -p outputs
docker compose build
docker compose up -d party0 party1 party2 party3
docker compose run --rm controller
```

Inside the controller, use `--config configs/local-containers.json`. Put the
externally obtained dbgen tools in `workspace/tools/`, prepare the input store
there, and run the same preparation and smoke commands. The container images
provide common tools, while native adapters prepare backend-specific dependencies.
Containers share the host's CPU, memory, and kernel, so their timings are useful
for functional reproduction and are not independent-machine LAN measurements.
WAN shaping additionally requires host kernel IFB/netem support and
`NET_ADMIN`, already requested by the Compose file. See [reproduction steps](docs/reproduction.md).

## Run the paper suites

The default workload has 65,536 rows, one sorting key, and one payload column.
Each native test point has a 3,600-second execution limit. Source downloads,
input preparation, and compilation occur outside the measured sorting call.

```bash
python3 benchmark.py run --suite table-size key-count key-width payload distribution \
  --systems Scape --config configs/cluster.local.json --run-id scape-inputs
python3 benchmark.py run --suite wan --systems ORQ --manage-network \
  --config configs/cluster.local.json --run-id orq-network
python3 benchmark.py run --suite malicious --systems Scape ORQ MP-SPDZ \
  --config configs/cluster.local.json --run-id security
```

WAN uses 100 Mbps and 50 ms RTT. `--manage-network` applies peer-only traffic
control and restores owned rules on exit. SSH traffic is excluded. Use
`network inspect`, `network measure`, and `network restore` to inspect,
measure, or restore the configured worker network. Do not run concurrent cases
against the same workers.

The malicious suite pairs SH and MAL on the LAN. Scape and MP-SPDZ use three
parties in both configurations. ORQ uses three parties for SH and four for MAL.
No other malicious backend is claimed by this artifact.

Results are written to `outputs/<run-id>/outcomes.json`, with per-case native
records under `workspace/runs/`. Logs and generated files stay local. Successful
results require a post-timing sorted-key and complete-tuple check on explicitly
authorized public inputs. Native protocol checks remain enabled. Missing or
invalid native results are failures, not zero-valued measurements.

## Validation and licenses

```bash
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests -v
```

Offline tests exercise input lineage, tuple preservation, case selection,
protocol dispatch, and result collection. They do not replace compiling and
running the upstream MPC implementations on your target deployment.

Original framework code uses the [MIT license](LICENSE). Third-party code
retains its own terms. See [NOTICE](NOTICE) and
[third-party notices](dependencies/THIRD_PARTY_NOTICES.md).
