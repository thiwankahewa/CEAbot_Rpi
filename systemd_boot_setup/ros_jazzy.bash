#!/usr/bin/env bash
# Source this file to make ROS 2 Jazzy commands and Python packages available.

ROS_SETUP_FILE=/opt/ros/jazzy/setup.bash

if [ ! -r "$ROS_SETUP_FILE" ]; then
    echo "ROS 2 Jazzy setup file not found: $ROS_SETUP_FILE" >&2
    return 1 2>/dev/null || exit 1
fi

source "$ROS_SETUP_FILE"

# Source a local Orbbec overlay if one is built in the usual location.
ORBBEC_OVERLAY=/home/thiwa/orbbec_ws/install/setup.bash
if [ -r "$ORBBEC_OVERLAY" ]; then
    source "$ORBBEC_OVERLAY"
fi

unset ROS_SETUP_FILE ORBBEC_OVERLAY
