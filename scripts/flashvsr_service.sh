#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

ACTION="${1:-}"
SERVICE_SCALE="${2:-${FLASHVSR_SERVICE_SCALE:-2}}"

FLASHVSR_UPSTREAM_ROOT="${FLASHVSR_UPSTREAM_ROOT:-${HOME}/.openclaw/workspace/mycode/FlashVSR}"
FLASHVSR_PYTHON="${FLASHVSR_PYTHON:-${FLASHVSR_UPSTREAM_ROOT}/.venv/bin/python}"
FLASHVSR_HOST="${FLASHVSR_HOST:-0.0.0.0}"
FLASHVSR_PORT="${FLASHVSR_PORT:-8000}"
FLASHVSR_MAX_QUEUED_JOBS="${FLASHVSR_MAX_QUEUED_JOBS:-0}"

scale_tag="${SERVICE_SCALE//./_}"

if [[ "${SERVICE_SCALE}" == "2" || "${SERVICE_SCALE}" == "2.0" ]]; then
  DEFAULT_STATE_DIR="${REPO_ROOT}/service_state"
else
  DEFAULT_STATE_DIR="${REPO_ROOT}/service_state_x${scale_tag}"
fi

STATE_DIR="${FLASHVSR_STATE_DIR:-${DEFAULT_STATE_DIR}}"
PID_FILE="${FLASHVSR_PID_FILE:-${REPO_ROOT}/.omx/state/service_x${scale_tag}.pid}"
LOG_FILE="${FLASHVSR_LOG_FILE:-${REPO_ROOT}/.omx/logs/service_x${scale_tag}.log}"

usage() {
  cat <<EOF
Usage:
  $(basename "$0") start [scale]
  $(basename "$0") stop [scale]
  $(basename "$0") restart [scale]
  $(basename "$0") status [scale]
  $(basename "$0") logs [scale]
  $(basename "$0") health [scale]

Examples:
  $(basename "$0") start
  $(basename "$0") start 4
  $(basename "$0") stop 4
  $(basename "$0") logs 2

Environment overrides:
  FLASHVSR_UPSTREAM_ROOT   Default: ${FLASHVSR_UPSTREAM_ROOT}
  FLASHVSR_PYTHON          Default: ${FLASHVSR_PYTHON}
  FLASHVSR_HOST            Default: ${FLASHVSR_HOST}
  FLASHVSR_PORT            Default: ${FLASHVSR_PORT}
  FLASHVSR_MAX_QUEUED_JOBS Default: ${FLASHVSR_MAX_QUEUED_JOBS}
  FLASHVSR_STATE_DIR       Default: ${STATE_DIR}
  FLASHVSR_PID_FILE        Default: ${PID_FILE}
  FLASHVSR_LOG_FILE        Default: ${LOG_FILE}
EOF
}

require_action() {
  if [[ -z "${ACTION}" ]]; then
    usage
    exit 1
  fi
}

ensure_paths() {
  if [[ ! -d "${FLASHVSR_UPSTREAM_ROOT}" ]]; then
    echo "Upstream FlashVSR root not found: ${FLASHVSR_UPSTREAM_ROOT}" >&2
    exit 1
  fi
  if [[ ! -x "${FLASHVSR_PYTHON}" ]]; then
    echo "FlashVSR Python not found or not executable: ${FLASHVSR_PYTHON}" >&2
    exit 1
  fi
  mkdir -p "$(dirname "${PID_FILE}")"
  mkdir -p "$(dirname "${LOG_FILE}")"
  mkdir -p "${STATE_DIR}"
}

pid_is_running() {
  local pid="$1"
  kill -0 "${pid}" 2>/dev/null
}

read_pid() {
  if [[ -f "${PID_FILE}" ]]; then
    cat "${PID_FILE}"
  fi
}

remove_stale_pid_file() {
  local pid
  pid="$(read_pid || true)"
  if [[ -n "${pid}" ]] && ! pid_is_running "${pid}"; then
    rm -f "${PID_FILE}"
  fi
}

