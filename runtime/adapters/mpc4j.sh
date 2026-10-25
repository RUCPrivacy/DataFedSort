#!/usr/bin/env bash

BENCH_MPC4J_PREPARED_IPS=""
MPC4J_PORT="${MPC4J_PORT:-20140}"
MPC4J_BUILD_TIMEOUT_S="${MPC4J_BUILD_TIMEOUT_S:-3600}"
MPC4J_TIMEOUT_S="${MPC4J_TIMEOUT_S:-${TIMEOUT_S:-2400}}"
MPC4J_JAVA_MODULES="${MPC4J_JAVA_MODULES:-jdk.incubator.vector}"

bench_mpc4j_algorithm_id() {
  case "$1" in
    bitonic) printf '%s\n' "1" ;;
    quick) printf '%s\n' "2" ;;
    radix) printf '%s\n' "3" ;;
    *) return 1 ;;
  esac
}

bench_mpc4j_prepare_node() {
  local ip="$1"
  local dir
  if [[ " ${BENCH_MPC4J_PREPARED_IPS} " == *" ${ip} "* ]]; then
    return 0
  fi

  dir="$(remote_system_dir mpc4j)"
  bench_log "MPC4J/Scape: clone/update, patch and install on ${ip}"
  bench_clone_or_update_system "${ip}" mpc4j
  bench_rsync_to "${ip}" "${ROOT_DIR}/systems/mpc4j/overlay/PgSortLanParty.java" "${dir}/mpc4j-work-scape/scape-s3pc-opf/src/test/java/edu/alibaba/mpc4j/work/scape/s3pc/opf/pgsort/"
  bench_rsync_to "${ip}" "${ROOT_DIR}/systems/mpc4j/overlay/BitonicPgSortParty.java" "${dir}/mpc4j-work-scape/scape-s3pc-opf/src/main/java/edu/alibaba/mpc4j/work/scape/s3pc/opf/pgsort/bitonic/"
  bench_rsync_to "${ip}" "${ROOT_DIR}/systems/mpc4j/overlay/PermutableBitonicSorter.java" "${dir}/mpc4j-common-circuit/src/main/java/edu/alibaba/mpc4j/common/circuit/z2/psorter/bitonic/"
  bench_rsync_to "${ip}" "${ROOT_DIR}/systems/mpc4j/overlay/QuickPgSortParty.java" "${dir}/mpc4j-work-scape/scape-s3pc-opf/src/main/java/edu/alibaba/mpc4j/work/scape/s3pc/opf/pgsort/quick/"
  bench_rsync_to "${ip}" "${ROOT_DIR}/systems/mpc4j/overlay/RadixPgSortParty.java" "${dir}/mpc4j-work-scape/scape-s3pc-opf/src/main/java/edu/alibaba/mpc4j/work/scape/s3pc/opf/pgsort/radix/"
  bench_ssh "${ip}" "set -euo pipefail
    mkdir -p '${BENCH_REMOTE_WORK_DIR}/build-logs'
    cd '${dir}'
    native_dir='${dir}/mpc4j-native-tool/cmake-build-release'
    if [[ -z \"\${JAVA_HOME:-}\" ]]; then
      javac_path=\"\$(readlink -f \"\$(command -v javac)\")\"
      export JAVA_HOME=\"\$(dirname \"\$(dirname \"\${javac_path}\")\")\"
    fi
    echo \"JAVA_HOME=\${JAVA_HOME}\" > '${BENCH_REMOTE_WORK_DIR}/build-logs/mpc4j_native_env.log'
    fourq_dir='${dir}/mpc4j-native-fourq/cmake-build-release'
    if [[ '${BENCH_FORCE_REBUILD}' == '1' || ! -f /usr/local/lib/libfourq.a ]]; then
      rm -rf \"\${fourq_dir}\"
      mkdir -p \"\${fourq_dir}\"
      cd \"\${fourq_dir}\"
      cmake .. -DCMAKE_BUILD_TYPE=Release > '${BENCH_REMOTE_WORK_DIR}/build-logs/mpc4j_fourq_cmake.log' 2>&1
      make -j\"\$(nproc)\" fourq > '${BENCH_REMOTE_WORK_DIR}/build-logs/mpc4j_fourq_make.log' 2>&1
      make install >> '${BENCH_REMOTE_WORK_DIR}/build-logs/mpc4j_fourq_make.log' 2>&1
      ldconfig || true
    fi
    if [[ ! -f /usr/local/lib/libfourq.a && ! -f /usr/local/lib/libfourq.so ]]; then
      echo 'mpc4j-native-fourq missing after build; expected /usr/local/lib/libfourq.*' >&2
      find /usr/local/lib '${dir}/mpc4j-native-fourq' -maxdepth 3 -type f -name 'libfourq*' >&2 || true
      exit 1
    fi
    if [[ '${BENCH_FORCE_REBUILD}' == '1' || ! -f \"\${native_dir}/libmpc4j-native-tool.so\" ]]; then
      rm -rf \"\${native_dir}\"
      mkdir -p \"\${native_dir}\"
      cd \"\${native_dir}\"
      cmake .. -DCMAKE_BUILD_TYPE=Release > '${BENCH_REMOTE_WORK_DIR}/build-logs/mpc4j_native_cmake.log' 2>&1
      make -j\"\$(nproc)\" mpc4j-native-tool > '${BENCH_REMOTE_WORK_DIR}/build-logs/mpc4j_native_make.log' 2>&1
    fi
    if [[ ! -f \"\${native_dir}/libmpc4j-native-tool.so\" ]]; then
      echo 'mpc4j native library missing after build; expected libmpc4j-native-tool.so' >&2
      find '${dir}/mpc4j-native-tool' -maxdepth 3 -type f -name 'libmpc4j-native-tool*' >&2 || true
      exit 1
    fi
    export MAVEN_OPTS=\"--add-modules=${MPC4J_JAVA_MODULES} \${MAVEN_OPTS:-}\"
    cd '${dir}'
    timeout --kill-after=30s '${MPC4J_BUILD_TIMEOUT_S}' mvn -pl mpc4j-work-scape/scape-s3pc-opf -am \
      -DskipTests \
      -Dmaven.test.skip=false \
      -DfailIfNoTests=false \
      -Dmaven.compiler.source=21 \
      -Dmaven.compiler.target=21 \
      install > '${BENCH_REMOTE_WORK_DIR}/build-logs/mpc4j_install.log' 2>&1
  "
  BENCH_MPC4J_PREPARED_IPS="${BENCH_MPC4J_PREPARED_IPS} ${ip}"
}

bench_mpc4j_prepare_nodes() {
  local ips=("$@")
  local ip pid rc=0
  local -a pids=()
  local -a launched=()

  for ip in "${ips[@]}"; do
    if [[ " ${BENCH_MPC4J_PREPARED_IPS} " == *" ${ip} "* ]]; then
      continue
    fi
    bench_mpc4j_prepare_node "${ip}" &
    pids+=("$!")
    launched+=("${ip}")
  done
  for pid in "${pids[@]}"; do
    wait "${pid}" || rc=1
  done
  (( rc == 0 )) || return "${rc}"
  for ip in "${launched[@]}"; do
    if [[ " ${BENCH_MPC4J_PREPARED_IPS} " != *" ${ip} "* ]]; then
      BENCH_MPC4J_PREPARED_IPS="${BENCH_MPC4J_PREPARED_IPS} ${ip}"
    fi
  done
}

bench_mpc4j_collect_build_logs() {
  local ip
  local i=0
  for ip in "$(party_ip 0)" "$(party_ip 1)" "$(party_ip 2)"; do
    bench_rsync_from "${ip}" "${BENCH_REMOTE_WORK_DIR}/build-logs/" "${ROOT_DIR}/${BENCH_LOG_DIR}/mpc4j/build/p${i}/" || true
    i=$((i + 1))
  done
}

bench_mpc4j_stage_case_all() {
  local input_file="$1" log_dir="$2"
  local ip pid rc=0
  local -a pids=()
  for ip in "$(party_ip 0)" "$(party_ip 1)" "$(party_ip 2)"; do
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

bench_mpc4j_cleanup_node() {
  local ip="$1" cleanup_b64
  cleanup_b64="$(base64 -w0 "${ROOT_DIR}/runtime/native/cleanup_owned.py")"
  bench_ssh "${ip}" "python3 -c \"import base64; exec(base64.b64decode('${cleanup_b64}'))\" '${BENCH_REMOTE_WORK_DIR}' mpc4j"
}

bench_run_case() {
  local system="$1" group="$2" algorithm="$3" pow="$4" cols="$5" keys="$6" dtype="$7" dist="$8" bits="$9" input_file="${10}" slug="${11}" rep="${12}"
  : "${group}" "${pow}" "${cols}" "${dtype}" "${dist}"
  local alg_id
  alg_id="$(bench_mpc4j_algorithm_id "${algorithm}")" || {
    bench_log "MPC4J/Scape: skip unsupported sorting algorithm '${algorithm}' for ${slug}"
    return 0
  }
  local security="semi-honest"
  case "${group}" in
    R10_protocol_malicious) security="malicious" ;;
    R10_protocol_semi_honest|R10_protocol) security="semi-honest" ;;
  esac

  local p0 p1 p2 dir input_remote log_dir port parties_spec task_id
  p0="$(party_ip 0)"
  p1="$(party_ip 1)"
  p2="$(party_ip 2)"
  dir="$(remote_system_dir mpc4j)"
  input_remote="$(remote_case_data_path "${input_file}")"
  log_dir="$(remote_case_log_dir "${system}" "${slug}" "${rep}")"
  port=$((MPC4J_PORT + rep * 10))
  parties_spec="${p0}:${port},${p1}:$((port + 1)),${p2}:$((port + 2))"
  task_id=$((70000 + rep))

  if ! bench_mpc4j_prepare_nodes "${p0}" "${p1}" "${p2}"; then
    bench_mpc4j_collect_build_logs
    return 1
  fi
  bench_mpc4j_collect_build_logs
  bench_mpc4j_stage_case_all "${input_file}" "${log_dir}"

  for ip in "${p0}" "${p1}" "${p2}"; do
    bench_mpc4j_cleanup_node "${ip}"
  done

  bench_log "MPC4J/Scape: launch 3PC ${algorithm}/${security} over ${parties_spec}"
  set +e
  local i ip
  local -a pids=()
  for i in 2 1 0; do
    ip="$(party_ip "${i}")"
    bench_ssh "${ip}" "set -euo pipefail
      cd '${dir}'
      export LD_LIBRARY_PATH='${dir}/mpc4j-native-tool/cmake-build-release':\"\${LD_LIBRARY_PATH:-}\"
      export MAVEN_OPTS=\"--add-modules=${MPC4J_JAVA_MODULES} \${MAVEN_OPTS:-}\"
      /usr/bin/time -v -o '${log_dir}/party${i}.time' timeout --kill-after=30s '${MPC4J_TIMEOUT_S}' mvn -pl mpc4j-work-scape/scape-s3pc-opf \
        -DskipTests \
        -Dmaven.test.skip=false \
        -DfailIfNoTests=false \
        -Dmaven.compiler.source=21 \
        -Dmaven.compiler.target=21 \
        -Dexec.classpathScope=test \
        -Dexec.mainClass=edu.alibaba.mpc4j.work.scape.s3pc.opf.pgsort.PgSortLanParty \
        -Djava.library.path='${dir}/mpc4j-native-tool/cmake-build-release' \
        -Dpgsort.input='${input_remote}' \
        -Dpgsort.algorithm='${alg_id}' \
        -Dpgsort.security='${security}' \
        -Dpgsort.keyNum='${keys}' \
        -Dpgsort.bitLen='${bits}' \
        -Dpgsort.parallel=true \
        -Dpgsort.stable=false \
        -Dpgsort.baseUseMac=false \
        -Dmpc4j.lan.rank='${i}' \
        -Dmpc4j.lan.parties='${parties_spec}' \
        -Dmpc4j.lan.taskId='${task_id}' \
        exec:java > '${log_dir}/party${i}.log' 2>&1
    " &
    pids[$i]=$!
    sleep 1
  done

  local rc=0 pid
  for pid in "${pids[@]}"; do
    wait "${pid}" || rc=1
  done
  set -e

  if (( rc != 0 )); then
    for i in 0 1 2; do
      ip="$(party_ip "${i}")"
      mkdir -p "${ROOT_DIR}/${BENCH_RAW_DIR}/${system}/${slug}/rep${rep}/p${i}"
      {
        echo "[process]"
        bench_ssh "${ip}" "ps -eo pid,etime,pcpu,pmem,args | grep -E 'PgSortLanParty|exec-maven-plugin|mvn|java' | grep -v grep || true" || true
        echo "[listen]"
        bench_ssh "${ip}" "ss -lntp | grep -E '2014|2015|2016|java' || true" || true
      } > "${ROOT_DIR}/${BENCH_RAW_DIR}/${system}/${slug}/rep${rep}/p${i}/diagnostics.txt"
      bench_mpc4j_cleanup_node "${ip}"
    done
  fi

  for i in 0 1 2; do
    ip="$(party_ip "${i}")"
    bench_rsync_from "${ip}" "${log_dir}/" "${ROOT_DIR}/${BENCH_RAW_DIR}/${system}/${slug}/rep${rep}/p${i}/" || true
  done

  (( rc == 0 )) || {
    bench_log "MPC4J/Scape: failed ${slug} repeat=${rep}"
    return 1
  }

  local local_rep_dir aggregate_log
  local_rep_dir="${ROOT_DIR}/${BENCH_RAW_DIR}/${system}/${slug}/rep${rep}"
  aggregate_log="${local_rep_dir}/aggregate.log"
  python3 "${ROOT_DIR}/runtime/native/scape_collect.py" \
    --folder "${local_rep_dir}" --security "${security}" \
    --system "${system}" --case "${slug}" > "${aggregate_log}"
  return 0
}
