#!/usr/bin/env bash
# Build the locked, checked ORQ profile for three and four computing parties.
set -euo pipefail
SOURCE="${1:?usage: build_native.sh SOURCE_DIRECTORY}"
JOBS="${JOBS:-2}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ -f "$SOURCE/CMakeLists.txt" && -f "$SOURCE/tests/test_sorting_revision.cpp" ]]
python3 "$SCRIPT_DIR/select_bitonic.py" "$SOURCE"
if [[ ! -f "$SOURCE/libOTe-install/lib/cmake/cryptoTools/cryptoToolsConfig.cmake" && ! -f "$SOURCE/libOTe-install/lib64/cmake/cryptoTools/cryptoToolsConfig.cmake" ]]; then
  (cd "$SOURCE" && bash scripts/_setup_cryptotools.sh)
fi
for parties in 3 4; do
  cmake -S "$SOURCE" -B "$SOURCE/build-native-$parties" \
    -DPROTOCOL="$parties" -DCOMM=MPI -DTRIPLES=REAL -DDEFAULT_BITWIDTH=32
  cmake --build "$SOURCE/build-native-$parties" --parallel "$JOBS" --target test_sorting_revision
done
