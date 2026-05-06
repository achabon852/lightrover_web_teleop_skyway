#!/usr/bin/env bash
set -eo pipefail
if [ $# -lt 1 ]; then
  echo "Usage: $0 /path/to/map.yaml [ROS_DOMAIN_ID]" >&2
  exit 1
fi
MAP_YAML=$1
export ROS_DOMAIN_ID=${2:-${ROS_DOMAIN_ID:-1}}
export RMW_IMPLEMENTATION=${RMW_IMPLEMENTATION:-rmw_cyclonedds_cpp}
export ROS_LOCALHOST_ONLY=${ROS_LOCALHOST_ONLY:-0}
source /opt/ros/jazzy/setup.bash
set -u
ros2 launch nav2_bringup localization_launch.py \
  map:=${MAP_YAML} \
  params_file:=$(pwd)/nav2/amcl_params_lightrover.yaml \
  use_sim_time:=false
