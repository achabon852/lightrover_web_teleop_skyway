#!/usr/bin/env bash
set -eo pipefail
source /opt/ros/jazzy/setup.bash
set -u
export RMW_IMPLEMENTATION=${RMW_IMPLEMENTATION:-rmw_cyclonedds_cpp}
export ROS_LOCALHOST_ONLY=${ROS_LOCALHOST_ONLY:-0}
export ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-1}
echo "Checking map -> base_link. Press Ctrl+C after Translation appears."
ros2 run tf2_ros tf2_echo map base_link
