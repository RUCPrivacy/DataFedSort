#!/usr/bin/env python3
"""Select the retained native Bitonic network in the checked ORQ profile."""

import argparse
from pathlib import Path


def select(source: Path, check: bool = False) -> None:
    path = source.resolve() / "include/core/containers/tabular/encoded_table.h"
    text = path.read_text(encoding="utf-8")
    before = "operators::pairwise_sort(keys_vec, data_a, data_b, order);"
    after = "operators::bitonic_sort(keys_vec, data_a, data_b, order);"
    if before in text:
        if check:
            raise ValueError("Native Bitonic table dispatch has not been selected")
        if text.count(before) != 1:
            raise ValueError("Unexpected upstream network-sort dispatch")
        path.write_text(text.replace(before, after), encoding="utf-8")
    elif text.count(after) != 1:
        raise ValueError("Expected native Bitonic dispatch was not found")
    print("Native Bitonic table dispatch verified")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    select(args.source, args.check)
