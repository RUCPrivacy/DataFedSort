#!/usr/bin/env bash
# Checked three-party and four-party ORQ profile, kept separate from base overlays.
BENCH_ORQ_PREPARED_IPS=""

bench_orq_prepare_node() {
  local host="$1" directory
  [[ " ${BENCH_ORQ_PREPARED_IPS} " == *" ${host} "* ]] && return 0
  directory="$(remote_system_dir orq)"
  bench_clone_or_update_system "${host}" orq
  bench_ssh "${host}" "mkdir -p '${directory}/tests' '${directory}/benchmark-support'"
  bench_rsync_to "${host}" "${ROOT_DIR}/systems/orq/native-overlay/tests/test_sorting_revision.cpp" "${directory}/tests/"
  bench_rsync_to "${host}" "${ROOT_DIR}/systems/orq/support/select_bitonic.py" "${directory}/benchmark-support/"
  bench_rsync_to "${host}" "${ROOT_DIR}/systems/orq/support/build_native.sh" "${directory}/benchmark-support/"
  bench_ssh "${host}" "set -euo pipefail; bash '${directory}/benchmark-support/build_native.sh' '${directory}' > '${BENCH_REMOTE_WORK_DIR}/build-logs/orq_native.log' 2>&1; test -x '${directory}/build-native-3/test_sorting_revision'; test -x '${directory}/build-native-4/test_sorting_revision'"
  BENCH_ORQ_PREPARED_IPS+=" ${host}"
}

bench_run_case() {
  local system="$1" group="$2" algorithm="$3" power="$4" columns="$5" keys="$6" dtype="$7" distribution="$8" bits="$9" input="${10}" slug="${11}" repeat="${12}"
  local count=3 directory log_dir hosts='' host rank rc=0
  [[ "${BENCH_SECURITY}" == MAL ]] && count=4
  directory="$(remote_system_dir orq)"
  log_dir="$(remote_case_log_dir "${system}" "${slug}" "${repeat}")"
  for ((rank=0;rank<count;rank++)); do
    host="$(party_ip "${rank}")"
    bench_sync_case_data "${host}" "${input}"
    bench_ssh "${host}" "mkdir -p '${log_dir}'"
    [[ -n "${hosts}" ]] && hosts+=,
    hosts+="${host}:1"
  done
  bench_ssh "$(party_ip 0)" "set -euo pipefail
    cd '${directory}/build-native-${count}'
    export FES_CASE_INPUT='$(remote_case_data_path "${input}")'
    export FES_CASE_ALGORITHM='${algorithm}'
    timeout --kill-after=15s '${TIMEOUT_S}' mpirun --allow-run-as-root \
      --mca plm_rsh_args '${BENCH_MPI_SSH_OPTIONS}' \
      --mca btl_tcp_if_include '${BENCH_INTERFACE}' --mca oob_tcp_if_include '${BENCH_INTERFACE}' \
      -np '${count}' --host '${hosts}' -x FES_CASE_INPUT -x FES_CASE_ALGORITHM \
      ./test_sorting_revision -t 1 -b -12 -s lan > '${log_dir}/mpirun.log' 2>&1
  " || rc=$?
  bench_rsync_from "$(party_ip 0)" "${log_dir}/" "${ROOT_DIR}/${BENCH_RAW_DIR}/${system}/${slug}/rep${repeat}/p0/" || true
  return "${rc}"
}
