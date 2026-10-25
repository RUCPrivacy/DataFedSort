# Third-party notices

The artifact references independent upstream projects and supplies benchmark
entry points, instrumentation, and source overlays for them. These components
retain the terms of their respective copyright holders. An artifact-level
license does not relicense third-party source or dependencies.

The following upstream license documents accompany the corresponding source
overlays. Fetched source trees also retain their own notices and dependency
licenses.

| Dependency | Included upstream reference |
| --- | --- |
| Alchemy | `licenses/alchemy-apache.txt`, `licenses/alchemy-mit.txt` |
| Conclave / Congregation | `licenses/congregation.txt` |
| MP-SPDZ | `licenses/MP-SPDZ.txt` |
| Scape / MPC4J | `licenses/mpc4j.txt` |
| ORQ | `licenses/orq.txt` |
| Secrecy | `licenses/Secrecy.txt` |
| SQL Parser, used by Secrecy | `licenses/sql-parser.txt` |
| SMCQL | `licenses/smcql.txt` |
| SCQL sorting runtime / SPU | `licenses/spu.txt` |

The ORQ reference is the GNU Affero General Public License, version 3.
The MPC4J, Secrecy, and SPU references are Apache License 2.0 documents.
The SMCQL and Congregation references are MIT license documents.
The SQL Parser reference is an MIT license document with its upstream
Hasso-Plattner-Institut copyright notice.
The MP-SPDZ reference contains its upstream CSIRO license terms.
Alchemy provides Apache and MIT license documents.

For MASQUE, SMPCache, JIFF, Liturgy, Rosetta, and the full SCQL repository,
consult the pinned upstream source and its included notices. This artifact
does not assign a license to a project for which a license document is not
bundled here. Public repository URLs and exact source revisions are listed
in `lock.json`.

Alchemy additionally references the independently maintained EMP Toolkit
repositories `emp-tool`, `emp-ot`, `emp-sh2pc`, and `emp-zk` at the exact
commits in `lock.json`. Their downloaded source retains its upstream license
and copyright notices. No EMP source or license grant is substituted by this
artifact's original-code license.

TPC-H and its database generation software are maintained by the Transaction
Processing Performance Council. Obtain `dbgen` from the official distribution
under its applicable terms. TPC-H source and generated tables are not included
in this artifact.
