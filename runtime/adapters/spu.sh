#!/usr/bin/env bash

BENCH_SPU_PREPARED_IPS=""
SPU_PORT="${SPU_PORT:-19140}"

bench_spu_method_name() {
  case "$1" in
    odd_even|odd-even|batcher) printf '%s\n' "SORT_NETWORK" ;;
    radix) printf '%s\n' "SORT_RADIX" ;;
    quick) printf '%s\n' "SORT_QUICK" ;;
    *) return 1 ;;
  esac
}

bench_spu_gtest_filter() {
  local group="$1" algorithm="$2" pow="$3" cols="$4" keys="$5" dtype="$6" dist="$7" input_file="${8:-}"
  local method payloads filename
  method="$(bench_spu_method_name "${algorithm}")" || return 1
  case "${group}" in
    R2_algorithm_odd_even|R3_algorithm_radix|R4_algorithm_quick)
      printf '*%s_%s*' "${group}" "${method}"
      ;;
    R5_rows)
      printf '*R5_Rows_N2pow%s_%s*' "${pow}" "${method}"
      ;;
    R6_keys)
      printf '*R6_Keys_K%s_%s*' "${keys}" "${method}"
      ;;
    R7_payloads)
      payloads=$((cols - keys))
      printf '*R7_Payloads_P%s_%s*' "${payloads}" "${method}"
      ;;
    R8_key_type)
      printf '*R8_KeyType_%s_%s*' "${dtype}" "${method}"
      ;;
    R9_zipf|R9_partially_sorted|R9_high_repeat)
      [[ -n "${input_file}" ]] || return 1
      filename="$(basename "${input_file}")"
      filename="$(printf '%s' "${filename}" | LC_ALL=C tr -c '[:alnum:]' '_')"
      printf '*R9_Distribution_%s_%s*' "${filename}" "${method}"
      ;;
    R10_protocol_*)
      printf '*R10_Protocol_%s_%s*' "${group#R10_protocol_}" "${method}"
      ;;
    *)
      return 1
      ;;
  esac
}

bench_spu_prepare_node() {
  local ip="$1"
  local dir
  if [[ " ${BENCH_SPU_PREPARED_IPS} " == *" ${ip} "* ]]; then
    return 0
  fi

  dir="$(remote_system_dir spu)"
  bench_log "SPU/SCQL: clone/update, patch and build on ${ip}"
  bench_clone_or_update_system "${ip}" spu
  bench_rsync_to "${ip}" "${ROOT_DIR}/systems/spu/overlay/sorting_micro_test.cc" "${dir}/src/libspu/kernel/hlo/"
  bench_rsync_to "${ip}" "${ROOT_DIR}/systems/spu/overlay/BUILD.bazel" "${dir}/src/libspu/kernel/hlo/"
  bench_rsync_to "${ip}" "${ROOT_DIR}/systems/spu/overlay/permute.cc" "${dir}/src/libspu/kernel/hal/"
  bench_ssh "${ip}" "set -euo pipefail
    export PATH='${BENCH_REMOTE_WORK_DIR}/toolchains/bazel/bin':\"\$PATH\"
    mkdir -p '${BENCH_REMOTE_WORK_DIR}/build-logs'
    cd '${dir}/src'
    bazelisk build //libspu/kernel/hlo:sorting_micro_test > '${BENCH_REMOTE_WORK_DIR}/build-logs/spu_sorting_micro_build.log' 2>&1
  "
  BENCH_SPU_PREPARED_IPS="${BENCH_SPU_PREPARED_IPS} ${ip}"
}

bench_spu_prepare_nodes() {
  local ips=("$@")
  local ip pid rc=0
  local -a pids=()
  local -a launched=()

  for ip in "${ips[@]}"; do
    if [[ " ${BENCH_SPU_PREPARED_IPS} " == *" ${ip} "* ]]; then
      continue
    fi
    bench_spu_prepare_node "${ip}" &
    pids+=("$!")
    launched+=("${ip}")
  done
  for pid in "${pids[@]}"; do
    wait "${pid}" || rc=1
  done
  (( rc == 0 )) || return "${rc}"
  for ip in "${launched[@]}"; do
    if [[ " ${BENCH_SPU_PREPARED_IPS} " != *" ${ip} "* ]]; then
      BENCH_SPU_PREPARED_IPS="${BENCH_SPU_PREPARED_IPS} ${ip}"
    fi
  done
}

