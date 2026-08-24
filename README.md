# Gemini 336 capture spool over the Gen3 expansion Ethernet

The Pi captures and compresses every view locally. It does **not** transfer a
view during the scan. After `/individual_scan_done` is published, the Jetson
downloads all pending archives in resumable chunks and installs them in the
original scan folders. The next capture automatically pauses transfer; the next
row-complete signal resumes it.

Each installed view contains:

```text
<run_dir>/plant_<ID>/<view>/
├── color.png
├── depth.npy
├── cloud_xyzrgb.npy
└── meta.yaml
```

`depth.npy` preserves the original `uint16` depth values. The canonical point
cloud is `cloud_xyzrgb.npy`; create a PLY later only when an external viewer
needs one, avoiding duplicate storage and transfer during acquisition.

## Raspberry Pi

Use Ubuntu Server ARM64 and synchronize the Pi and Jetson clocks with
chrony/NTP. Configure `eth0` as `10.20.0.200/24` and install the Orbbec ROS 2
wrapper, `cv_bridge`, `message_filters`, OpenCV, NumPy and PyYAML.

The registered colored point-cloud topic is required:

```bash
source /opt/ros/$ROS_DISTRO/setup.bash
source ~/orbbec_ws/install/setup.bash

ros2 launch orbbec_camera gemini_330_series.launch.py \
  camera_name:=gemini336 \
  depth_registration:=true \
  align_mode:=SW \
  enable_point_cloud:=false \
  enable_colored_point_cloud:=true
```

Create a writable spool directory. An NVMe SSD is strongly preferred because
RGB-D point-cloud archives are large:

```bash
sudo mkdir -p /var/lib/ceabot-captures
sudo chown "$USER":"$USER" /var/lib/ceabot-captures
```

Start the server:

```bash
export CEABOT_CAPTURE_TOKEN='replace-with-a-long-random-token'
python3 /home/thiwa/CEAbot_Rpi/rpi_orbbec_capture_server.py \
  --bind 10.20.0.200 \
  --spool-dir /var/lib/ceabot-captures \
  --depth-scale-m-per-unit 0.001
```

Health check from the Jetson:

```bash
curl -H "Authorization: Bearer $CEABOT_CAPTURE_TOKEN" \
  http://10.20.0.200:8080/health
```

## Jetson

Disable the Jetson-side Gemini 336 launch and the old `orbbec_test_scan` node.
Build and run the proxy:

```bash
cd /home/thiwa/CEAbot
colcon build --packages-select arm_controlling
source install/setup.bash

export CEABOT_CAPTURE_TOKEN='replace-with-the-same-token'
ros2 run arm_controlling remote_orbbec_capture --ros-args \
  -p rpi_url:=http://10.20.0.200:8080 \
  -p auth_token:="$CEABOT_CAPTURE_TOKEN" \
  -p chunk_bytes:=1048576
```

`plant_view_scanner` continues using `/orbbec_test_scan/capture_view`. A capture
returns after the Pi has safely written its compressed archive. The Jetson
creates `meta.yaml` immediately so timestamped TF/pose metadata can still be
recorded; it merges that metadata when the full archive arrives.

The proxy automatically starts/resumes transfer when it receives
`/individual_scan_done`. Manual controls are also available:

```bash
ros2 service call /remote_orbbec_transfer/start std_srvs/srv/Trigger '{}'
ros2 service call /remote_orbbec_transfer/pause std_srvs/srv/Trigger '{}'
ros2 service call /remote_orbbec_transfer/status std_srvs/srv/Trigger '{}'
```

Pi archives are deleted only after the Jetson verifies the complete archive,
extracts all files, merges metadata and acknowledges successful installation.
If the connection fails, the partial archive remains on both machines and the
next start/row-complete command resumes it.
