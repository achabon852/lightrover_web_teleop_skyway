#!/usr/bin/env bash
set -eo pipefail
source /opt/ros/jazzy/setup.bash
set -u
export RMW_IMPLEMENTATION=${RMW_IMPLEMENTATION:-rmw_cyclonedds_cpp}
export ROS_LOCALHOST_ONLY=${ROS_LOCALHOST_ONLY:-0}
export ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-1}
ros2 run usb_cam usb_cam_node_exe --ros-args \
  -p video_device:=${VIDEO_DEVICE:-/dev/video0} \
  -p image_width:=${IMAGE_WIDTH:-640} \
  -p image_height:=${IMAGE_HEIGHT:-480} \
  -p framerate:=${FRAMERATE:-15.0} \
  -p pixel_format:=${PIXEL_FORMAT:-mjpeg2rgb}
