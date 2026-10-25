#!/usr/bin/env bash
# Transport and pinned-source utilities shared by the benchmark adapters.
set -euo pipefail
RUNTIME_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${RUNTIME_DIR}/.." && pwd)"

bench_log() { printf '[%s] %s\n' "$(date -u '+%FT%TZ')" "$*"; }
bench_die() { bench_log "ERROR: $*" >&2; return 1; }
ssh_target() { if [[ -n "${SSH_USER:-}" ]]; then printf '%s@%s' "${SSH_USER}" "$1"; else printf '%s' "$1"; fi; }

bench_init_transport() {
  local configuration="$1"
  local environment
  environment="$(python3 "${RUNTIME_DIR}/config_env.py" "${configuration}")" || return
  eval "${environment}"
  mapfile -d '' -t BENCH_SSH_OPTIONS < <(python3 -c 'import json,os,sys; [sys.stdout.buffer.write(s.encode()+b"\0") for s in json.loads(os.environ["BENCH_SSH_OPTIONS_JSON"])]')
  BENCH_REMOTE_SRC_DIR="${BENCH_REMOTE_WORK_DIR}/src"
  BENCH_REMOTE_DATA_DIR="${BENCH_REMOTE_WORK_DIR}/inputs"
  BENCH_REMOTE_RESULTS_DIR="${BENCH_REMOTE_WORK_DIR}/runs/${RUN_ID}"
  export BENCH_REMOTE_SRC_DIR BENCH_REMOTE_DATA_DIR BENCH_REMOTE_RESULTS_DIR
}

bench_ssh() {
  local host="$1"; shift
  local environment='' name value rc=0
  for name in FES_PUBLIC_BENCHMARK FES_INPUT_SHA256 FES_REVISION_BENCH FES_PUBLIC_TPCH_INPUT; do
    if [[ -v "${name}" ]]; then
      printf -v value '%q' "${!name}"
      environment+="export ${name}=${value}; "
    fi
  done
  ssh -n "${BENCH_SSH_OPTIONS[@]}" "$(ssh_target "${host}")" "${environment}$*" || rc=$?
  if [[ "${rc}" == 124 && -n "${BENCH_CURRENT_CASE_DIR:-}" ]]; then
    touch "${BENCH_CURRENT_CASE_DIR}/.timeout-observed"
  fi
  return "${rc}"
}
bench_rsync_to() {
  local host="$1" source="$2" destination="$3"; shift 3
  local transport
  printf -v transport '%q ' ssh "${BENCH_SSH_OPTIONS[@]}"
  rsync -az "$@" -e "${transport}" "${source}" "$(ssh_target "${host}"):${destination}"
}
bench_rsync_from() {
  local host="$1" source="$2" destination="$3"
  local transport
  mkdir -p "${destination}"
  printf -v transport '%q ' ssh "${BENCH_SSH_OPTIONS[@]}"
  rsync -az -e "${transport}" "$(ssh_target "${host}"):${source}" "${destination}"
}

party_ip() { case "$1" in 0) printf '%s\n' "${P0_IP}";; 1) printf '%s\n' "${P1_IP}";; 2) printf '%s\n' "${P2_IP}";; 3) printf '%s\n' "${P3_IP}";; *) bench_die 'Party rank must be between zero and three';; esac; }
party_ips() { local i; for ((i=0;i<${1:-3};i++)); do party_ip "${i}"; done; }
remote_system_dir() {
  if [[ "$1" == orq && "${BENCH_SOURCE_PROFILE:-base}" == native_checked ]]; then printf '%s/orq-native\n' "${BENCH_REMOTE_SRC_DIR}";
  else printf '%s/%s\n' "${BENCH_REMOTE_SRC_DIR}" "$1"; fi
}
remote_case_data_path() { printf '%s/%s/%s\n' "${BENCH_REMOTE_DATA_DIR}" "${RUN_ID}" "$(basename "$1")"; }
remote_case_log_dir() { printf '%s/%s/%s/rep%s\n' "${BENCH_REMOTE_RESULTS_DIR}" "$1" "$2" "$3"; }

