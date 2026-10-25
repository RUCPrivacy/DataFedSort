#!/usr/bin/env bash

BENCH_CONGREGATION_PREPARED_IPS=""
CONGREGATION_JIFF_MPC_VERSION="${CONGREGATION_JIFF_MPC_VERSION:-1.0.0}"
CONGREGATION_JIFF_SERVER_PORT="${CONGREGATION_JIFF_SERVER_PORT:-9004}"

congregation_party_name() {
  case "$1" in
    1) printf '%s\n' "party_one" ;;
    2) printf '%s\n' "party_two" ;;
    3) printf '%s\n' "party_three" ;;
    *) bench_die "unsupported Congregation party: $1" ;;
  esac
}

bench_congregation_prepare_node() {
  local ip="$1"
  local dir target
  if [[ " ${BENCH_CONGREGATION_PREPARED_IPS} " == *" ${ip} "* ]]; then
    return 0
  fi

  dir="$(remote_system_dir congregation)"
  target="${dir}/demo/plaintext"
  bench_log "Congregation: clone/update and patch on ${ip}"
  bench_clone_or_update_system "${ip}" congregation
  bench_rsync_to "${ip}" "${ROOT_DIR}/systems/congregation/overlay/sorting_micro_test.py" "${target}/"
  bench_rsync_to "${ip}" "${ROOT_DIR}/systems/congregation/overlay/sort_operator_runner.js" "${target}/"
  bench_rsync_to "${ip}" "${ROOT_DIR}/systems/congregation/overlay/methods.tmpl" "${dir}/congregation/codegen/jiff/templates/modules/"
  bench_rsync_to "${ip}" "${ROOT_DIR}/systems/congregation/overlay/write_config.py" "${BENCH_REMOTE_WORK_DIR}/"
  set +e
  bench_ssh "${ip}" "set -euo pipefail
    export PATH='${BENCH_REMOTE_WORK_DIR}/toolchains/node/bin':\"\$PATH\"
    mkdir -p '${BENCH_REMOTE_WORK_DIR}/build-logs'
    cp '${dir}/congregation/codegen/jiff/templates/modules/helpers.tmpl' '${target}/helpers.js'
    cp '${dir}/congregation/codegen/jiff/templates/modules/methods.tmpl' '${target}/methods.js'
    node_needs_upgrade() {
      if ! command -v node >/dev/null 2>&1; then
        return 0
      fi
      local major
      major=\$(node --version 2>/dev/null | sed 's/^v//' | cut -d. -f1)
      [[ \"\${major:-0}\" -lt 16 ]]
    }
    if node_needs_upgrade; then
      echo 'Install the pinned revision Node toolchain before preparing Congregation.' >&2
      exit 1
    fi
    if node_needs_upgrade; then
      echo \"Congregation requires node >=16, current: \$(node --version 2>/dev/null || echo missing)\" >&2
      exit 1
    fi
    cd '${dir}'
    jiff_version_ok() {
      [[ -f '${target}/node_modules/jiff-mpc/package.json' ]] &&
        grep -q '\"version\"[[:space:]]*:[[:space:]]*\"${CONGREGATION_JIFF_MPC_VERSION}\"' '${target}/node_modules/jiff-mpc/package.json'
    }
    if [[ ! -f '${BENCH_REMOTE_WORK_DIR}/.congregation_setup_done' || '${BENCH_FORCE_REBUILD}' == '1' || ! -d '${target}/node_modules/jiff-mpc' || ! -d '${target}/node_modules/express' || ! -d '${target}/node_modules/jiff-mpc/node_modules/express' || ! jiff_version_ok ]]; then
      python3 -m pip install -r requirements.txt > '${BENCH_REMOTE_WORK_DIR}/build-logs/congregation_pip_requirements.log' 2>&1 || true
      python3 -m pip install -e . > '${BENCH_REMOTE_WORK_DIR}/build-logs/congregation_pip_editable.log' 2>&1
      rm -rf '${target}/node_modules' '${target}/package-lock.json'
      npm install --prefix '${target}' 'jiff-mpc@${CONGREGATION_JIFF_MPC_VERSION}' express@4.21.2 > '${BENCH_REMOTE_WORK_DIR}/build-logs/congregation_npm_jiff.log' 2>&1
      npm install --prefix '${target}/node_modules/jiff-mpc' express@4.21.2 > '${BENCH_REMOTE_WORK_DIR}/build-logs/congregation_npm_jiff_express.log' 2>&1
      touch '${BENCH_REMOTE_WORK_DIR}/.congregation_setup_done'
    fi
  "
  local prepare_status=$?
  set -e
  bench_rsync_from "${ip}" "${BENCH_REMOTE_WORK_DIR}/build-logs/" "${ROOT_DIR}/${BENCH_LOG_DIR}/congregation/build_${ip//./_}/" || true
  (( prepare_status == 0 )) || return "${prepare_status}"
  BENCH_CONGREGATION_PREPARED_IPS="${BENCH_CONGREGATION_PREPARED_IPS} ${ip}"
}

bench_congregation_prepare_nodes() {
  local ips=("$@")
  local ip pid rc=0
  local -a pids=()
  local -a launched=()

  for ip in "${ips[@]}"; do
    if [[ " ${BENCH_CONGREGATION_PREPARED_IPS} " == *" ${ip} "* ]]; then
      continue
    fi
    bench_congregation_prepare_node "${ip}" &
    pids+=("$!")
    launched+=("${ip}")
  done

  for pid in "${pids[@]}"; do
    wait "${pid}" || rc=1
  done
  (( rc == 0 )) || return "${rc}"
  for ip in "${launched[@]}"; do
    if [[ " ${BENCH_CONGREGATION_PREPARED_IPS} " != *" ${ip} "* ]]; then
      BENCH_CONGREGATION_PREPARED_IPS="${BENCH_CONGREGATION_PREPARED_IPS} ${ip}"
    fi
  done
}

bench_congregation_write_config() {
  local ip="$1" party="$2" dir target pname
  dir="$(remote_system_dir congregation)"
  target="${dir}/demo/plaintext"
  pname="$(congregation_party_name "${party}")"
  bench_ssh "${ip}" "python3 '${BENCH_REMOTE_WORK_DIR}/write_config.py' \
    '${target}/${pname}/config.json' \
    '${party}' \
    '$(party_ip 0)' '$(party_ip 1)' '$(party_ip 2)' \
    '${target}/node_modules/jiff-mpc' \
    '${CONGREGATION_JIFF_SERVER_PORT}'"
}

bench_congregation_stage_case_all() {
  local input_file="$1" log_dir="$2"
  local dir target ip idx pname pid rc=0
  local -a pids=()
  dir="$(remote_system_dir congregation)"
  target="${dir}/demo/plaintext"

  idx=1
  for ip in "$(party_ip 0)" "$(party_ip 1)" "$(party_ip 2)"; do
    (
      bench_sync_case_data "${ip}" "${input_file}"
      bench_congregation_write_config "${ip}" "${idx}"
      pname="$(congregation_party_name "${idx}")"
      bench_ssh "${ip}" "mkdir -p '${log_dir}' '${target}/${pname}/data' '${target}/${pname}/code'"
    ) &
    pids+=("$!")
    idx=$((idx + 1))
  done

  for pid in "${pids[@]}"; do
    wait "${pid}" || rc=1
  done
  (( rc == 0 )) || return "${rc}"
}

bench_congregation_cleanup_node() {
  local ip="$1" cleanup_b64
  cleanup_b64="$(base64 -w0 "${ROOT_DIR}/runtime/native/cleanup_owned.py")"
  bench_ssh "${ip}" "python3 -c \"import base64; exec(base64.b64decode('${cleanup_b64}'))\" '${BENCH_REMOTE_WORK_DIR}' congregation"
}

bench_congregation_cleanup_nodes() {
  local ip pid rc=0
  local -a pids=()
  for ip in "$@"; do
    bench_congregation_cleanup_node "${ip}" &
    pids+=("$!")
  done
  for pid in "${pids[@]}"; do
    wait "${pid}" || rc=1
  done
  return "${rc}"
}

bench_run_case() {
  local system="$1" group="$2" algorithm="$3" pow="$4" cols="$5" keys="$6" dtype="$7" dist="$8" bits="$9" input_file="${10}" slug="${11}" rep="${12}"
  : "${group}" "${bits}"
  case "${algorithm}" in
    odd_even|odd-even|batcher) ;;
    *)
      bench_log "Congregation: skip unsupported sorting algorithm '${algorithm}' for ${slug}"
      return 0
      ;;
  esac
  if [[ "${keys}" != "1" ]]; then
    bench_log "Congregation: skip ${slug}; current JIFF sort path supports one key column"
    return 0
  fi

  local p0 p1 p2 dir target input_remote log_dir data_type key_col
  p0="$(party_ip 0)"
  p1="$(party_ip 1)"
  p2="$(party_ip 2)"
  dir="$(remote_system_dir congregation)"
  target="${dir}/demo/plaintext"
  input_remote="$(remote_case_data_path "${input_file}")"
  log_dir="$(remote_case_log_dir "${system}" "${slug}" "${rep}")"
  data_type="int"
  key_col=0
  if [[ "${dtype}" != int* ]]; then
    bench_log "Congregation: skip ${slug}; only integer inputs are currently wired"
    return 0
  fi

  local idx ip pname
  bench_congregation_prepare_nodes "${p0}" "${p1}" "${p2}" || return 1
  bench_congregation_stage_case_all "${input_file}" "${log_dir}" || return 1

  bench_congregation_cleanup_nodes "${p0}" "${p1}" "${p2}" || true

  bench_log "Congregation: launch JIFF 3PC over ${p0},${p1},${p2}"
  set +e
  idx=1
  for ip in "${p0}" "${p1}" "${p2}"; do
    pname="$(congregation_party_name "${idx}")"
    bench_ssh "${ip}" "set -euo pipefail
      cd '${target}'
      export PATH='${BENCH_REMOTE_WORK_DIR}/toolchains/node/bin':\"\$PATH\"
      export PYTHONPATH='${dir}':\"\${PYTHONPATH:-}\"
      timeout --kill-after=15s '${TIMEOUT_S}' python3 sorting_micro_test.py \
        --no-rank-compress \
        --num-rows -1 \
        --data-type '${data_type}' \
        --party-id '${idx}' \
        --data-dir '${target}/${pname}/data' \
        --input '${input_remote}' \
        --key-col '${key_col}' \
        '${target}/${pname}/config.json' \
        '${target}/node_modules/jiff-mpc' > '${log_dir}/party${idx}.log' 2>&1
    " &
    eval "party_${idx}_pid=$!"
    sleep 1
    idx=$((idx + 1))
  done
  wait "${party_1_pid}"
  local party_1_status=$?
  wait "${party_2_pid}"
  local party_2_status=$?
  wait "${party_3_pid}"
  local party_3_status=$?
  set -e


  bench_congregation_cleanup_nodes "${p0}" "${p1}" "${p2}" || true

  bench_rsync_from "${p0}" "${log_dir}/" "${ROOT_DIR}/${BENCH_RAW_DIR}/${system}/${slug}/rep${rep}/p0/" || true
  bench_rsync_from "${p1}" "${log_dir}/" "${ROOT_DIR}/${BENCH_RAW_DIR}/${system}/${slug}/rep${rep}/p1/" || true
  bench_rsync_from "${p2}" "${log_dir}/" "${ROOT_DIR}/${BENCH_RAW_DIR}/${system}/${slug}/rep${rep}/p2/" || true

  if (( party_1_status != 0 || party_2_status != 0 || party_3_status != 0 )); then
    bench_log "Congregation: failed ${slug} repeat=${rep}; party_statuses=${party_1_status},${party_2_status},${party_3_status}"
    return 1
  fi

  return 0
}
