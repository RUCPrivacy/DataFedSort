#!/usr/bin/env bash

BENCH_MP_SPDZ_PREPARED_IPS=""
BENCH_MP_SPDZ_CERTS_READY=0

mp_spdz_protocol_bin() {
  case "${MP_SPDZ_PROTOCOL}" in
    replicated-field-party|replicated-field-party.x) printf '%s\n' "replicated-field-party.x" ;;
    malicious-rep-field-party|malicious-rep-field-party.x) printf '%s\n' "malicious-rep-field-party.x" ;;
    *) printf '%s.x\n' "${MP_SPDZ_PROTOCOL%.x}" ;;
  esac
}

bench_mp_spdz_prepare_certs() {
  local p0 dir tmp
  if [[ "${BENCH_MP_SPDZ_CERTS_READY}" == "1" ]]; then
    return 0
  fi

  p0="$(party_ip 0)"
  dir="$(remote_system_dir MP-SPDZ)"
  tmp="${ROOT_DIR}/${BENCH_RESULTS_DIR}/tmp/MP-SPDZ/Player-Data"
  mkdir -p "${tmp}"

  bench_log "MP-SPDZ: prepare SSL material on ${p0}"
  bench_clone_or_update_system "${p0}" MP-SPDZ
  bench_ssh "${p0}" "set -euo pipefail
    mkdir -p '${BENCH_REMOTE_WORK_DIR}/build-logs'
    cd '${dir}'
    if [[ '${BENCH_FORCE_REBUILD}' == '1' || ! -f Player-Data/P0.pem || ! -f Player-Data/P1.pem || ! -f Player-Data/P2.pem ]]; then
      bash Scripts/setup-ssl.sh 3 > '${BENCH_REMOTE_WORK_DIR}/build-logs/mp-spdz_setup_ssl.log' 2>&1
    fi
  "
  bench_rsync_from "${p0}" "${dir}/Player-Data/" "${tmp}/"

  local ip pid rc=0
  local -a pids=()
  for ip in $(party_ips 3); do
    (
      bench_clone_or_update_system "${ip}" MP-SPDZ
      bench_rsync_to "${ip}" "${tmp}/" "${dir}/Player-Data/"
    ) &
    pids+=("$!")
  done
  for pid in "${pids[@]}"; do
    wait "${pid}" || rc=1
  done
  (( rc == 0 )) || return "${rc}"
  BENCH_MP_SPDZ_CERTS_READY="1"
}

bench_mp_spdz_prepare_node() {
  local ip="$1"
  local dir protocol_bin
  if [[ " ${BENCH_MP_SPDZ_PREPARED_IPS} " == *" ${ip} "* ]]; then
    return 0
  fi

  dir="$(remote_system_dir MP-SPDZ)"
  protocol_bin="$(mp_spdz_protocol_bin)"
  bench_log "MP-SPDZ: clone/update, patch and build on ${ip}"
  bench_clone_or_update_system "${ip}" MP-SPDZ
  bench_rsync_to "${ip}" "${ROOT_DIR}/systems/MP-SPDZ/overlay/sorting.py" "${dir}/Compiler/"
  bench_rsync_to "${ip}" "${ROOT_DIR}/systems/MP-SPDZ/overlay/library.py" "${dir}/Compiler/"
  bench_rsync_to "${ip}" "${ROOT_DIR}/systems/MP-SPDZ/overlay/Programs/Source/sorting_micro_test.mpc" "${dir}/Programs/Source/"
  bench_rsync_to "${ip}" "${ROOT_DIR}/runtime/native/mp_spdz/instrument_vm.py" "${dir}/Processor/"
  bench_rsync_to "${ip}" "${ROOT_DIR}/runtime/native/mp_spdz/rss_window.h" "${dir}/Processor/"
  bench_ssh "${ip}" "set -euo pipefail
    mkdir -p '${BENCH_REMOTE_WORK_DIR}/build-logs'
    cd '${dir}'
    python3 Processor/instrument_vm.py '${dir}'
    if [[ '${BENCH_FORCE_REBUILD}' == '1' ]]; then
      rm -f '${protocol_bin}'
    fi
    make '${protocol_bin}' > '${BENCH_REMOTE_WORK_DIR}/build-logs/mp-spdz_make_${protocol_bin}.log' 2>&1
  "
  BENCH_MP_SPDZ_PREPARED_IPS="${BENCH_MP_SPDZ_PREPARED_IPS} ${ip}"
}

bench_mp_spdz_prepare_nodes() {
  local ips=("$@")
  local ip pid rc=0
  local -a pids=()
  local -a launched=()

  for ip in "${ips[@]}"; do
    if [[ " ${BENCH_MP_SPDZ_PREPARED_IPS} " == *" ${ip} "* ]]; then
      continue
    fi
    bench_mp_spdz_prepare_node "${ip}" &
    pids+=("$!")
    launched+=("${ip}")
  done

  for pid in "${pids[@]}"; do
    wait "${pid}" || rc=1
  done
  (( rc == 0 )) || return "${rc}"
  for ip in "${launched[@]}"; do
    if [[ " ${BENCH_MP_SPDZ_PREPARED_IPS} " != *" ${ip} "* ]]; then
      BENCH_MP_SPDZ_PREPARED_IPS="${BENCH_MP_SPDZ_PREPARED_IPS} ${ip}"
    fi
  done
}

