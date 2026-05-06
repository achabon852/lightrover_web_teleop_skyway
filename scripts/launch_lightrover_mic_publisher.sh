#!/usr/bin/env bash
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

source /opt/ros/jazzy/setup.bash
set -u

: "${ROS_DOMAIN_ID:=1}"
: "${RMW_IMPLEMENTATION:=rmw_cyclonedds_cpp}"
: "${ROS_LOCALHOST_ONLY:=0}"

export ROS_DOMAIN_ID
export RMW_IMPLEMENTATION
export ROS_LOCALHOST_ONLY

python3 "${PROJECT_DIR}/scripts/lightrover_mic_publisher.py" \
  --topic "${LIGHTROVER_AUDIO_TOPIC:-/lightrover/audio/pcm_s16le}" \
  --device "${LIGHTROVER_AUDIO_DEVICE:-default}" \
  --rate "${LIGHTROVER_AUDIO_RATE:-16000}" \
  --channels "${LIGHTROVER_AUDIO_CHANNELS:-1}" \
  --chunk-ms "${LIGHTROVER_AUDIO_CHUNK_MS:-20}"
