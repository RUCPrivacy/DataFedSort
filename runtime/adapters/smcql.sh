#!/usr/bin/env bash

BENCH_SMCQL_PREPARED_IPS=""
SMCQL_PORT="${SMCQL_PORT:-18145}"
SMCQL_BUILD_TIMEOUT_S="${SMCQL_BUILD_TIMEOUT_S:-900}"
SMCQL_TIMEOUT_S="${SMCQL_TIMEOUT_S:-${TIMEOUT_S:-2400}}"
# This adapter runs the BitonicSortLib microbenchmark across P0/P1 by default.
SMCQL_TWO_NODE="${SMCQL_TWO_NODE:-1}"

bench_smcql_prepare_node() {
  local ip="$1"
  local dir log_file
  if [[ " ${BENCH_SMCQL_PREPARED_IPS} " == *" ${ip} "* ]]; then
    return 0
  fi

  dir="$(remote_system_dir smcql)"
  log_file="${ROOT_DIR}/${BENCH_LOG_DIR}/smcql/prepare_${ip//./_}.log"
  mkdir -p "$(dirname "${log_file}")"
  bench_log "SMCQL: clone/update and patch on ${ip}"
  {
    echo "SMCQL prepare start: $(date '+%F %T')"
    echo "node=${ip}"
    bench_clone_or_update_system "${ip}" smcql
    bench_ssh "${ip}" "mkdir -p '${dir}/src/test/java/com/oblivm/backend/circuits' '${dir}/src/main/java/com/oblivm/backend/circuits'"
    bench_rsync_to "${ip}" "${ROOT_DIR}/systems/smcql/overlay/BitonicSortLibTest.java" "${dir}/src/test/java/com/oblivm/backend/circuits/"
    bench_rsync_to "${ip}" "${ROOT_DIR}/systems/smcql/overlay/BitonicSortLib.java" "${dir}/src/main/java/com/oblivm/backend/circuits/"
    bench_ssh "${ip}" "mkdir -p '${BENCH_REMOTE_WORK_DIR}/build-logs'"
    bench_ssh "${ip}" "set -euo pipefail
      cd '${dir}'
      test -f src/test/java/com/oblivm/backend/circuits/BitonicSortLibTest.java
      test -f src/main/java/com/oblivm/backend/circuits/BitonicSortLib.java
      if [[ '${BENCH_FORCE_REBUILD}' == '1' ]]; then
        timeout '${SMCQL_BUILD_TIMEOUT_S}' mvn -q -DskipTests -Dstyle.color=never clean test-compile dependency:build-classpath -Dmdep.outputFile=target/fes-bench-classpath.txt
      else
        timeout '${SMCQL_BUILD_TIMEOUT_S}' mvn -q -DskipTests -Dstyle.color=never test-compile dependency:build-classpath -Dmdep.outputFile=target/fes-bench-classpath.txt
      fi
      test -f target/test-classes/com/oblivm/backend/circuits/BitonicSortLibTest.class
      test -f target/classes/com/oblivm/backend/circuits/BitonicSortLib.class
      test -s target/fes-bench-classpath.txt
      ls -l src/test/java/com/oblivm/backend/circuits/BitonicSortLibTest.java target/test-classes/com/oblivm/backend/circuits/BitonicSortLibTest.class target/fes-bench-classpath.txt
    "
    echo "SMCQL prepare done: $(date '+%F %T')"
  } > "${log_file}" 2>&1
  BENCH_SMCQL_PREPARED_IPS="${BENCH_SMCQL_PREPARED_IPS} ${ip}"
}

bench_smcql_prepare_nodes() {
  local ips=("$@")
  local ip pid rc=0
  local -a pids=()
  local -a launched=()

  for ip in "${ips[@]}"; do
    if [[ " ${BENCH_SMCQL_PREPARED_IPS} " == *" ${ip} "* ]]; then
      continue
    fi
    bench_smcql_prepare_node "${ip}" &
    pids+=("$!")
    launched+=("${ip}")
  done
  for pid in "${pids[@]}"; do
    wait "${pid}" || rc=1
  done
  (( rc == 0 )) || return "${rc}"
  for ip in "${launched[@]}"; do
    if [[ " ${BENCH_SMCQL_PREPARED_IPS} " != *" ${ip} "* ]]; then
      BENCH_SMCQL_PREPARED_IPS="${BENCH_SMCQL_PREPARED_IPS} ${ip}"
    fi
  done
}

