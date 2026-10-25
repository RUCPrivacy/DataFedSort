#!/usr/bin/env bash

BENCH_SMPCACHE_PREPARED_IPS=""
SMPCACHE_CONDA_ENV="${SMPCACHE_CONDA_ENV:-fes-smpcache}"
SMPCACHE_PORT_BASE="${SMPCACHE_PORT_BASE:-25900}"
SMPCACHE_PREPARE_TIMEOUT_S="${SMPCACHE_PREPARE_TIMEOUT_S:-300}"
SMPCACHE_SETUP_TIMEOUT_S="${SMPCACHE_SETUP_TIMEOUT_S:-3600}"
SMPCACHE_TIMEOUT_S="${SMPCACHE_TIMEOUT_S:-${TIMEOUT_S:-2400}}"
SMPCACHE_PYTHON_VERSION="${SMPCACHE_PYTHON_VERSION:-3.7}"
SMPCACHE_TENSORFLOW_VERSION="${SMPCACHE_TENSORFLOW_VERSION:-1.14.0}"
SMPCACHE_ROSETTA_GIT_URL="${SMPCACHE_ROSETTA_GIT_URL:-https://github.com/LatticeX-Foundation/Rosetta.git}"

bench_smpcache_party_name() {
  printf 'P%s\n' "$1"
}