bench_cleanup_node() {
  local host="$1" system="$2" encoded
  encoded="$(base64 -w0 "${RUNTIME_DIR}/native/cleanup_owned.py")"
  bench_ssh "${host}" "python3 -c \"import base64; exec(base64.b64decode('${encoded}'))\" '${BENCH_REMOTE_WORK_DIR}' '${system}'"
}

bench_cleanup_system() {
  local system="$1" count="$2" rank rc=0
  [[ "${system}:${BENCH_SOURCE_PROFILE:-base}" != orq:native_checked ]] || system=orq-native
  for ((rank=0;rank<count;rank++)); do
    bench_cleanup_node "$(party_ip "${rank}")" "${system}" || rc=1
  done
  return "${rc}"
}

bench_clone_or_update_system() {
  local host="$1" system="$2" url pin directory
  directory="$(remote_system_dir "${system}")"
  read -r url pin < <(python3 - "${ROOT_DIR}/dependencies/lock.json" "${system}" "${BENCH_SOURCE_PROFILE:-base}" <<'PY'
import json,sys
item=json.load(open(sys.argv[1]))['dependencies'][sys.argv[2]]
if sys.argv[2]=='orq' and sys.argv[3]!='base':
    item=dict(item,**item['profiles'][sys.argv[3]])
print(item['url'],item['revision'])
PY
  )
  [[ "${url}" == https://github.com/* && "${pin}" =~ ^[0-9a-f]{40}$ ]] || bench_die 'Invalid upstream URL or pin'
  bench_ssh "${host}" "set -e; mkdir -p '${BENCH_REMOTE_SRC_DIR}' '${BENCH_REMOTE_WORK_DIR}/build-logs'; touch '${BENCH_REMOTE_WORK_DIR}/.datafedsort-runtime'; if [[ ! -e '${directory}' ]]; then git clone --no-checkout '${url}' '${directory}'; git -C '${directory}' checkout --detach '${pin}'; git -C '${directory}' submodule update --init --recursive; fi; test -d '${directory}/.git'; test \"\$(git -C '${directory}' rev-parse HEAD)\" = '${pin}'"
}

bench_sync_case_data() {
  local host="$1" input="$2" remote hash
  remote="$(remote_case_data_path "${input}")"
  [[ -f "${ROOT_DIR}/${input}" && -f "${ROOT_DIR}/${input}.manifest.json" ]] || bench_die 'Verified input and provenance sidecar are required'
  hash="${FES_INPUT_SHA256:?Verified input hash is required}"
  [[ "$(sha256sum "${ROOT_DIR}/${input}" | awk '{print $1}')" == "${hash}" ]] || bench_die 'Input changed after manifest verification'
  bench_ssh "${host}" "mkdir -p '$(dirname "${remote}")'"
  bench_rsync_to "${host}" "${ROOT_DIR}/${input}" "${remote}" --checksum
  bench_rsync_to "${host}" "${ROOT_DIR}/${input}.manifest.json" "${remote}.manifest.json" --checksum
  bench_ssh "${host}" "test \"\$(sha256sum '${remote}' | awk '{print \$1}')\" = '${hash}'"
}

bench_prepare_system() {
  local system="$1" count="$2" host prefix
  local -a hosts=()
  prefix="${system,,}"
  case "${system}" in MP-SPDZ) prefix=mp_spdz;; SMPCache) prefix=smpcache;; MASQUE) prefix=masque;; esac
  if [[ "${system}" == MP-SPDZ ]]; then bench_mp_spdz_prepare_certs; fi
  if declare -F "bench_${prefix}_prepare_nodes" >/dev/null; then
    mapfile -t hosts < <(party_ips "${count}")
    "bench_${prefix}_prepare_nodes" "${hosts[@]}"
  else
    for host in $(party_ips "${count}"); do "bench_${prefix}_prepare_node" "${host}"; done
  fi
}