bench_mp_spdz_stage_case_all() {
  local input_file="$1" rows="$2" cols="$3" algorithm="$4" bits="$5" log_dir="$6"
  local ip dir pid rc=0
  local -a pids=()

  dir="$(remote_system_dir MP-SPDZ)"
  for ip in "$(party_ip 0)" "$(party_ip 1)" "$(party_ip 2)"; do
    (
      bench_sync_case_data "${ip}" "${input_file}"
      bench_ssh "${ip}" "set -euo pipefail
        mkdir -p '${log_dir}'
        cd '${dir}'
        PYTHONPATH=\"\$PWD\${PYTHONPATH:+:\$PYTHONPATH}\" python3 ./Programs/Source/sorting_micro_test.mpc \
          --field '${MP_SPDZ_COMPARE_BITS:-64}' \
          --input '$(remote_case_data_path "${input_file}")' \
          --rows '${rows}' \
          --cols '${cols}' \
          --sort-col 0 \
          --algorithm '${algorithm}' \
          --n-bits '${bits}' > '${log_dir}/compile_p${ip##*.}.log' 2>&1
      "
    ) &
    pids+=("$!")
  done
  for pid in "${pids[@]}"; do
    wait "${pid}" || rc=1
  done
  (( rc == 0 )) || return "${rc}"
}

bench_run_case() {
  local system="$1" group="$2" algorithm="$3" pow="$4" cols="$5" keys="$6" dtype="$7" dist="$8" bits="$9" input_file="${10}" slug="${11}" rep="${12}"
  : "${group}" "${dtype}" "${dist}"
  case "${algorithm}" in
    batcher|radix) ;;
    *)
      bench_log "MP-SPDZ: skip unsupported sorting algorithm '${algorithm}' for ${slug}"
      return 0
      ;;
  esac
  if [[ "${keys}" != "1" ]]; then
    bench_log "MP-SPDZ: skip ${slug}; current MP-SPDZ table.sort harness supports one key column"
    return 0
  fi

  local p0 p1 p2 dir protocol_bin input_remote log_dir program port rows
  p0="$(party_ip 0)"
  p1="$(party_ip 1)"
  p2="$(party_ip 2)"
  dir="$(remote_system_dir MP-SPDZ)"
  protocol_bin="$(mp_spdz_protocol_bin)"
  input_remote="$(remote_case_data_path "${input_file}")"
  log_dir="$(remote_case_log_dir "${system}" "${slug}" "${rep}")"
  program="sorting_micro_test"
  port=$((MP_SPDZ_PORT + rep))
  rows=$((1 << pow))

  bench_mp_spdz_prepare_certs
  bench_mp_spdz_prepare_nodes "${p0}" "${p1}" "${p2}"
  bench_mp_spdz_stage_case_all "${input_file}" "${rows}" "${cols}" "${algorithm}" "${bits}" "${log_dir}"

  bench_log "MP-SPDZ: launch 3PC ${protocol_bin} over ${p0},${p1},${p2}, port=${port}"
  set +e
  bench_ssh "${p1}" "set -e; mkdir -p '${log_dir}'; cd '${dir}'; /usr/bin/time -v -o '${log_dir}/party1.time' timeout --kill-after=15s '${TIMEOUT_S}' ./'${protocol_bin}' 1 '${program}' -lgp '${MP_SPDZ_PRIME_BITS:-128}' -pn '${port}' -h '${p0}' > '${log_dir}/party1.log' 2>&1" &
  local p1_pid=$!
  bench_ssh "${p2}" "set -e; mkdir -p '${log_dir}'; cd '${dir}'; /usr/bin/time -v -o '${log_dir}/party2.time' timeout --kill-after=15s '${TIMEOUT_S}' ./'${protocol_bin}' 2 '${program}' -lgp '${MP_SPDZ_PRIME_BITS:-128}' -pn '${port}' -h '${p0}' > '${log_dir}/party2.log' 2>&1" &
  local p2_pid=$!
  sleep 2
  bench_ssh "${p0}" "set -e; mkdir -p '${log_dir}'; cd '${dir}'; /usr/bin/time -v -o '${log_dir}/party0.time' timeout --kill-after=15s '${TIMEOUT_S}' ./'${protocol_bin}' 0 '${program}' -lgp '${MP_SPDZ_PRIME_BITS:-128}' -pn '${port}' -h '${p0}' > '${log_dir}/party0.log' 2>&1"
  local p0_status=$?
  wait "${p1_pid}"
  local p1_status=$?
  wait "${p2_pid}"
  local p2_status=$?
  set -e

  bench_rsync_from "${p0}" "${log_dir}/" "${ROOT_DIR}/${BENCH_RAW_DIR}/${system}/${slug}/rep${rep}/p0/" || true
  bench_rsync_from "${p1}" "${log_dir}/" "${ROOT_DIR}/${BENCH_RAW_DIR}/${system}/${slug}/rep${rep}/p1/" || true
  bench_rsync_from "${p2}" "${log_dir}/" "${ROOT_DIR}/${BENCH_RAW_DIR}/${system}/${slug}/rep${rep}/p2/" || true

  if (( p0_status != 0 || p1_status != 0 || p2_status != 0 )); then
    bench_log "MP-SPDZ: failed ${slug} repeat=${rep}; party_statuses=${p0_status},${p1_status},${p2_status}"
    return 1
  fi

  local local_rep_dir aggregate_log
  local_rep_dir="${ROOT_DIR}/${BENCH_RAW_DIR}/${system}/${slug}/rep${rep}"
  aggregate_log="${local_rep_dir}/aggregate.log"
  python3 "${ROOT_DIR}/runtime/native/mp_spdz/collect.py" \
    --folder "${local_rep_dir}" --input "${ROOT_DIR}/${input_file}" \
    --system "${system}" --case "${slug}" > "${aggregate_log}"
  return 0
}
