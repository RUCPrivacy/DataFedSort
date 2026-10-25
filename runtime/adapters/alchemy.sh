#!/usr/bin/env bash

BENCH_ALCHEMY_PREPARED_IPS=""

bench_alchemy_prepare_node() {
  local ip="$1"
  local dir emp_tool_ref emp_ot_ref emp_sh2pc_ref emp_zk_ref ref
  if [[ " ${BENCH_ALCHEMY_PREPARED_IPS} " == *" ${ip} "* ]]; then
    return 0
  fi

  dir="$(remote_system_dir alchemy)"
  read -r emp_tool_ref emp_ot_ref emp_sh2pc_ref emp_zk_ref < <(python3 - "${ROOT_DIR}/dependencies/lock.json" <<'PY'
import json,sys
dependencies=json.load(open(sys.argv[1]))['dependencies']
print(*(dependencies[name]['revision'] for name in ('emp-tool','emp-ot','emp-sh2pc','emp-zk')))
PY
  )
  for ref in "${emp_tool_ref}" "${emp_ot_ref}" "${emp_sh2pc_ref}" "${emp_zk_ref}"; do
    [[ "${ref}" =~ ^[0-9a-f]{40}$ ]] || bench_die 'Alchemy EMP dependencies require exact source revisions'
  done
  bench_log "alchemy: clone/update and patch on ${ip}"
  bench_clone_or_update_system "${ip}" alchemy
  bench_rsync_to "${ip}" "${ROOT_DIR}/systems/alchemy/overlay/setup.sh" "${dir}/"
  bench_rsync_to "${ip}" "${ROOT_DIR}/systems/alchemy/overlay/emp_install.sh" "${dir}/deps/emp/install.sh"
  bench_rsync_to "${ip}" "${ROOT_DIR}/systems/alchemy/overlay/CMakeLists.txt" "${dir}/src/main/cpp/test/"
  bench_rsync_to "${ip}" "${ROOT_DIR}/systems/alchemy/overlay/sorting_micro_test.cpp" "${dir}/src/main/cpp/test/mpc/"
  bench_rsync_to "${ip}" "${ROOT_DIR}/systems/alchemy/overlay/sort.cpp" "${dir}/src/main/cpp/operators/"
  bench_ssh "${ip}" "set -euo pipefail
    mkdir -p '${BENCH_REMOTE_WORK_DIR}/build-logs'
    if [[ '${BENCH_FORCE_REBUILD}' == '1' ]]; then
      rm -f '${dir}/src/main/cpp/bin/sorting_micro_test'
      rm -f '${dir}/src/main/cpp/CMakeCache.txt'
      rm -rf '${dir}/src/main/cpp/CMakeFiles'
      rm -f '${BENCH_REMOTE_WORK_DIR}/.alchemy_setup_done'
    fi
    if [[ ! -f '${BENCH_REMOTE_WORK_DIR}/.alchemy_setup_done' ]] || \
       [[ ! -f /usr/local/include/emp-tool/emp-tool.h ]] || \
       [[ ! -f /usr/local/share/datafedsort/alchemy_emp_pinned.env ]] || \
       ! grep -qx 'EMP_TOOL_REF=${emp_tool_ref}' /usr/local/share/datafedsort/alchemy_emp_pinned.env || \
       ! grep -qx 'EMP_OT_REF=${emp_ot_ref}' /usr/local/share/datafedsort/alchemy_emp_pinned.env || \
       ! grep -qx 'EMP_SH2PC_REF=${emp_sh2pc_ref}' /usr/local/share/datafedsort/alchemy_emp_pinned.env || \
       ! grep -qx 'EMP_ZK_REF=${emp_zk_ref}' /usr/local/share/datafedsort/alchemy_emp_pinned.env || \
       ! ldconfig -p 2>/dev/null | grep 'libpqxx' >/dev/null; then
      cd '${dir}'
      chmod +x ./setup.sh
      export ALCHEMY_EMP_TOOL_REF='${emp_tool_ref}'
      export ALCHEMY_EMP_OT_REF='${emp_ot_ref}'
      export ALCHEMY_EMP_SH2PC_REF='${emp_sh2pc_ref}'
      export ALCHEMY_EMP_ZK_REF='${emp_zk_ref}'
      ./setup.sh > '${BENCH_REMOTE_WORK_DIR}/build-logs/alchemy_setup.log' 2>&1
      touch '${BENCH_REMOTE_WORK_DIR}/.alchemy_setup_done'
    fi
    cd '${dir}/src/main/cpp'
    cmake . > '${BENCH_REMOTE_WORK_DIR}/build-logs/alchemy_cmake.log' 2>&1
    make -j\"\$(nproc)\" vaultdb-emp sorting_micro_test > '${BENCH_REMOTE_WORK_DIR}/build-logs/alchemy_make.log' 2>&1
  "
  BENCH_ALCHEMY_PREPARED_IPS="${BENCH_ALCHEMY_PREPARED_IPS} ${ip}"
}

