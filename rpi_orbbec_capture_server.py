#!/usr/bin/env python3
"""Pi-side Gemini 336 capture spool with resumable batch downloads."""

import argparse
import hashlib
import hmac
import json
import os
import platform
import re
import signal
import socket
import tarfile
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np
import rclpy
import yaml
from cv_bridge import CvBridge
from message_filters import ApproximateTimeSynchronizer, Subscriber
from rclpy.node import Node
from rclpy.executors import ExternalShutdownException
from sensor_msgs.msg import CameraInfo, Image, PointCloud2

SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


class OrbbecFrameSource(Node):
    def __init__(self, camera_name="gemini336"):
        super().__init__("rpi_orbbec_capture_server")
        prefix = f"/{camera_name}"
        self.bridge = CvBridge()
        self.condition = threading.Condition()
        self.sequence = 0
        self.latest_set = None
        self.color_info = None
        self.depth_info = None
        self.create_subscription(CameraInfo, f"{prefix}/color/camera_info", self._color_info, 10)
        self.create_subscription(CameraInfo, f"{prefix}/depth/camera_info", self._depth_info, 10)
        color = Subscriber(self, Image, f"{prefix}/color/image_raw")
        depth = Subscriber(self, Image, f"{prefix}/depth/image_raw")
        cloud = Subscriber(self, PointCloud2, f"{prefix}/depth_registered/points")
        self.sync = ApproximateTimeSynchronizer([color, depth, cloud], queue_size=3, slop=0.15)
        self.sync.registerCallback(self._frames)

    def _color_info(self, message):
        self.color_info = message

    def _depth_info(self, message):
        self.depth_info = message

    def _frames(self, color, depth, cloud):
        with self.condition:
            self.sequence += 1
            self.latest_set = (self.sequence, color, depth, cloud, time.monotonic_ns())
            self.condition.notify_all()

    def capture_next(self, timeout):
        with self.condition:
            initial = self.sequence
            deadline = time.monotonic() + timeout
            while self.sequence <= initial:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("No synchronized RGB/depth/registered-cloud set received")
                self.condition.wait(remaining)
            _, color_msg, depth_msg, cloud_msg, received_ns = self.latest_set
        color = self.bridge.imgmsg_to_cv2(color_msg, desired_encoding="bgr8")
        depth = self.bridge.imgmsg_to_cv2(depth_msg, desired_encoding="passthrough")
        if depth.dtype != np.uint16:
            raise ValueError(f"Expected uint16 depth, received {depth.dtype}")
        return color, depth, color_msg, depth_msg, cloud_msg, received_ns


def stamp_dict(stamp):
    return {"sec": int(stamp.sec), "nanosec": int(stamp.nanosec)}


def camera_info_dict(message):
    if message is None:
        return None
    return {
        "frame_id": message.header.frame_id, "width": int(message.width),
        "height": int(message.height), "distortion_model": message.distortion_model,
        "d": [float(v) for v in message.d], "k": [float(v) for v in message.k],
        "r": [float(v) for v in message.r], "p": [float(v) for v in message.p],
    }


def pointcloud_xyzrgb(message):
    fields = {field.name: field for field in message.fields}
    if not all(name in fields for name in ("x", "y", "z")):
        raise ValueError("Point cloud is missing x, y, or z")
    rgb_name = "rgb" if "rgb" in fields else "rgba" if "rgba" in fields else None
    if rgb_name is None:
        raise ValueError("Registered point cloud has no rgb/rgba field")
    endian = ">" if message.is_bigendian else "<"
    names = ["x", "y", "z", rgb_name]
    dtype = np.dtype({
        "names": names,
        "formats": [endian + "f4", endian + "f4", endian + "f4", endian + "u4"],
        "offsets": [fields[name].offset for name in names],
        "itemsize": message.point_step,
    })
    points = np.frombuffer(message.data, dtype=dtype, count=message.width * message.height)
    valid = np.isfinite(points["x"]) & np.isfinite(points["y"]) & np.isfinite(points["z"])
    points = points[valid]
    packed = points[rgb_name].astype(np.uint32, copy=False)
    red = ((packed >> 16) & 0xFF).astype(np.float32)
    green = ((packed >> 8) & 0xFF).astype(np.float32)
    blue = (packed & 0xFF).astype(np.float32)
    return np.column_stack((points["x"], points["y"], points["z"], red, green, blue)).astype(np.float32, copy=False)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json_atomic(path, value):
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as output:
        json.dump(value, output, indent=2)
    os.replace(temporary, path)


