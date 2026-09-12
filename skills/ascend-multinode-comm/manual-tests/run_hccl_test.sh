#!/usr/bin/env bash

# Usage:
#   bash run_hccl_test.sh alltoall  ./hostfile_8x2
#   bash run_hccl_test.sh broadcast ./hostfile_pair 7
set -o pipefail

OP="${1:-alltoall}"
HOSTFILE="${2:?usage: $0 <alltoall|broadcast> <hostfile> [device_id]}"
DEVICE_ID="${3:-0}"

: "${MPI_HOME:?set MPI_HOME to the MPICH directory}"
: "${CANN_HOME:?set CANN_HOME to the CANN directory}"
: "${HCCL_TEST_DIR:?set HCCL_TEST_DIR to the hccl_test directory}"
: "${HCCL_SOCKET_IFNAME:?set HCCL_SOCKET_IFNAME to the communication interface}"

source "${CANN_ENV:-$CANN_HOME/set_env.sh}"
export PATH="$MPI_HOME/bin:$PATH"
export LD_LIBRARY_PATH="$MPI_HOME/lib:$CANN_HOME/lib64:${LD_LIBRARY_PATH:-}"
export HCCL_BUFFSIZE="${HCCL_BUFFSIZE:-2048}"
export HCCL_SOCKET_IFNAME

WORLD_SIZE=$(awk -F: '{sum += $NF} END {print sum}' "$HOSTFILE")
RANKS_PER_NODE=$(awk -F: 'NR == 1 {print $NF}' "$HOSTFILE")
MPI_IFACE="${MPI_IFACE:-$HCCL_SOCKET_IFNAME}"
MPI=("$MPI_HOME/bin/mpirun" -launcher ssh -iface "$MPI_IFACE" \
  -f "$HOSTFILE" -n "$WORLD_SIZE" -prepend-rank)

case "$OP" in
  alltoall)
    unset HCCL_TEST_USE_DEVS
    TEST_BIN="$HCCL_TEST_DIR/bin/alltoall_test"
    TEST_ARGS=(-b "${MIN_SIZE:-8}" -e "${MAX_SIZE:-1G}" -f "${FACTOR:-2}" -p "$RANKS_PER_NODE")
    if [ "${USE_AIV:-0}" = 1 ]; then
      TEST_ARGS+=(-a aiv)
    fi
    TIMEOUT_S="${TIMEOUT_S:-300}"
    ;;
  broadcast)
    export HCCL_TEST_USE_DEVS="$DEVICE_ID"
    TEST_BIN="$HCCL_TEST_DIR/bin/broadcast_test"
    TEST_ARGS=(-b "${MIN_SIZE:-8K}" -e "${MAX_SIZE:-8K}" -f "${FACTOR:-2}" \
      -d fp32 -p "$RANKS_PER_NODE" -n "${ITERS:-1}" -w "${WARMUP:-0}" -c 1 -r 0)
    TIMEOUT_S="${TIMEOUT_S:-90}"
    ;;
  *)
    echo "unknown operation: $OP" >&2
    exit 2
    ;;
esac

LOG_FILE="${LOG_FILE:-hccl-${OP}-$(date +%Y%m%d-%H%M%S).log}"
timeout --signal=INT --kill-after=10s "$TIMEOUT_S" \
  "${MPI[@]}" "$TEST_BIN" "${TEST_ARGS[@]}" 2>&1 | tee "$LOG_FILE"

rc=${PIPESTATUS[0]}
echo "HCCL test exit code: $rc"
exit "$rc"
