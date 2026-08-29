# CEAbot Raspberry Pi Gemini 336 capture system

This repository configures a Raspberry Pi 5 as the capture computer for an
Orbbec Gemini 305/336 mounted on the Kinova Gen3 end effector. The Pi captures and
compresses each view locally; the Jetson communicates with it over the Kinova
expansion Ethernet.

After capture and compression, the Jetson downloads the archive in a background
thread while the arm moves to its next view. A new capture pauses an unfinished
download at its current chunk and transfer resumes afterward.
`/individual_scan_done` remains a final resume signal.

```text
<run_dir>/plant_<ID>/<view>/
├── color.png
├── depth.npy
├── cloud_xyzrgb.npy
└── meta.yaml
```

## 1. Install Ubuntu and enable remote access

The tested installation started with **Ubuntu 24.04 LTS Desktop ARM64** on the
Pi 5. Connect it to Wi-Fi during first-boot setup, then install and enable SSH.

Ubuntu Desktop does not need to be reinstalled to run headlessly. After the
complete system is tested, optionally boot to server/console mode:

```bash
sudo systemctl set-default multi-user.target
sudo reboot
```

SSH and the boot services continue working. Restore desktop boot with:

```bash
sudo systemctl set-default graphical.target
sudo reboot
```

## 2. Install ROS 2 Jazzy

