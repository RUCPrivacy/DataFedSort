# TPC-H inputs

Every benchmark input is a projection or complete-row transformation of the
TPC-H `LINEITEM` table. There is no random-value or synthetic-table fallback.
Random seeds control row sampling and distribution transformations, not the
generation of replacement attribute values.

## Prepare the source

Obtain the official TPC-H tools from the [TPC download page](https://www.tpc.org/tpc_documents_current_versions/current_specifications5.asp).
The tools remain an external dependency and retain their original licensing
terms. Pass the directory containing `makefile.suite` and `dists.dss`:

```bash
python3 data/prepare_tpch.py --dbgen-dir /path/to/tpch/dbgen
```

Alternatively, use an existing official executable and its distribution file:

```bash
python3 data/prepare_tpch.py --dbgen /path/to/dbgen --dists /path/to/dists.dss
```

Preparation runs `dbgen -s 1 -T L`, freezes a master of `2^20` distinct order-key
groups, selects one complete row per group, and materializes the `2^8` and
`2^16` default inputs. Sampling seeds are 42 and 43. The default store is
`workspace/data/tpch-sf1`, inside the ignored runtime workspace. The generated
manifest records source, binary, master, input, and validation-reference hashes.
When a release header is supplied, its actual version is recorded.

## Generate individual configurations

The first two positional arguments are the row exponent and total column count.
Keys precede payload columns. For example:

```bash
python3 data/generateData.py 16 2 -k 1 --dtype int32
python3 data/generateData.py 16 8 -k 8 --dtype int32
python3 data/generateData.py 16 9 -k 1 --dtype int32
python3 data/generateData.py 16 2 -k 1 --dtype int8 --profile key-width
python3 data/generateData.py 16 2 -k 1 --dist Zipf --zipf_factor 1.2
python3 data/generateData.py 16 2 -k 1 --dist PartiallySorted --sorted_fraction 0.75
python3 data/generateData.py 16 2 -k 1 --dist HighRepeat --repeat_fraction 0.5
```

Default output is `workspace/data/generated/tpch-v1/<profile>`. Override it with
`--output-dir`. Use `--tpch-store` or `FES_TPCH_STORE` to select another frozen
store. `--source-table` or `FES_TPCH_TABLE` accepts an existing `lineitem.tbl`;
its generator release and scale are left unspecified unless the store was
created by the preparation step above.

`--verify-existing` checks the source/master lineage, configuration, and exact
deterministic bytes without changing existing files. Existing unverified or
modified inputs are rejected. `--overwrite` explicitly replaces generated
inputs, never the source or master table.

## Column and transformation contract

The default projection is `L_ORDERKEY` followed by `L_PARTKEY`. Additional
columns, in order, are `L_SUPPKEY`, `L_LINENUMBER`, `L_QUANTITY`,
`L_EXTENDEDPRICE`, `L_DISCOUNT`, `L_TAX`, `L_SHIPDATE`, `L_COMMITDATE`, and
`L_RECEIPTDATE`. The first `k` projected columns are sorting keys.

The key-width profile always uses `L_QUANTITY` and `L_LINENUMBER`, so `int8`,
`int32`, and `int64` compare identical logical values. Values are never clipped
or wrapped to fit a type. Integer decimal quantities remain exact, other
decimal fields are multiplied by 100, and dates are integer days since
1970-01-01.

Uniform inputs are nested prefixes of the frozen master. Zipf and duplicate
variations resample complete source rows. Partial-order variations sort a
prefix of complete rows. Key/payload associations are preserved throughout.
Each input has a `.manifest.json` sidecar describing the selected columns,
encodings, seeds, transformation parameters, row counts, and SHA-256 hashes.

The small LINEITEM-shaped fixtures under `tests/` are only unit-test data.
They are not experiment inputs or claimed dbgen output.
