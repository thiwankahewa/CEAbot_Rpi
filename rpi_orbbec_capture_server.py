#!/usr/bin/env python3
"""Raspberry Pi HTTP capture server using the Orbbec ROS 2 topics.

The Orbbec ROS driver must already be running. Each POST /capture waits for a
new synchronized RGB/depth pair and returns color.png, depth.png and meta.yaml
as a gzip-compressed tar archive.
"""

import argparse
import hashlib
import hmac
import io
import json
import os
import platform
import socket
import tarfile
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np
import rclpy
import yaml
from cv_bridge import CvBridge
from message_filters import ApproximateTimeSynchronizer, Subscriber
from rclpy.node import Node
from sensor_msgs.msg import CameraInfo, Image


class OrbbecFrameSource(Node):
    def __init__(self, camera_name="gemini336"):
        super().__init__("rpi_orbbec_capture_server")
        prefix = f"/{camera_name}"
        self.bridge = CvBridge()
        self.condition = threading.Condition()
        self.sequence = 0
        self.latest_pair = None
        self.color_info = None
        self.depth_info = None
        self.create_subscription(
            CameraInfo, f"{prefix}/color/camera_info", self._color_info, 10
        )
        self.create_subscription(
            CameraInfo, f"{prefix}/depth/camera_info", self._depth_info, 10
        )
        color = Subscriber(self, Image, f"{prefix}/color/image_raw")
        depth = Subscriber(self, Image, f"{prefix}/depth/image_raw")
        self.sync = ApproximateTimeSynchronizer(
            [color, depth], queue_size=20, slop=0.08
        )
        self.sync.registerCallback(self._frames)

    def _color_info(self, message):
        self.color_info = message

    def _depth_info(self, message):
        self.depth_info = message

    def _frames(self, color, depth):
        with self.condition:
            self.sequence += 1
            self.latest_pair = (self.sequence, color, depth, time.monotonic_ns())
            self.condition.notify_all()

    def capture_next(self, timeout):
        with self.condition:
            initial_sequence = self.sequence
            deadline = time.monotonic() + timeout
            while self.sequence <= initial_sequence:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("No synchronized RGB/depth pair received")
                self.condition.wait(remaining)
            _, color_msg, depth_msg, received_monotonic_ns = self.latest_pair

        color = self.bridge.imgmsg_to_cv2(color_msg, desired_encoding="bgr8")
        depth = self.bridge.imgmsg_to_cv2(depth_msg, desired_encoding="passthrough")
        if depth.dtype != np.uint16:
            raise ValueError(f"Expected uint16 depth, received {depth.dtype}")
        return color, depth, color_msg, depth_msg, received_monotonic_ns


def stamp_dict(stamp):
    return {"sec": int(stamp.sec), "nanosec": int(stamp.nanosec)}


def camera_info_dict(message):
    if message is None:
        return None
    return {
        "frame_id": message.header.frame_id,
        "width": int(message.width),
        "height": int(message.height),
        "distortion_model": message.distortion_model,
        "d": [float(value) for value in message.d],
        "k": [float(value) for value in message.k],
        "r": [float(value) for value in message.r],
        "p": [float(value) for value in message.p],
    }