bench_spu_stage_case_all() {
  local input_file="$1" log_dir="$2"
  local parties="$3"
  local ip pid rc=0
  local -a pids=()
  local i

  for ((i = 0; i < parties; i++)); do
    ip="$(party_ip "${i}")"
    (
      bench_sync_case_data "${ip}" "${input_file}"
      bench_ssh "${ip}" "mkdir -p '${log_dir}'"
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
  : "${bits}"
  local filter parties method
  filter="$(bench_spu_gtest_filter "${group}" "${algorithm}" "${pow}" "${cols}" "${keys}" "${dtype}" "${dist}" "${input_file}")" || {
    bench_log "SPU/SCQL: skip unsupported case ${slug}"
    return 0
  }
  method="$(bench_spu_method_name "${algorithm}")"
  parties=3
  if [[ "${group}" == R10_protocol_* ]]; then
    case "${group#R10_protocol_}" in
      ABY3) parties=3 ;;
      SEMI2K|CHEETAH) parties=2 ;;
      *) parties=3 ;;
    esac
  fi

  local dir log_dir port parties_spec i ip
  dir="$(remote_system_dir spu)"
  log_dir="$(remote_case_log_dir "${system}" "${slug}" "${rep}")"
  port=$((SPU_PORT + rep * 10))
  parties_spec=""
  for ((i = 0; i < parties; i++)); do
    [[ -n "${parties_spec}" ]] && parties_spec+=","
    parties_spec+="$(party_ip "${i}"):$((port + i))"
  done

  local -a ips=()
  for ((i = 0; i < parties; i++)); do
    ips+=("$(party_ip "${i}")")
  done
  bench_spu_prepare_nodes "${ips[@]}"
  bench_spu_stage_case_all "${input_file}" "${log_dir}" "${parties}"

  bench_log "SPU/SCQL: launch ${parties}PC ${method} over ${parties_spec}"
  set +e
  local -a pids=()
  for ((i = parties - 1; i >= 0; i--)); do
    ip="$(party_ip "${i}")"
    bench_ssh "${ip}" "set -euo pipefail
      cd '${dir}/src'
      export FES_BENCH_RANK='${i}'
      export FES_BENCH_PARTIES='${parties_spec}'
      export FES_BENCH_DATA_DIR='${BENCH_REMOTE_DATA_DIR}'
      export FES_BENCH_POW='${pow}'
      export FES_BENCH_INPUT_FILE='$(remote_case_data_path "${input_file}")'
      export FES_BENCH_LINK_TIMEOUT_MS='$((TIMEOUT_S * 1000))'
      export FES_REVISION_BENCH='${FES_REVISION_BENCH:-0}'
      if [[ '${FES_REVISION_BENCH:-0}' == '1' ]]; then export FES_REQUIRE_SORT_METHOD=1; fi
      /usr/bin/time -v -o '${log_dir}/party${i}.time' timeout --kill-after=15s '${TIMEOUT_S}' ./bazel-bin/libspu/kernel/hlo/sorting_micro_test \
        --gtest_filter='${filter}' > '${log_dir}/party${i}.log' 2>&1
    " &
    pids[$i]=$!
    sleep 1
  done

  local rc=0 pid
  for pid in "${pids[@]}"; do
    wait "${pid}" || rc=1
  done
  set -e

  for ((i = 0; i < parties; i++)); do
    ip="$(party_ip "${i}")"
    bench_rsync_from "${ip}" "${log_dir}/" "${ROOT_DIR}/${BENCH_RAW_DIR}/${system}/${slug}/rep${rep}/p${i}/" || true
  done

  (( rc == 0 )) || {
    bench_log "SPU/SCQL: failed ${slug} repeat=${rep}"
    return 1
  }

  return 0
}