bench_smcql_stage_case_all() {
  local input_file="$1" log_dir="$2"
  local ip pid rc=0
  local -a pids=()
  for ip in "$(party_ip 0)" "$(party_ip 1)"; do
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
  : "${group}" "${pow}" "${cols}" "${dist}"
  case "${algorithm}" in
    bitonic) ;;
    *)
      bench_log "SMCQL: skip unsupported sorting algorithm '${algorithm}' for ${slug}"
      return 0
      ;;
  esac

  local p0 p1 dir input_remote log_dir port
  p0="$(party_ip 0)"
  p1="$(party_ip 1)"
  dir="$(remote_system_dir smcql)"
  input_remote="$(remote_case_data_path "${input_file}")"
  log_dir="$(remote_case_log_dir "${system}" "${slug}" "${rep}")"
  port=$((SMCQL_PORT + rep))

  bench_smcql_prepare_nodes "${p0}" "${p1}"
  bench_smcql_stage_case_all "${input_file}" "${log_dir}"


  bench_log "SMCQL: launch 2PC over ${p0},${p1}, port=${port}"
  set +e
  bench_ssh "${p0}" "set +e
    cd '${dir}'
    : > '${log_dir}/party0.log'
    cp_path=\"target/test-classes:target/classes:\$(cat target/fes-bench-classpath.txt)\"
    /usr/bin/time -v -o '${log_dir}/party0.time' timeout --kill-after=15s '${SMCQL_TIMEOUT_S}' java -cp \"\$cp_path\" \
      -Dsmcql.profileRounds=true \
      -Dsmcql.role=server \
      -Dport='${port}' \
      -Dinput='${input_remote}' \
      -DnumKeys='${keys}' \
      -DcolBits='${bits}' \
      -DdataType='${dtype}' \
      com.oblivm.backend.circuits.BitonicSortLibTest >> '${log_dir}/party0.log' 2>&1
    status=\$?
    if [[ -d target/surefire-reports ]]; then
      mkdir -p '${log_dir}/surefire-reports'
      cp -f target/surefire-reports/* '${log_dir}/surefire-reports/' 2>/dev/null || true
    fi
    exit \${status}
  " &
  local p0_pid=$!
  sleep 2
  bench_ssh "${p1}" "set +e
    cd '${dir}'
    : > '${log_dir}/party1.log'
    cp_path=\"target/test-classes:target/classes:\$(cat target/fes-bench-classpath.txt)\"
    /usr/bin/time -v -o '${log_dir}/party1.time' timeout --kill-after=15s '${SMCQL_TIMEOUT_S}' java -cp \"\$cp_path\" \
      -Dsmcql.profileRounds=true \
      -Dsmcql.role=client \
      -Dsmcql.host='${p0}' \
      -Dport='${port}' \
      -Dinput='${input_remote}' \
      -DnumKeys='${keys}' \
      -DcolBits='${bits}' \
      -DdataType='${dtype}' \
      com.oblivm.backend.circuits.BitonicSortLibTest >> '${log_dir}/party1.log' 2>&1
    status=\$?
    if [[ -d target/surefire-reports ]]; then
      mkdir -p '${log_dir}/surefire-reports'
      cp -f target/surefire-reports/* '${log_dir}/surefire-reports/' 2>/dev/null || true
    fi
    exit \${status}
  "
  local p1_status=$?
  wait "${p0_pid}"
  local p0_status=$?
  set -e

  bench_rsync_from "${p0}" "${log_dir}/" "${ROOT_DIR}/${BENCH_RAW_DIR}/${system}/${slug}/rep${rep}/p0/" || true
  bench_rsync_from "${p1}" "${log_dir}/" "${ROOT_DIR}/${BENCH_RAW_DIR}/${system}/${slug}/rep${rep}/p1/" || true

  if (( p0_status != 0 || p1_status != 0 )); then
    bench_log "SMCQL: failed ${slug} repeat=${rep}; party_statuses=${p0_status},${p1_status}"
    return 1
  fi

  return 0
}
