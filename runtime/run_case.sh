#!/usr/bin/env bash
# Run one actual native sorting case on isolated workers. No inferred timeouts.
set -euo pipefail
RUNTIME_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${RUNTIME_DIR}/lib.sh"
system='' group=R1 algorithm='' power=16 columns=2 keys=1 dtype=int32
distribution=Uniform bits=32 security=SH input='' run_id='' repeat=1 config=''
source_profile=base prepare=0 public_input=0
usage() {
  cat <<'EOF'
Usage: bash runtime/run_case.sh --system ID --algorithm TAG --config JSON [options]
  --group R1..R11        Paper experiment group
  --power N             Input cardinality is 2^N
  --columns M --keys K  Projected columns and leading sorting keys
  --dtype TYPE --distribution NAME --bits B
  --security SH|MAL --source-profile base|native_checked
  --input PATH          Repository-relative generated TPC-H input
  --benchmark-public-input  Authorize post-sort opening of public benchmark data
  --run-id ID --repeat N
  --prepare             Only fetch, stage, and build the selected native backend
EOF
}
while (($#)); do
  case "$1" in
    --prepare) prepare=1; shift;;
    --benchmark-public-input) public_input=1; shift;;
    --help|-h) usage; exit 0;;
    --system|--group|--algorithm|--power|--columns|--keys|--dtype|--distribution|--bits|--security|--input|--run-id|--repeat|--config|--source-profile)
      (($# >= 2)) || { usage >&2; exit 2; }
      name="${1#--}"; name="${name//-/_}"
      printf -v "${name}" '%s' "$2"; shift 2;;
    *) bench_die "Unknown option: $1"; exit 2;;
  esac
done
if [[ -z "$algorithm" && "$prepare" == 1 ]]; then
  case "$system" in MP-SPDZ|congregation|SMPCache|spu) algorithm=batcher;; *) algorithm=bitonic;; esac
fi
[[ -n "$system" && -n "$algorithm" && -f "$config" ]] || { usage >&2; exit 2; }
[[ "$security" == SH || "$security" == MAL ]] || bench_die 'security must be SH or MAL'
[[ "$source_profile" == base || "$source_profile" == native_checked ]] || bench_die 'Unknown source profile'
[[ "$group" =~ ^R([1-9]|10|11)(_[A-Za-z0-9_]+)?$ ]] || bench_die 'Invalid experiment group'
for value in "$power" "$columns" "$keys" "$bits" "$repeat"; do
  [[ "$value" =~ ^[0-9]+$ ]] || bench_die 'Numeric case options must be nonnegative integers'
done
((power <= 20 && columns >= 1 && columns <= 11 && keys >= 1 && keys <= columns && bits >= 1 && bits <= 64 && repeat >= 1)) || bench_die 'Invalid input shape, width, or repeat'
[[ "$dtype" =~ ^(int8|int32|int64|float)$ && "$distribution" =~ ^(Uniform|Zipf|PartiallySorted|HighRepeat)$ ]] || bench_die 'Unsupported dtype or distribution'
case "$system:$algorithm" in
  smcql:bitonic|alchemy:bitonic|Secrecy:bitonic|MASQUE:bitonic|MASQUE:quick|orq:bitonic|orq:radix|orq:quick|mpc4j:bitonic|mpc4j:radix|mpc4j:quick|MP-SPDZ:batcher|MP-SPDZ:radix|congregation:odd_even|congregation:batcher|SMPCache:odd_even|SMPCache:batcher|spu:odd_even|spu:batcher|spu:radix|spu:quick) ;;
  *) bench_die 'System and sorting algorithm are not supported by a native adapter';;
esac
if [[ "$security" == MAL ]]; then
  case "$system" in mpc4j|MP-SPDZ) ;; orq) [[ "$source_profile" == native_checked ]] || bench_die 'ORQ malicious security requires native_checked';; *) bench_die 'No qualified malicious backend for this system';; esac
fi
if [[ "$source_profile" == native_checked ]]; then
  [[ "$system" == orq && ( "$power" == 8 || "$power" == 16 ) && "$columns" == 2 && "$keys" == 1 && "$dtype" == int32 ]] || bench_die 'native_checked ORQ requires 256 or 65536 rows, two int32 columns, and one key'
