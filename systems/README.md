# Native system integration

Each directory uses the upstream dependency ID from `dependencies/lock.json`.
`overlay/` contains benchmark entry points and required source modifications.
`dependencies/overlays.json` maps those files to their destinations in a
pinned upstream checkout. Only deployment helpers have no upstream destination.

The runtime adapters select the sorting entry point and algorithm explicitly,
prepare the system outside the timed interval, invoke the sorting call, and
collect its metrics. Source fetches and native build products are kept under
the ignored workspace. No upstream repository history is vendored.

Scape is implemented through MPC4J. Conclave uses Congregation's JIFF path.
SCQL uses its SPU sorting runtime. SMPCache uses its Rosetta execution path.
The source profiles for ORQ are described in `dependencies/README.md`.

`orq/native-overlay/` and `orq/support/` provide the checked ORQ source profile
without replacing its upstream sorting or malicious-security protocol headers.
`support/build_native.sh` accepts a source directory and builds both required
party-count configurations with real protocol material.

Preserve the upstream copyright notices in overlay files and the attribution
documents under `dependencies/`. Synthetic data generators, plotting scripts,
private machine inventories, native build products, and experiment logs do
not belong in this source directory.