def build_archive(source, plant_id, view_label, timeout, depth_scale_m):
    color, depth, color_msg, depth_msg, received_ns = source.capture_next(timeout)
    with tempfile.TemporaryDirectory(prefix="orbbec_capture_") as directory:
        color_path = os.path.join(directory, "color.png")
        depth_path = os.path.join(directory, "depth.png")
        meta_path = os.path.join(directory, "meta.yaml")
        if not cv2.imwrite(color_path, color, [cv2.IMWRITE_PNG_COMPRESSION, 3]):
            raise IOError("Failed to encode color.png")
        if not cv2.imwrite(depth_path, depth, [cv2.IMWRITE_PNG_COMPRESSION, 5]):
            raise IOError("Failed to encode depth.png")

        # cloud_timestamp is retained as a compatibility timestamp for the
        # existing Jetson TF lookup. It is the depth-image timestamp; no point
        # cloud is captured or transferred.
        metadata = {
            "schema_version": 1,
            "plant_id": int(plant_id),
            "view_label": str(view_label),
            "capture_utc": datetime.now(timezone.utc).isoformat(),
            "received_monotonic_ns": int(received_ns),
            "color_timestamp": stamp_dict(color_msg.header.stamp),
            "depth_timestamp": stamp_dict(depth_msg.header.stamp),
            "cloud_timestamp": stamp_dict(depth_msg.header.stamp),
            "frame_id": depth_msg.header.frame_id,
            "color_frame_id": color_msg.header.frame_id,
            "depth_frame_id": depth_msg.header.frame_id,
            "color_encoding": color_msg.encoding,
            "depth_encoding": depth_msg.encoding,
            "color_shape": list(color.shape),
            "depth_shape": list(depth.shape),
            "depth_dtype": str(depth.dtype),
            "depth_scale_m_per_unit": float(depth_scale_m),
            "color_camera_info": camera_info_dict(source.color_info),
            "depth_camera_info": camera_info_dict(source.depth_info),
            "host": socket.gethostname(),
            "platform": platform.platform(),
        }
        with open(meta_path, "w", encoding="utf-8") as output:
            yaml.safe_dump(metadata, output, sort_keys=False)

        metadata["files"] = {}
        for name in ("color.png", "depth.png"):
            path = os.path.join(directory, name)
            with open(path, "rb") as data:
                metadata["files"][name] = {
                    "bytes": os.path.getsize(path),
                    "sha256": hashlib.sha256(data.read()).hexdigest(),
                }
        with open(meta_path, "w", encoding="utf-8") as output:
            yaml.safe_dump(metadata, output, sort_keys=False)

        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz", compresslevel=3) as tar:
            for name in ("color.png", "depth.png", "meta.yaml"):
                tar.add(os.path.join(directory, name), arcname=name, recursive=False)
        return archive.getvalue()


class CaptureHandler(BaseHTTPRequestHandler):
    server_version = "CEAbotRpiCapture/1.0"

    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return
        body = json.dumps(
            {"ok": True, "frames_received": self.server.frame_source.sequence}
        ).encode("utf-8")
        self._reply(200, "application/json", body)

    def do_POST(self):
        if self.path != "/capture":
            self.send_error(404)
            return
        if self.server.auth_token:
            supplied = self.headers.get("Authorization", "")
            expected = f"Bearer {self.server.auth_token}"
            if not hmac.compare_digest(supplied, expected):
                self.send_error(401, "Invalid bearer token")
                return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 4096:
                raise ValueError("Invalid request length")
            request = json.loads(self.rfile.read(length))
            plant_id = int(request["plant_id"])
            view_label = str(request["view_label"])
            if not view_label or len(view_label) > 128:
                raise ValueError("Invalid view_label")
            archive = build_archive(
                self.server.frame_source,
                plant_id,
                view_label,
                self.server.capture_timeout,
                self.server.depth_scale_m,
            )
            self._reply(200, "application/gzip", archive)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self.send_error(400, str(exc))
        except TimeoutError as exc:
            self.send_error(504, str(exc))
        except Exception as exc:
            self.server.frame_source.get_logger().error(
                f"Capture request failed: {exc}"
            )
            self.send_error(500, str(exc))

    def _reply(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string, *args):
        self.server.frame_source.get_logger().info(format_string % args)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind", default="10.20.0.200")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--camera-name", default="gemini336")
    parser.add_argument("--capture-timeout", type=float, default=10.0)
    parser.add_argument(
        "--depth-scale-m-per-unit",
        type=float,
        default=0.001,
        help="Gemini depth conversion factor; verify against the active SDK profile",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("CEABOT_CAPTURE_TOKEN", ""),
        help="Bearer token; defaults to CEABOT_CAPTURE_TOKEN",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    rclpy.init()
    source = OrbbecFrameSource(args.camera_name)
    ros_thread = threading.Thread(target=rclpy.spin, args=(source,), daemon=True)
    ros_thread.start()
    server = ThreadingHTTPServer((args.bind, args.port), CaptureHandler)
    server.frame_source = source
    server.capture_timeout = args.capture_timeout
    server.depth_scale_m = args.depth_scale_m_per_unit
    server.auth_token = args.token
    source.get_logger().info(f"Listening on http://{args.bind}:{args.port}")
    try:
        server.serve_forever()
    finally:
        server.server_close()
        source.destroy_node()
        rclpy.shutdown()
        ros_thread.join(timeout=2.0)


if __name__ == "__main__":
    main()