bench_smpcache_write_config() {
  local ip="$1" dir="$2"
  bench_ssh "${ip}" "cat > '${dir}/CONFIG.json' <<'EOF'
{
  \"NODE_INFO\": [
    {\"NAME\": \"PartyA(P0)\", \"HOST\": \"$(party_ip 0)\", \"PORT\": ${SMPCACHE_PORT_BASE}, \"NODE_ID\": \"P0\"},
    {\"NAME\": \"PartyB(P1)\", \"HOST\": \"$(party_ip 1)\", \"PORT\": $((SMPCACHE_PORT_BASE + 1)), \"NODE_ID\": \"P1\"},
    {\"NAME\": \"PartyC(P2)\", \"HOST\": \"$(party_ip 2)\", \"PORT\": $((SMPCACHE_PORT_BASE + 2)), \"NODE_ID\": \"P2\"}
  ],
  \"DATA_NODES\": [\"P0\", \"P1\", \"P2\"],
  \"COMPUTATION_NODES\": {\"P0\": 0, \"P1\": 1, \"P2\": 2},
  \"RESULT_NODES\": [\"P0\", \"P1\", \"P2\"]
}
EOF"
}

bench_smpcache_remote_env_setup_script() {
  local remote_log_dir="$1"
  cat <<EOF
      mkdir -p '${BENCH_REMOTE_WORK_DIR}/build-logs' '${remote_log_dir}'
      remote_log_dir='${remote_log_dir}'
      conda_sh=''
      for candidate in '${BENCH_REMOTE_WORK_DIR}/toolchains/conda/etc/profile.d/conda.sh' "\$HOME/miniconda3/etc/profile.d/conda.sh" "\$HOME/anaconda3/etc/profile.d/conda.sh" '/opt/conda/etc/profile.d/conda.sh'; do
        if [[ -f "\$candidate" ]]; then
          conda_sh="\$candidate"
          break
        fi
      done
      if [[ -z "\$conda_sh" ]]; then
        echo 'SMPCache requires conda, but conda.sh was not found.' >&2
        exit 1
      fi
      source "\$conda_sh"
      check_smpcache_env() {
        python - <<'PY'
import tensorflow
import latticex.rosetta
import psutil
print('SMPCache Python environment OK')
PY
      }
      if ! conda env list | awk '{print \$1}' | grep -qx '${SMPCACHE_CONDA_ENV}'; then
        echo 'SMPCache conda env not found on this node; creating ${SMPCACHE_CONDA_ENV} with Python ${SMPCACHE_PYTHON_VERSION}'
        conda create -y -n '${SMPCACHE_CONDA_ENV}' python='${SMPCACHE_PYTHON_VERSION}' \
          > "\$remote_log_dir/SMPCache_conda_create.log" 2>&1
      fi
      conda activate '${SMPCACHE_CONDA_ENV}'
      if ! check_smpcache_env; then
        echo 'SMPCache Python imports failed; installing TensorFlow/Rosetta dependencies'
        python -m pip install -U 'pip<24' 'setuptools<60' wheel \
          > "\$remote_log_dir/SMPCache_pip_bootstrap.log" 2>&1 || true
        python -m pip install \
          'tensorflow==${SMPCACHE_TENSORFLOW_VERSION}' \
          'protobuf==3.20.3' \
          'numpy==1.18.5' 'psutil==5.9.8' 'pybind11==2.11.1' \
          > "\$remote_log_dir/SMPCache_pip_runtime.log" 2>&1
        # Build the pinned native source instead of choosing an unrecorded wheel.
      fi
      if ! check_smpcache_env; then
        echo 'latticex.rosetta is still unavailable; building Rosetta from source'
        rosetta_dir='${BENCH_REMOTE_SRC_DIR}/Rosetta'
        test -d "\$rosetta_dir/.git"
        (
          cd "\$rosetta_dir"
          git submodule update --init --recursive
          export CMAKE_POLICY_VERSION_MINIMUM=3.5
          timeout '${SMPCACHE_SETUP_TIMEOUT_S}' ./rosetta.sh compile --enable-protocol-mpc-securenn
          export SKLEARN_ALLOW_DEPRECATED_SKLEARN_PACKAGE_INSTALL=True
          timeout '${SMPCACHE_SETUP_TIMEOUT_S}' ./rosetta.sh install
        ) > "\$remote_log_dir/SMPCache_rosetta_build.log" 2>&1
      fi
      timeout '${SMPCACHE_PREPARE_TIMEOUT_S}' python - <<'PY'
import tensorflow
import latticex.rosetta
import psutil
print('SMPCache Python environment OK')
PY
EOF
}

bench_smpcache_prepare_node() {
  local ip="$1"
  local dir log_file prepare_rc
  if [[ " ${BENCH_SMPCACHE_PREPARED_IPS} " == *" ${ip} "* ]]; then
    return 0
  fi

  dir="$(remote_system_dir SMPCache)"
  log_file="${ROOT_DIR}/${BENCH_LOG_DIR}/SMPCache/prepare_${ip//./_}.log"
  mkdir -p "$(dirname "${log_file}")"
  prepare_rc=0
  bench_log "SMPCache: clone/update and stage LAN party runner on ${ip}"
  {
    echo "SMPCache prepare start: $(date '+%F %T')"
    echo "node=${ip}"
    bench_clone_or_update_system "${ip}" SMPCache
    bench_clone_or_update_system "${ip}" Rosetta
    bench_rsync_to "${ip}" "${ROOT_DIR}/systems/SMPCache/overlay/obliviousSort_lan_party.py" "${dir}/"
    bench_smpcache_write_config "${ip}" "${dir}"
    if ! bench_ssh "${ip}" "set -euo pipefail
$(bench_smpcache_remote_env_setup_script "${BENCH_REMOTE_RESULTS_DIR}/logs/SMPCache/build_${ip//./_}")
    "; then
      echo "SMPCache prepare failed on ${ip}; downloading remote build logs"
      bench_rsync_from "${ip}" "${BENCH_REMOTE_RESULTS_DIR}/logs/SMPCache/build_${ip//./_}/" "${ROOT_DIR}/${BENCH_LOG_DIR}/SMPCache/build_${ip//./_}/" || true
      prepare_rc=1
    else
      bench_rsync_from "${ip}" "${BENCH_REMOTE_RESULTS_DIR}/logs/SMPCache/build_${ip//./_}/" "${ROOT_DIR}/${BENCH_LOG_DIR}/SMPCache/build_${ip//./_}/" || true
      echo "SMPCache prepare done: $(date '+%F %T')"
    fi
  } > "${log_file}" 2>&1
  mkdir -p "${ROOT_DIR}/${BENCH_RESULTS_DIR}/logs/SMPCache"
  local result_prepare_log="${ROOT_DIR}/${BENCH_RESULTS_DIR}/logs/SMPCache/prepare_${ip//./_}.log"
  if [[ "${log_file}" != "${result_prepare_log}" ]]; then
    cp -f "${log_file}" "${result_prepare_log}" || true
  fi
  (( prepare_rc == 0 )) || return "${prepare_rc}"
  BENCH_SMPCACHE_PREPARED_IPS="${BENCH_SMPCACHE_PREPARED_IPS} ${ip}"
}

bench_smpcache_prepare_nodes() {
  local ips=("$@")
  local ip pid rc=0
  local -a pids=()
  local -a launched=()

  for ip in "${ips[@]}"; do
    if [[ " ${BENCH_SMPCACHE_PREPARED_IPS} " == *" ${ip} "* ]]; then
      continue
    fi
    bench_smpcache_prepare_node "${ip}" &
    pids+=("$!")
    launched+=("${ip}")
  done

  for pid in "${pids[@]}"; do
    wait "${pid}" || rc=1
  done
  (( rc == 0 )) || return "${rc}"
  for ip in "${launched[@]}"; do
    if [[ " ${BENCH_SMPCACHE_PREPARED_IPS} " != *" ${ip} "* ]]; then
      BENCH_SMPCACHE_PREPARED_IPS="${BENCH_SMPCACHE_PREPARED_IPS} ${ip}"
    fi
  done
}

bench_smpcache_stage_case_all() {
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

bench_run_case() {
  local system="$1" group="$2" algorithm="$3" pow="$4" cols="$5" keys="$6" dtype="$7" dist="$8" bits="$9" input_file="${10}" slug="${11}" rep="${12}"
  : "${group}" "${pow}" "${cols}" "${dist}" "${bits}"
  case "${algorithm}" in
    odd_even|odd-even) ;;
    *)
      bench_log "SMPCache: skip unsupported sorting algorithm '${algorithm}' for ${slug}"
      return 0
      ;;
  esac
  if [[ "${keys}" != "1" ]]; then
    bench_log "SMPCache: skip ${slug}; current Rosetta sort runner supports one key column"
    return 0
  fi
  if [[ "${dtype}" != int* ]]; then
    bench_log "SMPCache: skip ${slug}; current runner maps benchmark inputs to Rosetta int tensors"
    return 0
  fi

  local p0 p1 p2 dir input_remote log_dir
  p0="$(party_ip 0)"
  p1="$(party_ip 1)"
  p2="$(party_ip 2)"
  dir="$(remote_system_dir SMPCache)"
  input_remote="$(remote_case_data_path "${input_file}")"
  log_dir="$(remote_case_log_dir "${system}" "${slug}" "${rep}")"

  bench_smpcache_prepare_nodes "${p0}" "${p1}" "${p2}"
  bench_smpcache_stage_case_all "${input_file}" "${log_dir}"

  # The revision driver performs workspace-scoped cleanup before/after each case.

  bench_log "SMPCache: launch Rosetta SecureNN 3PC over ${p0},${p1},${p2}"
  set +e
  for party in 1 2; do
    local ip
    ip="$(party_ip "${party}")"
    bench_ssh "${ip}" "set -euo pipefail
$(bench_smpcache_remote_env_setup_script "${log_dir}/env_party${party}")
      cd '${dir}'
      export FES_REVISION_BENCH='${FES_REVISION_BENCH:-0}'
      /usr/bin/time -v -o '${log_dir}/party${party}.time' timeout --kill-after=15s '${SMPCACHE_TIMEOUT_S}' python obliviousSort_lan_party.py \
        --party-id '${party}' \
        --input '${input_remote}' \
        --sort-col 0 \
        --dtype int > '${log_dir}/party${party}.log' 2>&1
    " &
    eval "party_${party}_pid=$!"
  done
  sleep 2
  bench_ssh "${p0}" "set -euo pipefail
$(bench_smpcache_remote_env_setup_script "${log_dir}/env_party0")
    cd '${dir}'
    export FES_REVISION_BENCH='${FES_REVISION_BENCH:-0}'
    /usr/bin/time -v -o '${log_dir}/party0.time' timeout --kill-after=15s '${SMPCACHE_TIMEOUT_S}' python obliviousSort_lan_party.py \
      --party-id 0 \
      --input '${input_remote}' \
      --sort-col 0 \
      --dtype int > '${log_dir}/party0.log' 2>&1
  "
  local party_0_status=$?
  wait "${party_1_pid}"
  local party_1_status=$?
  wait "${party_2_pid}"
  local party_2_status=$?
  set -e

  for ip in "${p0}" "${p1}" "${p2}"; do
    bench_cleanup_node "${ip}" SMPCache || true
  done

  bench_rsync_from "${p0}" "${log_dir}/" "${ROOT_DIR}/${BENCH_RAW_DIR}/${system}/${slug}/rep${rep}/p0/" || true
  bench_rsync_from "${p1}" "${log_dir}/" "${ROOT_DIR}/${BENCH_RAW_DIR}/${system}/${slug}/rep${rep}/p1/" || true
  bench_rsync_from "${p2}" "${log_dir}/" "${ROOT_DIR}/${BENCH_RAW_DIR}/${system}/${slug}/rep${rep}/p2/" || true

  if (( party_0_status != 0 || party_1_status != 0 || party_2_status != 0 )); then
    bench_log "SMPCache: failed ${slug} repeat=${rep}; party_statuses=${party_0_status},${party_1_status},${party_2_status}"
    return 1
  fi

  return 0
}
