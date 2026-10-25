# Source dependencies

`lock.json` records public upstream URLs and exact commits. Source checkouts,
downloaded packages, native builds, and generated files belong in the ignored
`workspace/` directory rather than in this repository.

List references without accessing the network:

```bash
python3 dependencies/fetch.py --list
```

Fetch only the systems needed for an experiment, initialize their upstream
submodules, and install the source overlays:

```bash
python3 dependencies/fetch.py Secrecy mpc4j spu
python3 dependencies/fetch.py orq --profile native_checked
```

Use `--workspace /path/to/workspace` to choose another user-owned directory.
`--skip-submodules` defers nested dependency downloads, and `--no-overlay`
retains a pristine upstream checkout. Fetching does not compile or execute
the benchmark. Native compilation and computing-party deployment are performed
by the runtime adapters.

Apply or inspect an overlay independently:

```bash
python3 dependencies/apply.py mpc4j
python3 dependencies/apply.py orq --profile native_checked --check
```

Existing checkouts at another commit are rejected instead of being reset.
`overlays.json` lists every supplied overlay and its upstream destination.
Deployment-only helpers have a null destination and are handled by their
runtime adapter.

Secrecy requires the separately pinned `sql-parser` dependency. Fetching Secrecy
automatically fetches that commit and copies its source, without Git metadata,
to `include/external-lib/sql-parser`. Existing dependency mounts with unknown
provenance are rejected. The remote adapter uses the same locked source through
a source-directory link.

Alchemy's four EMP libraries (`emp-tool`, `emp-ot`, `emp-sh2pc`, and `emp-zk`)
also have exact source commits in the lock. Fetching Alchemy mounts their
sources under `deps/emp/`, and its installer verifies these pins before
building. It does not resolve a moving branch or select a commit by date.
The installer requires permission to install native libraries under
`/usr/local`. It can install common build packages, or use preinstalled
packages with `ALCHEMY_EMP_INSTALL_DEPS=0`. Other upstream prerequisites,
including libpqxx and GoogleTest, use the versions specified by its setup
recipe.

## ORQ source profiles

The `base` profile selects commit
`a4e4f8270fb7af5f7c8f396240e192b03586ca67` and the operator overlays used by
the main semi-honest input sweeps. Its source path is `workspace/src/orq`.

The `native_checked` profile selects commit
`d2c1094b4ba053ad3a555ce8954d14c03b1ef39e` for the paired network and security
comparisons. Its source path is `workspace/src/orq-native`. This profile uses
the upstream three-party SH and four-party MAL protocols, the native benchmark
entry point, and one explicit dispatch selection for the upstream Bitonic
implementation. Its input is one `int32` key and one payload column at the
configured default or smoke-test size.

The profiles have separate overlay maps. Legacy ORQ operator headers are never
applied to `native_checked`. To build the latter after fetching:

```bash
bash systems/orq/support/build_native.sh workspace/src/orq-native
```

This creates `build-native-3/test_sorting_revision` and
`build-native-4/test_sorting_revision`. Set `JOBS` to control compilation
parallelism. Native protocol material generation and required checks remain
enabled.

## Names used by the adapters

The ten systems in the paper map to these source dependencies:

| Paper name | Dependency ID |
| --- | --- |
| SMCQL | `smcql` |
| Secrecy | `Secrecy` |
| Alchemy | `alchemy` |
| ORQ | `orq` |
| Scape | `mpc4j` |
| MASQUE | `MASQUE` |
| Conclave | `congregation`, with its JIFF runtime |
| SMPCache | `SMPCache`, with `Rosetta` |
| MP-SPDZ | `MP-SPDZ` |
| SCQL | `spu`, the sorting runtime used by SCQL |

Additional `scql`, `jiff`, and `liturgy` references are retained in the lock
for upstream traceability. The Conclave benchmark uses the pinned `jiff-mpc`
package version recorded under `runtime_packages`.

TPC-H `dbgen` is obtained separately through the official source and license
process described in the data documentation. No third-party database generator
source or generated input tables are vendored here.

## Attribution

Upstream projects retain their own licenses and copyright notices. See
`THIRD_PARTY_NOTICES.md` and the reference texts in `licenses/`. Upstream Git
metadata is fetched only inside the ignored workspace and is not part of the
artifact's Git history. The fetching scripts never copy a developer checkout
or its history.
