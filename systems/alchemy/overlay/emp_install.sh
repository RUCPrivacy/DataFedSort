#!/usr/bin/env bash
set -euo pipefail

INSTALL_DEPS="${ALCHEMY_EMP_INSTALL_DEPS:-1}"

install_packages() {
  if [[ "${INSTALL_DEPS}" != "1" ]]; then
    return 0
  fi

  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y cmake git build-essential libssl-dev
  elif command -v yum >/dev/null 2>&1; then
    yum install -y python3 gcc make git cmake gcc-c++ openssl-devel
  else
    echo "System not supported yet!" >&2
    return 1
  fi
}

checkout_pinned_commit() {
  local repo="$1"
  local ref="$2"
  local dir="emp-${repo}"
  local key="EMP_${repo^^}"
  local url="https://github.com/emp-toolkit/${dir}.git"
  [[ "${ref}" =~ ^[0-9a-f]{40}$ ]] || {
    echo "EMP dependencies require exact commit IDs from dependencies/lock.json" >&2
    return 1
  }

  if [[ -f "${dir}/.datafedsort-dependency.json" ]]; then
    python3 - "${dir}/.datafedsort-dependency.json" "${dir}" "${ref}" <<'PY'
import json,sys
record=json.load(open(sys.argv[1]))
if record != {"dependency":sys.argv[2],"revision":sys.argv[3]}:
    raise SystemExit("EMP source mount does not match the locked revision")
PY
  elif [[ ! -d "${dir}/.git" ]]; then
    test ! -e "${dir}"
    git init --quiet "${dir}"
    git -C "${dir}" remote add origin "${url}"
    git -C "${dir}" fetch --depth=1 origin "${ref}"
    git -C "${dir}" checkout --detach "${ref}"
  else
    if [[ "$(git -C "${dir}" rev-parse HEAD)" != "${ref}" ]]; then
      echo "Existing ${dir} checkout has another revision; use a fresh dependency workspace" >&2
      return 1
    fi
  fi

  printf '%s_REF=%s\n' "${key}" "${ref}"
  printf '%s_COMMIT=%s\n' "${key}" "${ref}"
}

build_and_install() {
  local repo="$1"
  local dir="emp-${repo}"

  cmake -S "${dir}" -B "${dir}/build" -DCMAKE_POLICY_VERSION_MINIMUM=3.5
  cmake --build "${dir}/build" -j"$(nproc)"
  cmake --install "${dir}/build"
}

main() {
  local marker_dir="/usr/local/share/datafedsort"
  local marker="${marker_dir}/alchemy_emp_pinned.env"
  local tmp_marker

  install_packages
  # Reuse installed libraries only when their exact source pins match.
  tmp_marker="$(mktemp)"
  {
    checkout_pinned_commit tool "${ALCHEMY_EMP_TOOL_REF:-11093a7d2160e7e7a4dcae3ffd9e6935bf2b8c1c}"
    checkout_pinned_commit ot "${ALCHEMY_EMP_OT_REF:-52b32c8371c09c1567e3d650c0f0adfbb229a270}"
    checkout_pinned_commit sh2pc "${ALCHEMY_EMP_SH2PC_REF:-1fd0101e77c1dc008ab07288ec49c62fa0c5546c}"
    checkout_pinned_commit zk "${ALCHEMY_EMP_ZK_REF:-73ab193a923d4c122be5a4f6bc1fe4f617966b02}"
  } | tee "${tmp_marker}"

  build_and_install tool
  build_and_install ot
  build_and_install sh2pc
  build_and_install zk

  mkdir -p "${marker_dir}"
  cp "${tmp_marker}" "${marker}"
  rm -f "${tmp_marker}"
}

main "$@"
