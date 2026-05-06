#!/usr/bin/env bash
set -eo pipefail
source /opt/ros/jazzy/setup.bash
set -u
export RMW_IMPLEMENTATION=${RMW_IMPLEMENTATION:-rmw_cyclonedds_cpp}
export ROS_LOCALHOST_ONLY=${ROS_LOCALHOST_ONLY:-0}
export ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-1}
echo "--- topic list ---"
ros2 topic list | grep -E 'image|scan|odom|tf|rover_twist|map|amcl' || true
echo "--- image hz ---"
timeout 5 ros2 topic hz /image_raw/compressed || true
echo "--- scan hz ---"
timeout 5 ros2 topic hz /scan || true
echo "--- odom hz ---"
timeout 5 ros2 topic hz /odom || true