def build_spooled_capture(server, request):
    run_id, view_label = str(request["run_id"]), str(request["view_label"])
    plant_id = int(request["plant_id"])
    if not SAFE_ID.fullmatch(run_id) or not SAFE_ID.fullmatch(view_label):
        raise ValueError("run_id/view_label contains unsupported characters")
    if plant_id < 0:
        raise ValueError("plant_id must be non-negative")
    color, depth, color_msg, depth_msg, cloud_msg, received_ns = server.frame_source.capture_next(server.capture_timeout)
    xyzrgb = pointcloud_xyzrgb(cloud_msg)
    archive_id = uuid.uuid4().hex
    run_spool = os.path.join(server.spool_dir, run_id)
    os.makedirs(run_spool, exist_ok=True)
    final_archive = os.path.join(run_spool, f"{archive_id}.tar.gz")
    index_path = os.path.join(run_spool, f"{archive_id}.json")

    with tempfile.TemporaryDirectory(prefix="capture_", dir=run_spool) as directory:
        paths = {name: os.path.join(directory, name) for name in (
            "color.png", "depth.npy", "cloud_xyzrgb.npy", "meta.yaml"
        )}
        if not cv2.imwrite(paths["color.png"], color, [cv2.IMWRITE_PNG_COMPRESSION, 3]):
            raise IOError("Failed to encode color.png")
        np.save(paths["depth.npy"], depth)
        np.save(paths["cloud_xyzrgb.npy"], xyzrgb)
        metadata = {
            "schema_version": 2, "run_id": run_id, "archive_id": archive_id,
            "plant_id": plant_id, "view_label": view_label,
            "capture_utc": datetime.now(timezone.utc).isoformat(),
            "received_monotonic_ns": int(received_ns),
            "color_timestamp": stamp_dict(color_msg.header.stamp),
            "depth_timestamp": stamp_dict(depth_msg.header.stamp),
            "cloud_timestamp": stamp_dict(cloud_msg.header.stamp),
            "frame_id": cloud_msg.header.frame_id,
            "color_frame_id": color_msg.header.frame_id,
            "depth_frame_id": depth_msg.header.frame_id,
            "color_encoding": color_msg.encoding, "depth_encoding": depth_msg.encoding,
            "color_shape": list(color.shape), "depth_shape": list(depth.shape),
            "depth_dtype": str(depth.dtype),
            "depth_scale_m_per_unit": float(server.depth_scale_m),
            "cloud_xyzrgb_point_count": int(len(xyzrgb)),
            "color_camera_info": camera_info_dict(server.frame_source.color_info),
            "depth_camera_info": camera_info_dict(server.frame_source.depth_info),
            "host": socket.gethostname(), "platform": platform.platform(),
        }
        metadata["files"] = {
            name: {"bytes": os.path.getsize(path), "sha256": sha256_file(path)}
            for name, path in paths.items() if name != "meta.yaml"
        }
        with open(paths["meta.yaml"], "w", encoding="utf-8") as output:
            yaml.safe_dump(metadata, output, sort_keys=False)
        temporary_archive = final_archive + ".tmp"
        with tarfile.open(temporary_archive, mode="w:gz", compresslevel=3) as tar:
            for name, path in paths.items():
                tar.add(path, arcname=name, recursive=False)
        os.replace(temporary_archive, final_archive)

    index = {
        "archive_id": archive_id, "run_id": run_id, "plant_id": plant_id,
        "view_label": view_label, "bytes": os.path.getsize(final_archive),
        "sha256": sha256_file(final_archive), "metadata": metadata,
    }
    write_json_atomic(index_path, index)
    return index


