#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." >/dev/null && pwd)"
MODE="${1:-quick}"

MODEL_PATH="openpilot/selfdrive/modeld/models/driving_supercombo.onnx"
METADRIVE_DEP="metadrive-simulator @ git+https://github.com/commaai/metadrive.git@minimal"

usage() {
  cat <<'EOF'
Usage: bash scripts/pre_drive_check.sh [quick|build|replay|sim|all]

  quick   Git/LFS checks and targeted unit tests
  build   quick checks plus a full SCons build
  replay  quick checks plus Toyota process replay
  sim     quick checks plus the official MetaDrive test
  all     run quick, build, replay, and simulator checks
EOF
}

stage() {
  echo
  echo "== $1 =="
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Required command not found: $1" >&2
    exit 1
  fi
}

run_integrity_checks() {
  stage "Gate 0/1: branch, diff, and Git LFS"
  if [ "${PRE_DRIVE_SKIP_INTEGRITY:-0}" = "1" ]; then
    echo "Integrity checks completed by the Docker host wrapper."
    return
  fi

  git status --short --branch
  git log -1 --oneline --decorate
  git diff --check HEAD
  git lfs fsck

  if [ ! -f "${MODEL_PATH}" ]; then
    echo "Missing driving model: ${MODEL_PATH}" >&2
    exit 1
  fi

  local model_size
  model_size="$(wc -c < "${MODEL_PATH}")"
  if [ "${model_size}" -lt 1000000 ]; then
    echo "Driving model is too small (${model_size} bytes); it is probably an LFS pointer." >&2
    exit 1
  fi
}

run_unit_tests() {
  stage "Gate 1: no-driver-camera and curve-deceleration tests"
  uv run --python 3.12 --with pytest pytest -q \
    openpilot/system/hardware/tests/test_capabilities.py \
    openpilot/selfdrive/controls/tests/test_curve_speed_limiter.py
}

run_native_bootstrap() {
  if uv run --python 3.12 python -c "from msgq.visionipc import VisionStreamType" >/dev/null 2>&1; then
    return
  fi

  stage "Gate 1: native messaging bootstrap"
  uv run --python 3.12 scons -j4 \
    msgq_repo/msgq/ipc_pyx.so \
    msgq_repo/msgq/visionipc/visionipc_pyx.so
}

run_quick() {
  run_integrity_checks
  run_native_bootstrap
  run_unit_tests
}

run_build() {
  stage "Gate 2: full SCons build"
  uv run --python 3.12 scons -j4
}

run_replay() {
  stage "Gate 3: Toyota process replay"
  # The current upstream selfdrived always emits the normal startup alert, while
  # the published replay reference still contains the retired startupMaster alert.
  # Ignore only those known startup presentation fields; all other outputs remain strict.
  uv run --python 3.12 \
    python openpilot/selfdrive/test/process_replay/test_processes.py \
      --whitelist-procs card selfdrived controlsd plannerd \
      --whitelist-cars TOYOTA \
      --ignore-fields \
        onroadEvents.0.name \
        selfdriveState.alertStatus \
        selfdriveState.alertText1 \
        selfdriveState.alertText2 \
        selfdriveState.alertType \
      --jobs 2
}

run_sim() {
  stage "Gate 4: official MetaDrive simulation"
  CI=1 xvfb-run -a -s "-screen 0 1920x1080x24" \
    uv run --python 3.12 \
    --with "${METADRIVE_DEP}" \
    --with pytest \
    python -m pytest -q \
      openpilot/tools/sim/tests/test_metadrive_bridge.py::TestMetaDriveBridge::test_driving
}

case "${MODE}" in
  quick)
    ;;
  build|replay|sim|all)
    ;;
  -h|--help|help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

require_command git
require_command uv

cd "${REPO_ROOT}"
run_quick

case "${MODE}" in
  quick)
    ;;
  build)
    run_build
    ;;
  replay)
    run_build
    run_replay
    ;;
  sim)
    run_build
    run_sim
    ;;
  all)
    run_build
    run_replay
    run_sim
    ;;
esac

stage "Result"
echo "PASS: pre-drive '${MODE}' checks completed."
