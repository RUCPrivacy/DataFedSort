#!/usr/bin/env bash

BENCH_MASQUE_PREPARED_IPS=""

bench_masque_prepare_node() {
  local ip="$1"
  local dir
  if [[ " ${BENCH_MASQUE_PREPARED_IPS} " == *" ${ip} "* ]]; then
    return 0
  fi

  dir="$(remote_system_dir MASQUE)"
  bench_log "MASQUE: clone/update and patch on ${ip}"
  bench_clone_or_update_system "${ip}" MASQUE
  bench_rsync_to "${ip}" "${ROOT_DIR}/systems/MASQUE/overlay/test_sorting_micro.cpp" "${dir}/src/test/"
  bench_rsync_to "${ip}" "${ROOT_DIR}/systems/MASQUE/overlay/CMakeLists.txt" "${dir}/src/test/"
  bench_rsync_to "${ip}" "${ROOT_DIR}/systems/MASQUE/overlay/ShuffleQSort.cpp" "${dir}/src/core/Sort/"
  bench_ssh "${ip}" "set -euo pipefail
    mkdir -p '${BENCH_REMOTE_WORK_DIR}/build-logs'
    cd '${dir}'
    git submodule update --init --recursive > '${BENCH_REMOTE_WORK_DIR}/build-logs/masque_submodule.log' 2>&1
    mkdir -p build
    cd build
    if [[ '${BENCH_FORCE_REBUILD}' == '1' ]]; then
      rm -f test_sorting_micro
    fi
    cmake .. -DCMAKE_POLICY_VERSION_MINIMUM=3.5 > '${BENCH_REMOTE_WORK_DIR}/build-logs/masque_cmake.log' 2>&1
    make -j\"\$(nproc)\" test_sorting_micro > '${BENCH_REMOTE_WORK_DIR}/build-logs/masque_make.log' 2>&1
  "
  BENCH_MASQUE_PREPARED_IPS="${BENCH_MASQUE_PREPARED_IPS} ${ip}"
}

bench_masque_prepare_nodes() {
  local ips=("$@")
  local ip pid rc=0
  local -a pids=()
  local -a launched=()

  for ip in "${ips[@]}"; do
    if [[ " ${BENCH_MASQUE_PREPARED_IPS} " == *" ${ip} "* ]]; then
      continue
    fi
    bench_masque_prepare_node "${ip}" &
    pids+=("$!")
    launched+=("${ip}")
  done

  for pid in "${pids[@]}"; do
    wait "${pid}" || rc=1
  done
  (( rc == 0 )) || return "${rc}"
  for ip in "${launched[@]}"; do
    if [[ " ${BENCH_MASQUE_PREPARED_IPS} " != *" ${ip} "* ]]; then
      BENCH_MASQUE_PREPARED_IPS="${BENCH_MASQUE_PREPARED_IPS} ${ip}"
    fi
  done
}

bench_masque_sync_case_data_all() {
  local input_file="$1"
  local ip pid rc=0
  local -a pids=()
  for ip in "$(party_ip 0)" "$(party_ip 1)"; do
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
  : "${group}" "${pow}" "${cols}" "${dist}" "${bits}"

  local p0 p1 dir input_remote log0 log1 port masque_alg
  p0="$(party_ip 0)"
  p1="$(party_ip 1)"
  dir="$(remote_system_dir MASQUE)"
  input_remote="$(remote_case_data_path "${input_file}")"
  log0="$(remote_case_log_dir "${system}" "${slug}" "${rep}")/server.log"
  log1="$(remote_case_log_dir "${system}" "${slug}" "${rep}")/client.log"
  port=$((MASQUE_PORT + rep))

  if [[ "${group}" == "R6_keys" || "${slug}" == R6_keys_* ]]; then
    bench_log "MASQUE: skip ${slug}; native MASQUE sort supports one 64-bit sort key, not multi-key sorting"
    return 0
  fi
  if [[ "${keys}" != "1" ]]; then
    bench_log "MASQUE: skip ${slug}; native MASQUE sort supports exactly one sort key"
    return 0
  fi

  case "${algorithm}" in
    batcher|bitonic|odd_even|odd-even) masque_alg="opt" ;;
    quick) masque_alg="quick" ;;
    *)
      bench_log "MASQUE: skip unsupported sorting algorithm '${algorithm}' for ${slug}"
      return 0
      ;;
  esac

  bench_masque_prepare_nodes "${p0}" "${p1}"
  bench_masque_sync_case_data_all "${input_file}"

  bench_log "MASQUE: launch server on ${p0}, client on ${p1}, port=${port}, algorithm=${masque_alg}"
  set +e
  bench_ssh "${p0}" "mkdir -p '$(remote_case_log_dir "${system}" "${slug}" "${rep}")'; cd '${dir}/build'; export FES_PUBLIC_TPCH_INPUT='${FES_PUBLIC_TPCH_INPUT:-0}'; /usr/bin/time -v -o '${log0}.time' timeout --kill-after=15s '${TIMEOUT_S}' ./test_sorting_micro 0 --address '${p0}' --port '${port}' --input '${input_remote}' --algorithm '${masque_alg}' --num-keys '${keys}' --data-type '${dtype}' > '${log0}' 2>&1" &
  local server_ssh_pid=$!
  sleep 2
  bench_ssh "${p1}" "mkdir -p '$(remote_case_log_dir "${system}" "${slug}" "${rep}")'; cd '${dir}/build'; export FES_PUBLIC_TPCH_INPUT='${FES_PUBLIC_TPCH_INPUT:-0}'; /usr/bin/time -v -o '${log1}.time' timeout --kill-after=15s '${TIMEOUT_S}' ./test_sorting_micro 1 --address '${p0}' --port '${port}' --input '${input_remote}' --algorithm '${masque_alg}' --num-keys '${keys}' --data-type '${dtype}' > '${log1}' 2>&1"
  local client_status=$?
  wait "${server_ssh_pid}"
  local server_status=$?
  set -e

  bench_rsync_from "${p0}" "$(remote_case_log_dir "${system}" "${slug}" "${rep}")/" "${ROOT_DIR}/${BENCH_RAW_DIR}/${system}/${slug}/rep${rep}/p0/" || true
  bench_rsync_from "${p1}" "$(remote_case_log_dir "${system}" "${slug}" "${rep}")/" "${ROOT_DIR}/${BENCH_RAW_DIR}/${system}/${slug}/rep${rep}/p1/" || true
  if (( server_status != 0 || client_status != 0 )); then
    bench_log "MASQUE: failed ${slug} repeat=${rep}; party_statuses=${server_status},${client_status}"
    return 1
  fi
  return 0
}
