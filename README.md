# Gemini 336 capture over the Gen3 expansion Ethernet

The Raspberry Pi runs the Orbbec ROS 2 driver and
`rpi_orbbec_capture_server.py`. The Jetson runs
`arm_controlling remote_orbbec_capture`, which preserves the existing
`/orbbec_test_scan/capture_view` service used by `plant_view_scanner`.

## Raspberry Pi

Configure `eth0` as `10.20.0.200/24`, install the Orbbec ROS 2 wrapper,
`cv_bridge`, `message_filters`, OpenCV and PyYAML, then start the camera without
point-cloud publishing. Use the same camera settings currently found in
`bench_robot/launch/robot.launch.py`.

Synchronize the Pi and Jetson clocks with chrony/NTP. The Jetson uses the
depth-image timestamp to look up the arm transform, so unsynchronized clocks
will produce missing or incorrect capture poses.

```bash
export CEABOT_CAPTURE_TOKEN='replace-with-a-long-random-token'
source /opt/ros/$ROS_DISTRO/setup.bash
source ~/orbbec_ws/install/setup.bash
ros2 launch orbbec_camera gemini_330_series.launch.py \
  camera_name:=gemini336 depth_registration:=true align_mode:=SW \
  enable_point_cloud:=false enable_colored_point_cloud:=false
```

In a second terminal:

```bash
export CEABOT_CAPTURE_TOKEN='replace-with-the-same-token'
source /opt/ros/$ROS_DISTRO/setup.bash
source ~/orbbec_ws/install/setup.bash
python3 rpi_orbbec_capture_server.py --bind 10.20.0.200
```

Test from the Jetson:

```bash
curl http://10.20.0.200:8080/health
```

## Jetson

Build and source the CEAbot workspace, then run the proxy instead of
`bench_robot/orbbec_test_scan`:

```bash
colcon build --packages-select arm_controlling
source install/setup.bash
ros2 run arm_controlling remote_orbbec_capture --ros-args \
  -p rpi_url:=http://10.20.0.200:8080 \
  -p auth_token:="$CEABOT_CAPTURE_TOKEN"
```

The proxy saves each response to:

```text
<run_dir>/plant_<ID>/<view_label>/color.png
<run_dir>/plant_<ID>/<view_label>/depth.png
<run_dir>/plant_<ID>/<view_label>/meta.yaml
```

`depth.png` is lossless 16-bit depth. `meta.yaml` is installed last and is the
completion marker used by `plant_view_scanner`.

Disable both the Gemini 336 launch and `orbbec_test_scan` on the Jetson; those
now run on, or are replaced by, the Pi and the remote proxy. Existing
point-cloud reconstruction code will also need a separate RGB-D back-projection
update because this transport deliberately does not create `cloud.ply` or
`cloud_xyzrgb.npy`.
