#!/usr/bin/env bash

BENCH_ORQ_PREPARED_IPS=""

bench_orq_prepare_node() {
  local ip="$1"
  local dir
  if [[ " ${BENCH_ORQ_PREPARED_IPS} " == *" ${ip} "* ]]; then
    return 0
  fi

  dir="$(remote_system_dir orq)"
  bench_log "ORQ: clone/update, patch and reuse/build on ${ip}"
  bench_clone_or_update_system "${ip}" orq
  bench_rsync_to "${ip}" "${ROOT_DIR}/systems/orq/overlay/test_sorting_micro.cpp" "${dir}/tests/"
  bench_rsync_to "${ip}" "${ROOT_DIR}/systems/orq/overlay/sorting.h" "${dir}/include/core/operators/"
  bench_rsync_to "${ip}" "${ROOT_DIR}/systems/orq/overlay/quicksort.h" "${dir}/include/core/operators/"
  bench_rsync_to "${ip}" "${ROOT_DIR}/systems/orq/overlay/radixsort.h" "${dir}/include/core/operators/"
  bench_ssh "${ip}" "set -euo pipefail
    mkdir -p '${BENCH_REMOTE_WORK_DIR}/build-logs'
    cd '${dir}'
    if [[ ! -f '${BENCH_REMOTE_WORK_DIR}/.orq_setup_done' || '${BENCH_FORCE_REBUILD}' == '1' ]]; then
      bash scripts/setup.sh > '${BENCH_REMOTE_WORK_DIR}/build-logs/orq_setup.log' 2>&1
      touch '${BENCH_REMOTE_WORK_DIR}/.orq_setup_done'
    fi
    build_sig_file='${BENCH_REMOTE_WORK_DIR}/.orq_build_signature'
    build_sig=\"\$(sha256sum \
      tests/test_sorting_micro.cpp \
      include/core/operators/sorting.h \
      include/core/operators/quicksort.h \
      include/core/operators/radixsort.h \
      | sha256sum | awk '{print \$1}')|PROTOCOL=3|COMM=MPI\"
    mkdir -p build
    cd build
    if [[ '${BENCH_FORCE_REBUILD}' == '1' ]]; then
      rm -f test_sorting_micro
    fi
    if [[ -x test_sorting_micro && -f \"\${build_sig_file}\" && \"\$(cat \"\${build_sig_file}\")\" == \"\${build_sig}\" ]]; then
      echo \"ORQ build reused: \${build_sig}\" > '${BENCH_REMOTE_WORK_DIR}/build-logs/orq_reuse.log'
    else
      cmake .. -DPROTOCOL=3 -DCOMM=MPI > '${BENCH_REMOTE_WORK_DIR}/build-logs/orq_cmake.log' 2>&1
      make -j\"\$(nproc)\" test_sorting_micro > '${BENCH_REMOTE_WORK_DIR}/build-logs/orq_make.log' 2>&1
      echo \"\${build_sig}\" > \"\${build_sig_file}\"
    fi
  "
  BENCH_ORQ_PREPARED_IPS="${BENCH_ORQ_PREPARED_IPS} ${ip}"
}

bench_orq_prepare_nodes() {
  local ips=("$@")
  local ip pid rc=0
  local -a pids=()
  local -a launched=()

  for ip in "${ips[@]}"; do
    if [[ " ${BENCH_ORQ_PREPARED_IPS} " == *" ${ip} "* ]]; then
      continue
    fi
    bench_orq_prepare_node "${ip}" &
    pids+=("$!")
    launched+=("${ip}")
  done

  for pid in "${pids[@]}"; do
    wait "${pid}" || rc=1
  done
  (( rc == 0 )) || return "${rc}"
  for ip in "${launched[@]}"; do
    if [[ " ${BENCH_ORQ_PREPARED_IPS} " != *" ${ip} "* ]]; then
      BENCH_ORQ_PREPARED_IPS="${BENCH_ORQ_PREPARED_IPS} ${ip}"
    fi
  done
}

bench_orq_sync_case_data_all() {
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
  : "${group}" "${cols}" "${dist}" "${bits}"
  case "${algorithm}" in
    bitonic|radix|quick) ;;
    *)
      bench_log "ORQ: skip unsupported sorting algorithm '${algorithm}' for ${slug}"
      return 0
      ;;
  esac

  local p0 p1 p2 dir input_remote log_dir rows
  p0="$(party_ip 0)"
  p1="$(party_ip 1)"
  p2="$(party_ip 2)"
  dir="$(remote_system_dir orq)"
  input_remote="$(remote_case_data_path "${input_file}")"
  log_dir="$(remote_case_log_dir "${system}" "${slug}" "${rep}")"
  rows=$((1 << pow))

  bench_orq_prepare_nodes "${p0}" "${p1}" "${p2}"
  bench_orq_sync_case_data_all "${input_file}"

  bench_log "ORQ: launch MPI 3PC from ${p0} over ${p0},${p1},${p2}, algorithm=${algorithm}"
  set +e
  bench_ssh "${p0}" "set -euo pipefail
    mkdir -p '${log_dir}'
    cd '${dir}/build'
    printf '%s slots=1\n%s slots=1\n%s slots=1\n' '${p0}' '${p1}' '${p2}' > '${log_dir}/hostfile'
    timeout --kill-after=15s '${TIMEOUT_S}' mpirun --allow-run-as-root --mca plm_rsh_args '${BENCH_MPI_SSH_OPTIONS}' --mca btl_tcp_if_include '${BENCH_INTERFACE}' --mca oob_tcp_if_include '${BENCH_INTERFACE}' -np 3 --hostfile '${log_dir}/hostfile' -x FES_PUBLIC_BENCHMARK -x FES_INPUT_SHA256 \
      ./test_sorting_micro 1 1 -12 '${rows}' \
      --input '${input_remote}' \
      --sort-protocol '${algorithm}' \
      --num-keys '${keys}' \
      --data-type '${dtype}' > '${log_dir}/mpirun.log' 2>&1
  "
  local run_status=$?
  set -e

  bench_rsync_from "${p0}" "${log_dir}/" "${ROOT_DIR}/${BENCH_RAW_DIR}/${system}/${slug}/rep${rep}/p0/" || true
  if (( run_status != 0 )); then
    bench_log "ORQ: failed ${slug} repeat=${rep}; status=${run_status}"
    return 1
  fi
  return 0
}