Expected: `jazzy`. See the official
[ROS 2 Jazzy Ubuntu installation](https://docs.ros.org/en/jazzy/Installation/Ubuntu-Install-Debs.html).

## 3. Install the capture-server dependencies

```bash
sudo apt update
sudo apt install -y \
  ros-jazzy-cv-bridge \
  ros-jazzy-message-filters \
  python3-opencv \
  python3-numpy \
  python3-yaml
```

## 4. Build the Orbbec wrapper

Gemini 336 belongs to the Gemini 330 series and uses
`gemini_330_series.launch.py`. Use Orbbec's `v2-main` branch.

```bash
source /opt/ros/jazzy/setup.bash
sudo apt update
sudo apt install -y \
  git libgflags-dev nlohmann-json3-dev libdw-dev libssl-dev \
  mesa-utils libgl1 libgoogle-glog-dev \
  ros-jazzy-image-transport ros-jazzy-image-transport-plugins \
  ros-jazzy-compressed-image-transport ros-jazzy-image-publisher \
  ros-jazzy-camera-info-manager ros-jazzy-diagnostic-updater \
  ros-jazzy-diagnostic-msgs ros-jazzy-statistics-msgs \
  ros-jazzy-xacro ros-jazzy-backward-ros

mkdir -p /home/thiwa/orbbec_ws/src
cd /home/thiwa/orbbec_ws/src
git clone --branch v2-main --single-branch \
  https://github.com/orbbec/OrbbecSDK_ROS2.git

cd /home/thiwa/orbbec_ws
colcon build --event-handlers console_direct+ \
  --cmake-args -DCMAKE_BUILD_TYPE=Release
```

For an upgrade, first stop the services and keep the old workspace as a backup:

```bash
sudo systemctl stop ceabot-capture.service orbbec-camera.service
cd /home/thiwa
mv orbbec_ws orbbec_ws_backup
mkdir -p orbbec_ws/src
```

Install the mandatory udev rules:

```bash
cd /home/thiwa/orbbec_ws/src/OrbbecSDK_ROS2/orbbec_camera/scripts
sudo bash install_udev_rules.sh
sudo udevadm control --reload-rules
sudo udevadm trigger
```

Disconnect and reconnect the camera, then verify:

```bash
source /opt/ros/jazzy/setup.bash
source /home/thiwa/orbbec_ws/install/setup.bash
ros2 pkg prefix orbbec_camera
ros2 run orbbec_camera list_devices_node
```

The prefix must be under `/home/thiwa/orbbec_ws/install`. See Orbbec's official
[ROS 2 wrapper instructions](https://github.com/orbbec/OrbbecSDK_ROS2/tree/v2-main).

## 5. Automatically source Jazzy and the Orbbec workspace

Add it to `.bashrc` once:

```bash
grep -qxF 'source /home/thiwa/CEAbot_Rpi/systemd_boot_setup/ros_jazzy.bash' \
  ~/.bashrc || \
  echo 'source /home/thiwa/CEAbot_Rpi/systemd_boot_setup/ros_jazzy.bash' \
  >> ~/.bashrc

source ~/.bashrc
echo "$ROS_DISTRO"
ros2 pkg prefix orbbec_camera
```

Equivalently, `.bashrc` can contain the two sources directly:

```bash
source /opt/ros/jazzy/setup.bash
if [ -r /home/thiwa/orbbec_ws/install/setup.bash ]; then
  source /home/thiwa/orbbec_ws/install/setup.bash
fi
```

Use only one method. The systemd services source the helper independently and
do not depend on `.bashrc`.

## 6. Install the project and token

Place this repository at `/home/thiwa/CEAbot_Rpi`, then create the spool:

```bash
sudo mkdir -p /var/lib/ceabot-captures
sudo chown thiwa:thiwa /var/lib/ceabot-captures
sudo chmod 750 /var/lib/ceabot-captures
```

Generate one token on the Pi:

```bash
openssl rand -hex 32
sudo nano /etc/ceabot-capture.env
```

Add the generated value:

```text
CEABOT_CAPTURE_TOKEN=replace_with_the_generated_token
```

```bash
sudo chown root:root /etc/ceabot-capture.env
sudo chmod 600 /etc/ceabot-capture.env
```

Store the same token on the Jetson:

```bash
mkdir -p /home/thiwa/.config/ceabot
chmod 700 /home/thiwa/.config/ceabot
nano /home/thiwa/.config/ceabot/capture.env
chmod 600 /home/thiwa/.config/ceabot/capture.env
```

The Jetson file contains:

```text
CEABOT_CAPTURE_TOKEN=replace_with_the_same_generated_token
```

Automatically export the token in every new interactive Jetson Bash terminal.
Open the Jetson shell configuration:

```bash
nano /home/thiwa/.bashrc
```

Add this block once:

```bash
if [ -r /home/thiwa/.config/ceabot/capture.env ]; then
    set -a
    source /home/thiwa/.config/ceabot/capture.env
    set +a
fi
```

Apply it to the current terminal and verify without printing the secret:

```bash
source /home/thiwa/.bashrc

if [ -n "$CEABOT_CAPTURE_TOKEN" ]; then
    echo "Capture token loaded"
else
    echo "Capture token missing"
fi
```

After this, the token does not need to be sourced manually in each new Bash
terminal. The equivalent one-session fallback is:

```bash
set -a
source /home/thiwa/.config/ceabot/capture.env
set +a
```

Systemd does not read `.bashrc`. If the Jetson robot launch is later installed
as a systemd service, add this to that service's `[Service]` section:

```ini
EnvironmentFile=/home/thiwa/.config/ceabot/capture.env
```

## 7. Verified physical wiring

The installed 20-pin FFC reverses its pin order:

```text
Kinova pin 1  -> breakout pin 20
Kinova pin 20 -> breakout pin 1
breakout pin = 21 - Kinova pin
```

For a T568B cable whose RJ45 end plugs into the Pi:

| RJ45 pin | Cable color  | Kinova signal | Kinova pin | Breakout pin |
| -------: | ------------ | ------------- | ---------: | -----------: |
|        1 | white/orange | ETH_RX_P      |          5 |           16 |
|        2 | orange       | ETH_RX_N      |          6 |           15 |
|        3 | white/green  | ETH_TX_P      |          8 |           13 |
|        6 | green        | ETH_TX_N      |          9 |           12 |

Insulate the unused blue and brown pairs. Power the Pi independently; do not
connect Kinova +24 V to it. Verify mapping and absence of shorts with power off.

## 8. Configure persistent networking with NetworkManager

Use NetworkManager as the sole owner of these interfaces. Do not also enable
the legacy route services, because competing owners caused unreliable reboot
behavior.

```text
Jetson:          192.168.1.2   (enP1p1s0)
Kinova external: 192.168.1.10
Kinova EXP:      10.20.0.1
Pi:              10.20.0.200   (eth0)
```

### Pi

Find its connection name:

```bash
nmcli -g GENERAL.CONNECTION device show eth0
```

For the tested `netplan-eth0` connection:

```bash
sudo nmcli connection modify netplan-eth0 \
  ipv4.method manual \
  ipv4.addresses 10.20.0.200/24 \
  ipv4.gateway "" \
  ipv4.routes "192.168.1.0/24 10.20.0.1" \
  ipv4.never-default yes \
  ipv6.method link-local \
  802-3-ethernet.auto-negotiate no \
  802-3-ethernet.speed 100 \
  802-3-ethernet.duplex full \
  connection.autoconnect yes

sudo nmcli connection up netplan-eth0
```

Substitute the actual name if different. Verify:

```bash
ip -brief address show eth0
ip route get 192.168.1.2
sudo ethtool eth0 | grep -E 'Speed|Duplex|Auto-negotiation|Link detected'
ping -c 4 10.20.0.1
```

### Jetson

Find the profile:

```bash
nmcli -g GENERAL.CONNECTION device show enP1p1s0
```

Modify it, substituting its exact name:

```bash
sudo nmcli connection modify "Wired connection 1" \
  ipv4.method manual \
  ipv4.addresses 192.168.1.2/24 \
  ipv4.gateway "" \
  ipv4.routes "10.20.0.0/24 192.168.1.10" \
  ipv4.never-default yes \
  ipv6.method link-local \
  connection.autoconnect yes

sudo nmcli connection up "Wired connection 1"
```

If there is no profile, create one:

```bash
sudo nmcli connection add type ethernet ifname enP1p1s0 \
  con-name ceabot-kinova \
  ipv4.method manual ipv4.addresses 192.168.1.2/24 \
  ipv4.routes "10.20.0.0/24 192.168.1.10" \
  ipv4.never-default yes ipv6.method link-local \
  connection.autoconnect yes
sudo nmcli connection up ceabot-kinova
```

Verify:

```bash
ip route get 10.20.0.200
ping -c 4 192.168.1.10
ping -c 4 10.20.0.200
```

## 9. Install and enable the Pi services

```bash
sudo systemctl stop ceabot-capture.service orbbec-camera.service
cd /home/thiwa/CEAbot_Rpi/systemd_boot_setup
sudo cp orbbec-camera.service ceabot-capture.service /etc/systemd/system/
sudo chmod 644 /etc/systemd/system/orbbec-camera.service \
  /etc/systemd/system/ceabot-capture.service
sudo systemctl daemon-reload
sudo systemctl reset-failed orbbec-camera.service ceabot-capture.service
sudo systemctl enable orbbec-camera.service ceabot-capture.service
sudo systemctl start orbbec-camera.service
sleep 15
sudo systemctl start ceabot-capture.service
```

Verify:

```bash
systemctl --no-pager --full status orbbec-camera.service ceabot-capture.service
journalctl -u orbbec-camera.service -n 100 --no-pager
journalctl -u ceabot-capture.service -n 100 --no-pager
ros2 node list
ros2 topic list | grep /gemini336
timeout 10 ros2 topic hz /gemini336/depth/image_raw
```

Do not run a foreground Orbbec launch while its service is active. Two drivers
cause `uvc_open failed ... Return Code: -6`.

Authenticated local health test:

```bash
sudo bash -c 'set -a; source /etc/ceabot-capture.env; curl -H "Authorization: Bearer $CEABOT_CAPTURE_TOKEN" http://10.20.0.200:8080/health'
```

## 10. Jetson proxy and end-to-end test

```bash
cd /home/thiwa/CEAbot
colcon build --packages-select arm_controlling
source /opt/ros/humble/setup.bash
source install/setup.bash
set -a
source /home/thiwa/.config/ceabot/capture.env
set +a

ros2 run arm_controlling remote_orbbec_capture --ros-args \
  -p rpi_url:="http://10.20.0.200:8080" \
  -p auth_token:="$CEABOT_CAPTURE_TOKEN"
```

Do not paste a Markdown URL such as `[http://...](http://...)` into Bash.

In a second sourced Jetson terminal:

```bash
ros2 service call /orbbec_test_scan/capture_view \
  arm_interfaces/srv/CaptureView \
  "{run_dir: '/home/thiwa/scan_data/stationary_rpi_test', plant_id: 1, view_label: 'stationary_01'}"
```

Transfer starts automatically after compression, while the arm is free to move.
Manual controls and the row-complete fallback remain available:

```bash
ros2 service call /remote_orbbec_transfer/start std_srvs/srv/Trigger '{}'
ros2 service call /remote_orbbec_transfer/pause std_srvs/srv/Trigger '{}'
ros2 service call /remote_orbbec_transfer/status std_srvs/srv/Trigger '{}'
```

Verify:

```bash
ls -lh /home/thiwa/scan_data/stationary_rpi_test/plant_01/stationary_01
grep remote_archive_pending \
  /home/thiwa/scan_data/stationary_rpi_test/plant_01/stationary_01/meta.yaml
```

Expected: `color.png`, `depth.npy`, `cloud_xyzrgb.npy`, `meta.yaml`, and
`remote_archive_pending: false`.

## 11. Updating and troubleshooting

Reinstall edited service files with:

```bash
cd /home/thiwa/CEAbot_Rpi/systemd_boot_setup
sudo systemctl stop ceabot-capture.service orbbec-camera.service
sudo cp orbbec-camera.service ceabot-capture.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start orbbec-camera.service
sleep 15
sudo systemctl start ceabot-capture.service
```

Useful diagnostics:

```bash
lsusb | grep -i -E 'orbbec|2bc5'
lsusb -t
sudo journalctl -u orbbec-camera.service -b --no-pager -l
sudo journalctl -u ceabot-capture.service -b --no-pager -l
sudo ss -ltnp | grep 8080
```

If topics exist but publish no frames:

```bash
sudo systemctl stop ceabot-capture.service
sudo systemctl restart orbbec-camera.service
sleep 15
timeout 10 ros2 topic hz /gemini336/depth/image_raw
sudo systemctl start ceabot-capture.service
```
