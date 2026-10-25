#!/usr/bin/env bash

BENCH_SECRECY_PREPARED_IPS=""

bench_secrecy_prepare_node() {
  local ip="$1"
  local dir
  if [[ " ${BENCH_SECRECY_PREPARED_IPS} " == *" ${ip} "* ]]; then
    return 0
  fi

  dir="$(remote_system_dir Secrecy)"
  bench_log "Secrecy: clone/update and patch on ${ip}"
  bench_clone_or_update_system "${ip}" Secrecy
  bench_clone_or_update_system "${ip}" sql-parser
  bench_rsync_to "${ip}" "${ROOT_DIR}/systems/Secrecy/overlay/exp_sorting_micro.cpp" "${dir}/src/experiments/"
  bench_rsync_to "${ip}" "${ROOT_DIR}/systems/Secrecy/overlay/relational.cpp" "${dir}/src/core/"
  bench_ssh "${ip}" "set -euo pipefail
    mkdir -p '${BENCH_REMOTE_WORK_DIR}/build-logs'
    mkdir -p '${dir}/include/external-lib'
    if [[ ! -e '${dir}/include/external-lib/sql-parser' ]]; then
      ln -s '${BENCH_REMOTE_SRC_DIR}/sql-parser' '${dir}/include/external-lib/sql-parser'
    fi
    test \"\$(readlink -f '${dir}/include/external-lib/sql-parser')\" = '${BENCH_REMOTE_SRC_DIR}/sql-parser'
    mkdir -p '${dir}/build'
    cd '${dir}/build'
    if [[ '${BENCH_FORCE_REBUILD}' == '1' ]]; then
      rm -f exp_sorting_micro
    fi
    cmake .. > '${BENCH_REMOTE_WORK_DIR}/build-logs/secrecy_cmake.log' 2>&1
    make -j\"\$(nproc)\" exp_sorting_micro > '${BENCH_REMOTE_WORK_DIR}/build-logs/secrecy_make.log' 2>&1
  "
  BENCH_SECRECY_PREPARED_IPS="${BENCH_SECRECY_PREPARED_IPS} ${ip}"
}

bench_secrecy_prepare_nodes() {
  local ips=("$@")
  local ip pid rc=0
  local -a pids=()
  local -a launched=()

  for ip in "${ips[@]}"; do
    if [[ " ${BENCH_SECRECY_PREPARED_IPS} " == *" ${ip} "* ]]; then
      continue
    fi
    bench_secrecy_prepare_node "${ip}" &
    pids+=("$!")
    launched+=("${ip}")
  done

  for pid in "${pids[@]}"; do
    wait "${pid}" || rc=1
  done
  (( rc == 0 )) || return "${rc}"
  for ip in "${launched[@]}"; do
    if [[ " ${BENCH_SECRECY_PREPARED_IPS} " != *" ${ip} "* ]]; then
      BENCH_SECRECY_PREPARED_IPS="${BENCH_SECRECY_PREPARED_IPS} ${ip}"
    fi
  done
}

bench_secrecy_sync_case_data_all() {
  local input_file="$1"
  local ip pid rc=0
  local -a pids=()
  for ip in "$(party_ip 0)" "$(party_ip 1)" "$(party_ip 2)"; do
    bench_sync_case_data "${ip}" "${input_file}" &
    pids+=("$!")
  done
  for pid in "${pids[@]}"; do
    wait "${pid}" || rc=1
  done
  (( rc == 0 )) || return "${rc}"
}

bench_run_case() {
  local system="$1" group="$2" algorithm="$3" pow="$4" cols="$5" keys="$6" dtype="$7" dist="$8" bits="$9" input_file="${10}" slug="${11}" rep="${12}"
  : "${group}" "${dtype}" "${dist}" "${bits}"
  case "${algorithm}" in
    batcher|bitonic|odd_even|odd-even) ;;
    *)
      bench_log "Secrecy: skip unsupported sorting algorithm '${algorithm}' for ${slug}"
      return 0
      ;;
  esac

  local p0 p1 p2 dir input_remote log_dir log0 rows
  p0="$(party_ip 0)"
  p1="$(party_ip 1)"
  p2="$(party_ip 2)"
  dir="$(remote_system_dir Secrecy)"
  input_remote="$(remote_case_data_path "${input_file}")"
  log_dir="$(remote_case_log_dir "${system}" "${slug}" "${rep}")"
  log0="${log_dir}/mpirun.log"
  rows=$((1 << pow))

  bench_secrecy_prepare_nodes "${p0}" "${p1}" "${p2}"
  bench_secrecy_sync_case_data_all "${input_file}"

  bench_log "Secrecy: launch MPI 3PC from ${p0} over ${p0},${p1},${p2}"
  set +e
  bench_ssh "${p0}" "set -e; mkdir -p '${log_dir}'; cd '${dir}/build'; printf '%s slots=1\n%s slots=1\n%s slots=1\n' '${p0}' '${p1}' '${p2}' > '${log_dir}/hostfile'; timeout --kill-after=15s '${TIMEOUT_S}' mpirun --allow-run-as-root --mca plm_rsh_args '${BENCH_MPI_SSH_OPTIONS}' --mca btl_tcp_if_include '${BENCH_INTERFACE}' --mca oob_tcp_if_include '${BENCH_INTERFACE}' -np 3 --hostfile '${log_dir}/hostfile' ./exp_sorting_micro '${rows}' '${cols}' '${keys}' '${input_remote}' > '${log0}' 2>&1"
  local run_status=$?
  set -e

  bench_rsync_from "${p0}" "${log_dir}/" "${ROOT_DIR}/${BENCH_RAW_DIR}/${system}/${slug}/rep${rep}/p0/" || true
  if (( run_status != 0 )); then
    bench_log "Secrecy: failed ${slug} repeat=${rep}; status=${run_status}"
    return 1
  fi
  grep -q '^SORT_TUPLES_VERIFIED ' "${ROOT_DIR}/${BENCH_RAW_DIR}/${system}/${slug}/rep${rep}/p0/mpirun.log" || return 1
  return 0
}