start_service() {
  ensure_paths
  remove_stale_pid_file

  local existing_pid
  existing_pid="$(read_pid || true)"
  if [[ -n "${existing_pid}" ]] && pid_is_running "${existing_pid}"; then
    echo "Service already running for scale ${SERVICE_SCALE}: PID ${existing_pid}"
    echo "State dir: ${STATE_DIR}"
    echo "Log file: ${LOG_FILE}"
    return 0
  fi

  local -a cmd=(
    env
    "PYTHONPATH=${REPO_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"
    "${FLASHVSR_PYTHON}"
    -u
    -m
    flashvsr_long_video_runner.cli
    serve
    --host
    "${FLASHVSR_HOST}"
    --port
    "${FLASHVSR_PORT}"
    --state-dir
    "${STATE_DIR}"
    --scale
    "${SERVICE_SCALE}"
    --upstream-root
    "${FLASHVSR_UPSTREAM_ROOT}"
  )

  if [[ "${FLASHVSR_MAX_QUEUED_JOBS}" != "0" ]]; then
    cmd+=(
      --max-queued-jobs
      "${FLASHVSR_MAX_QUEUED_JOBS}"
    )
  fi

  : > "${LOG_FILE}"
  (
    cd "${REPO_ROOT}"
    nohup "${cmd[@]}" >>"${LOG_FILE}" 2>&1 &
    echo $! > "${PID_FILE}"
  )

  local pid
  pid="$(read_pid)"
  sleep 1
  if [[ -n "${pid}" ]] && pid_is_running "${pid}"; then
    echo "Started FlashVSR service"
    echo "  PID: ${pid}"
    echo "  Scale: ${SERVICE_SCALE}"
    echo "  Host: ${FLASHVSR_HOST}"
    echo "  Port: ${FLASHVSR_PORT}"
    echo "  State dir: ${STATE_DIR}"
    echo "  Log file: ${LOG_FILE}"
    return 0
  fi

  echo "Service failed to stay up. Last log lines:" >&2
  tail -n 40 "${LOG_FILE}" >&2 || true
  rm -f "${PID_FILE}"
  exit 1
}

stop_service() {
  remove_stale_pid_file

  local pid
  pid="$(read_pid || true)"
  if [[ -z "${pid}" ]]; then
    echo "Service is not running for scale ${SERVICE_SCALE}"
    return 0
  fi

  if ! pid_is_running "${pid}"; then
    rm -f "${PID_FILE}"
    echo "Service was not running; removed stale PID file"
    return 0
  fi

  kill "${pid}" 2>/dev/null || true
  for _ in {1..10}; do
    if ! pid_is_running "${pid}"; then
      rm -f "${PID_FILE}"
      echo "Stopped FlashVSR service PID ${pid}"
      return 0
    fi
    sleep 1
  done

  kill -9 "${pid}" 2>/dev/null || true
  rm -f "${PID_FILE}"
  echo "Force-stopped FlashVSR service PID ${pid}"
}

status_service() {
  remove_stale_pid_file

  local pid
  pid="$(read_pid || true)"
  if [[ -n "${pid}" ]] && pid_is_running "${pid}"; then
    echo "running"
    echo "  PID: ${pid}"
    echo "  Scale: ${SERVICE_SCALE}"
    echo "  State dir: ${STATE_DIR}"
    echo "  Log file: ${LOG_FILE}"
    return 0
  fi

  echo "stopped"
  echo "  Scale: ${SERVICE_SCALE}"
  echo "  State dir: ${STATE_DIR}"
  echo "  Log file: ${LOG_FILE}"
}

logs_service() {
  ensure_paths
  touch "${LOG_FILE}"
  echo "Tailing ${LOG_FILE}"
  tail -f "${LOG_FILE}"
}

health_service() {
  local url="http://127.0.0.1:${FLASHVSR_PORT}/healthz"
  curl -sS "${url}"
}

restart_service() {
  stop_service
  start_service
}

require_action

case "${ACTION}" in
  start)
    start_service
    ;;
  stop)
    stop_service
    ;;
  restart)
    restart_service
    ;;
  status)
    status_service
    ;;
  logs)
    logs_service
    ;;
  health)
    health_service
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    echo "Unknown action: ${ACTION}" >&2
    usage
    exit 1
    ;;
esac
