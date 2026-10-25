#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
EMP_PIN_MARKER="/usr/local/share/datafedsort/alchemy_emp_pinned.env"
EMP_TOOL_REF="${ALCHEMY_EMP_TOOL_REF:-11093a7d2160e7e7a4dcae3ffd9e6935bf2b8c1c}"
EMP_OT_REF="${ALCHEMY_EMP_OT_REF:-52b32c8371c09c1567e3d650c0f0adfbb229a270}"
EMP_SH2PC_REF="${ALCHEMY_EMP_SH2PC_REF:-1fd0101e77c1dc008ab07288ec49c62fa0c5546c}"
EMP_ZK_REF="${ALCHEMY_EMP_ZK_REF:-73ab193a923d4c122be5a4f6bc1fe4f617966b02}"
mkdir -p "${ROOT_DIR}/deps"

if [[ ! -f /usr/local/include/emp-tool/emp-tool.h ]] || \
   ! ldconfig -p 2>/dev/null | grep -q 'libemp-tool' || \
   [[ ! -f "${EMP_PIN_MARKER}" ]] || \
   ! grep -qx "EMP_TOOL_REF=${EMP_TOOL_REF}" "${EMP_PIN_MARKER}" || \
   ! grep -qx "EMP_OT_REF=${EMP_OT_REF}" "${EMP_PIN_MARKER}" || \
   ! grep -qx "EMP_SH2PC_REF=${EMP_SH2PC_REF}" "${EMP_PIN_MARKER}" || \
   ! grep -qx "EMP_ZK_REF=${EMP_ZK_REF}" "${EMP_PIN_MARKER}"; then
  cd "${ROOT_DIR}/deps/emp"
  /bin/bash install.sh
  ldconfig || true
fi

if ! ldconfig -p 2>/dev/null | grep -q 'libpqxx'; then
  cd "${ROOT_DIR}/deps"
  if [[ ! -d libpqxx/.git ]]; then
    git clone --branch 7.10.1 https://github.com/jtv/libpqxx.git
  fi
  cd libpqxx
  ./configure --disable-documentation --enable-shared CXXFLAGS="-std=c++17 -O2"
  make -j"$(nproc)"
  make install
  ldconfig || true
fi

if [[ ! -f /usr/local/lib/libgtest.a && ! -f /usr/local/lib64/libgtest.a ]]; then
  cd "${ROOT_DIR}/deps"
  if [[ ! -d googletest/.git ]]; then
    git clone --branch v1.13.0 https://github.com/google/googletest.git
  fi
  cd googletest
  cmake .
  make -j"$(nproc)"
  make install
  ldconfig || true
fi

echo "Setup completed successfully."