bench_alchemy_prepare_nodes() {
  local ips=("$@")
  local ip pid rc=0
  local -a pids=()
  local -a launched=()

  for ip in "${ips[@]}"; do
    if [[ " ${BENCH_ALCHEMY_PREPARED_IPS} " == *" ${ip} "* ]]; then
      continue
    fi
    bench_alchemy_prepare_node "${ip}" &
    pids+=("$!")
    launched+=("${ip}")
  done

  for pid in "${pids[@]}"; do
    wait "${pid}" || rc=1
  done
  (( rc == 0 )) || return "${rc}"
  for ip in "${launched[@]}"; do
    if [[ " ${BENCH_ALCHEMY_PREPARED_IPS} " != *" ${ip} "* ]]; then
      BENCH_ALCHEMY_PREPARED_IPS="${BENCH_ALCHEMY_PREPARED_IPS} ${ip}"
    fi
  done
}

bench_alchemy_sync_case_data_all() {
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
  case "${algorithm}" in
    batcher|bitonic|odd_even|odd-even) ;;
    *)
      bench_log "alchemy: skip unsupported sorting algorithm '${algorithm}' for ${slug}"
      return 0
      ;;
  esac

  local p0 p1 dir input_remote log0 log1 port alchemy_dtype
  p0="$(party_ip 0)"
  p1="$(party_ip 1)"
  dir="$(remote_system_dir alchemy)"
  input_remote="$(remote_case_data_path "${input_file}")"
  log0="$(remote_case_log_dir "${system}" "${slug}" "${rep}")/alice.log"
  log1="$(remote_case_log_dir "${system}" "${slug}" "${rep}")/bob.log"
  port=$((ALCHEMY_PORT + rep))
  alchemy_dtype="${dtype}"
  if [[ "${dtype}" == "int8" ]]; then
    alchemy_dtype="int32"
  fi

  bench_alchemy_prepare_nodes "${p0}" "${p1}"
  bench_alchemy_sync_case_data_all "${input_file}"

  bench_log "alchemy: launch Alice on ${p0}, Bob on ${p1}, port=${port}"
  set +e
  bench_ssh "${p0}" "mkdir -p '$(remote_case_log_dir "${system}" "${slug}" "${rep}")'; cd '${dir}/src/main/cpp'; export LD_LIBRARY_PATH=\"\$PWD:\${LD_LIBRARY_PATH:-}\"; /usr/bin/time -v -o '${log0}.time' timeout --kill-after=15s '${TIMEOUT_S}' ./bin/sorting_micro_test --party=1 --port='${port}' --input='${input_remote}' --sort_cols='${keys}' --data_type='${alchemy_dtype}' --validation='${FES_REVISION_BENCH:-0}' > '${log0}' 2>&1" &
  local alice_ssh_pid=$!
  sleep 2
  bench_ssh "${p1}" "mkdir -p '$(remote_case_log_dir "${system}" "${slug}" "${rep}")'; cd '${dir}/src/main/cpp'; export LD_LIBRARY_PATH=\"\$PWD:\${LD_LIBRARY_PATH:-}\"; /usr/bin/time -v -o '${log1}.time' timeout --kill-after=15s '${TIMEOUT_S}' ./bin/sorting_micro_test --party=2 --port='${port}' --alice_host='${p0}' --input='${input_remote}' --sort_cols='${keys}' --data_type='${alchemy_dtype}' --validation='${FES_REVISION_BENCH:-0}' > '${log1}' 2>&1"
  local bob_status=$?
  wait "${alice_ssh_pid}"
  local alice_status=$?
  set -e

  bench_rsync_from "${p0}" "$(remote_case_log_dir "${system}" "${slug}" "${rep}")/" "${ROOT_DIR}/${BENCH_RAW_DIR}/${system}/${slug}/rep${rep}/p0/" || true
  bench_rsync_from "${p1}" "$(remote_case_log_dir "${system}" "${slug}" "${rep}")/" "${ROOT_DIR}/${BENCH_RAW_DIR}/${system}/${slug}/rep${rep}/p1/" || true
  if (( alice_status != 0 || bob_status != 0 )); then
    bench_log "alchemy: failed ${slug} repeat=${rep}; alice_status=${alice_status}, bob_status=${bob_status}"
    return 1
  fi
  return 0
}