class CaptureHandler(BaseHTTPRequestHandler):
    server_version = "CEAbotRpiCapture/2.0"

    def _authorized(self):
        return not self.server.auth_token or hmac.compare_digest(
            self.headers.get("Authorization", ""), f"Bearer {self.server.auth_token}"
        )

    def do_GET(self):
        if not self._authorized():
            self.send_error(401, "Invalid bearer token"); return
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/health":
                self._json_reply(200, {"ok": True, "frames_received": self.server.frame_source.sequence}); return
            query = parse_qs(parsed.query)
            if parsed.path == "/manifest":
                self._json_reply(200, {"archives": self._manifest(query["run_id"][0])}); return
            if parsed.path == "/archive/chunk":
                self._send_chunk(query["run_id"][0], query["archive_id"][0], int(query.get("offset", ["0"])[0]), int(query.get("limit", [str(1024 * 1024)])[0])); return
            self.send_error(404)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            self.send_error(400, str(exc))

    def do_POST(self):
        if not self._authorized():
            self.send_error(401, "Invalid bearer token"); return
        try:
            request = self._read_json()
            if self.path == "/capture":
                with self.server.capture_lock:
                    result = build_spooled_capture(self.server, request)
                self._json_reply(200, result); return
            if self.path == "/archive/complete":
                self._complete(str(request["run_id"]), str(request["archive_id"]))
                self._json_reply(200, {"success": True}); return
            self.send_error(404)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self.send_error(400, str(exc))
        except TimeoutError as exc:
            self.send_error(504, str(exc))
        except Exception as exc:
            self.server.frame_source.get_logger().error(f"Request failed: {exc}")
            self.send_error(500, str(exc))

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 8192:
            raise ValueError("Invalid request length")
        return json.loads(self.rfile.read(length))

    def _paths(self, run_id, archive_id):
        if not SAFE_ID.fullmatch(run_id) or not SAFE_ID.fullmatch(archive_id):
            raise ValueError("Invalid archive identifier")
        base = os.path.join(self.server.spool_dir, run_id, archive_id)
        return base + ".tar.gz", base + ".json"

    def _manifest(self, run_id):
        if not SAFE_ID.fullmatch(run_id):
            raise ValueError("Invalid run_id")
        directory = os.path.join(self.server.spool_dir, run_id)
        if not os.path.isdir(directory):
            return []
        entries = []
        for name in sorted(os.listdir(directory)):
            if name.endswith(".json"):
                with open(os.path.join(directory, name), "r", encoding="utf-8") as source:
                    entries.append(json.load(source))
        return sorted(entries, key=lambda entry: entry["metadata"]["capture_utc"])

    def _send_chunk(self, run_id, archive_id, offset, limit):
        archive_path, index_path = self._paths(run_id, archive_id)
        if not os.path.isfile(archive_path) or not os.path.isfile(index_path):
            self.send_error(404, "Archive not found"); return
        if offset < 0 or limit <= 0 or limit > 4 * 1024 * 1024:
            raise ValueError("Invalid chunk range")
        total = os.path.getsize(archive_path)
        if offset > total:
            raise ValueError("Offset exceeds archive size")
        with open(archive_path, "rb") as source:
            source.seek(offset); body = source.read(limit)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Archive-Size", str(total))
        self.send_header("X-Chunk-Offset", str(offset))
        self.end_headers(); self.wfile.write(body)

    def _complete(self, run_id, archive_id):
        archive_path, index_path = self._paths(run_id, archive_id)
        for path in (archive_path, index_path):
            if os.path.exists(path):
                os.unlink(path)

    def _json_reply(self, status, value):
        body = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def log_message(self, format_string, *args):
        self.server.frame_source.get_logger().info(format_string % args)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind", default="10.20.0.200")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--camera-name", default="gemini336")
    parser.add_argument("--capture-timeout", type=float, default=15.0)
    parser.add_argument("--spool-dir", default="/var/lib/ceabot-captures")
    parser.add_argument("--depth-scale-m-per-unit", type=float, default=0.001)
    parser.add_argument("--token", default=os.environ.get("CEABOT_CAPTURE_TOKEN", ""))
    return parser.parse_args()


def main():
    args = parse_args(); os.makedirs(args.spool_dir, exist_ok=True); rclpy.init()
    source = OrbbecFrameSource(args.camera_name)
    def spin_ros():
        try:
            rclpy.spin(source)
        except ExternalShutdownException:
            pass

    ros_thread = threading.Thread(target=spin_ros, daemon=True); ros_thread.start()
    server = ThreadingHTTPServer((args.bind, args.port), CaptureHandler)
    server.frame_source, server.capture_timeout = source, args.capture_timeout
    server.capture_lock = threading.Lock()
    server.depth_scale_m, server.spool_dir = args.depth_scale_m_per_unit, os.path.abspath(args.spool_dir)
    server.auth_token = args.token
    source.get_logger().info(f"Listening on http://{args.bind}:{args.port}; spool={server.spool_dir}")
    stop_requested = threading.Event()

    def request_stop(signum, frame):
        stop_requested.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    server.timeout = 0.5
    try:
        while not stop_requested.is_set():
            server.handle_request()
    finally:
        server.server_close()
        if rclpy.ok():
            rclpy.shutdown()
        ros_thread.join(timeout=2.0)
        source.destroy_node()


if __name__ == "__main__":
    main()