fi
if [[ "$system" != orq && "$source_profile" != base ]]; then bench_die 'Only ORQ has a separate source profile'; fi
case "$system" in MP-SPDZ|MASQUE|congregation|SMPCache) [[ "$keys" == 1 ]] || bench_die 'This native harness supports one sorting key';; esac
case "$system" in MASQUE) [[ "$dtype" == int8 || "$dtype" == int32 ]] || bench_die 'MASQUE native inputs are 32-bit integers';; congregation|SMPCache|Secrecy|MP-SPDZ|mpc4j) [[ "$dtype" == int* ]] || bench_die 'This native harness supports integer input only';; esac
run_id="${run_id:-run-$(date -u '+%Y%m%dT%H%M%SZ')}"
[[ "$run_id" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ && ${#run_id} -le 100 && "$run_id" != . && "$run_id" != .. ]] || bench_die 'run-id must be a short safe identifier'
export RUN_ID="$run_id" BENCH_SOURCE_PROFILE="$source_profile" BENCH_SECURITY="$security"
export BENCH_RESULTS_DIR="workspace/runs/${RUN_ID}"
network="${FES_NETWORK_CONDITION:-LAN}"
[[ "$network" == LAN || "$network" == WAN ]] || bench_die 'Network condition must be LAN or WAN'
if ((prepare == 0)); then
  ((public_input == 1)) || bench_die 'Pass --benchmark-public-input to authorize verification of public TPC-H inputs'
  [[ -n "$input" ]] || bench_die 'input is required for execution'
  verified_environment="$(python3 "${RUNTIME_DIR}/input_env.py" "${ROOT_DIR}" "$input" "$power" "$columns" "$keys" "$dtype" "$distribution")"
  eval "$verified_environment"
fi
slug="${system}-${group}-${algorithm}-${security}-n${power}-m${columns}-k${keys}-${dtype}-${distribution}-b${bits}-${source_profile}-${network}"
[[ -z "${FES_INPUT_SHA256:-}" ]] || slug+="-${FES_INPUT_SHA256:0:12}"
export BENCH_CURRENT_CASE_DIR="${ROOT_DIR}/${BENCH_RESULTS_DIR}/cases/${slug}"
export BENCH_RAW_DIR="${BENCH_RESULTS_DIR}/cases/${slug}/raw"
export BENCH_LOG_DIR="${BENCH_RESULTS_DIR}/build-logs"
export BENCH_FORCE_REBUILD="${BENCH_FORCE_REBUILD:-0}"
mkdir -p "${BENCH_CURRENT_CASE_DIR}" "${ROOT_DIR}/${BENCH_LOG_DIR}"
# Lock a controller workspace because native backends reuse fixed port/compiler paths.
exec 9>"${ROOT_DIR}/workspace/.runtime.lock"
flock -n 9 || bench_die 'Another native case is running from this controller workspace'
[[ ! -e "${BENCH_CURRENT_CASE_DIR}/result.json" ]] || bench_die 'This case already has a result; use a new run-id'
bench_init_transport "$config"
export SMCQL_TWO_NODE=1
export MP_SPDZ_PROTOCOL=replicated-field-party
[[ "$security" == SH ]] || export MP_SPDZ_PROTOCOL=malicious-rep-field-party
adapter="$system"
[[ "$system:$source_profile" != orq:native_checked ]] || adapter=orq_native
source "${RUNTIME_DIR}/adapters/${adapter}.sh"
parties=3
case "$system" in smcql|MASQUE|alchemy) parties=2;; esac
[[ "$system:$security" != orq:MAL ]] || parties=4
python3 "${RUNTIME_DIR}/result.py" --metadata "${BENCH_CURRENT_CASE_DIR}/case.json" \
  --system "$system" --group "$group" --algorithm "$algorithm" --power "$power" \
  --columns "$columns" --keys "$keys" --dtype "$dtype" --distribution "$distribution" \
  --bits "$bits" --security "$security" --profile "$source_profile" --network "$network" \
  --input "$input" --input-sha256 "${FES_INPUT_SHA256:-}" --parties "$parties" --repeat "$repeat"
set +e
(set -e; bench_prepare_system "$system" "$parties")
prepare_status=$?
set -e
if ((prepare_status != 0)); then
  python3 "${RUNTIME_DIR}/result.py" --case-directory "${BENCH_CURRENT_CASE_DIR}" --status PREPARE_FAILED --exit-code "$prepare_status"
  exit 1
fi
if ((prepare == 1)); then
  printf 'Native backend prepared successfully on %s workers.\n' "$parties"
  exit 0
fi
# Prepared flags are set only after every requested node completed the real build.
prepared_name="${system^^}"; prepared_name="${prepared_name//-/_}"
printf -v "BENCH_${prepared_name}_PREPARED_IPS" '%s' "$(party_ips "$parties" | tr '\n' ' ')"
export "BENCH_${prepared_name}_PREPARED_IPS"
[[ "$system" != MP-SPDZ ]] || export BENCH_MP_SPDZ_CERTS_READY=1
bench_cleanup_system "$system" "$parties"
native_group="$group"
case "$group" in
  R1) native_group=R1_algorithm_bitonic;; R2) native_group=R2_algorithm_odd_even;;
  R3) native_group=R3_algorithm_radix;; R4) native_group=R4_algorithm_quick;;
  R5) native_group=R5_rows;; R6) native_group=R6_keys;; R7) native_group=R7_payloads;;
  R8) native_group=R8_key_type;;
  R9) case "$distribution" in Zipf) native_group=R9_zipf;; PartiallySorted) native_group=R9_partially_sorted;; HighRepeat) native_group=R9_high_repeat;; *) bench_die 'R9 requires an input-distribution variant';; esac;;
  R11) case "$algorithm" in bitonic) native_group=R1_algorithm_bitonic;; odd_even|batcher) native_group=R2_algorithm_odd_even;; radix) native_group=R3_algorithm_radix;; quick) native_group=R4_algorithm_quick;; esac;;
esac
if [[ "$system" == mpc4j || "$group" == R10 ]]; then
  native_group=R10_protocol_semi_honest
  [[ "$security" == SH ]] || native_group=R10_protocol_malicious
fi
set +e
(set -e; bench_run_case "$system" "$native_group" "$algorithm" "$power" "$columns" "$keys" "$dtype" "$distribution" "$bits" "$input" "$slug" "$repeat")
case_status=$?
set -e
bench_cleanup_system "$system" "$parties" || bench_log 'Owned-process cleanup was incomplete; inspect the workers before another run'
python3 "${RUNTIME_DIR}/result.py" --case-directory "${BENCH_CURRENT_CASE_DIR}" --exit-code "$case_status"
