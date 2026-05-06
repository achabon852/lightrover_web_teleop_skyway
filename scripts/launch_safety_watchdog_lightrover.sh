#!/usr/bin/env bash
set -eo pipefail
source /opt/ros/jazzy/setup.bash
source ~/lightrover_safety_ws/install/setup.bash
set -u
export RMW_IMPLEMENTATION=${RMW_IMPLEMENTATION:-rmw_cyclonedds_cpp}
export ROS_LOCALHOST_ONLY=${ROS_LOCALHOST_ONLY:-0}
export ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-1}
ros2 run lightrover_safety_watchdog safety_watchdog --ros-args \
  -p input_topic:=/rover_twist_cmd \
  -p output_topic:=/rover_twist \
  -p timeout_sec:=0.35 \
  -p publish_hz:=20.0 \
  -p enabled:=true
