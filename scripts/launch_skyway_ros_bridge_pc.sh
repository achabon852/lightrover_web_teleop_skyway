#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

set +u
source /opt/ros/jazzy/setup.bash

if [[ -n "${SKYWAY_ROS_BRIDGE_SETUP:-}" ]]; then
  source "${SKYWAY_ROS_BRIDGE_SETUP}"
elif [[ -f "${ROOT_DIR}/../skyway_ros_bridge/install/setup.bash" ]]; then
  source "${ROOT_DIR}/../skyway_ros_bridge/install/setup.bash"
elif [[ -f "${HOME}/skyway_ros_bridge/install/setup.bash" ]]; then
  source "${HOME}/skyway_ros_bridge/install/setup.bash"
fi
set -u

cd "${ROOT_DIR}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
if [[ -x "${ROOT_DIR}/.venv/bin/python" ]]; then
  PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"
fi
exec "${PYTHON_BIN}" scripts/skyway_ros_bridge_v9.py --start-bridge --keep-alive "$@"
