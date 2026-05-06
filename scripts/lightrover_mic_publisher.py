#!/usr/bin/env python3
from __future__ import annotations

import argparse
from array import array
import subprocess
import threading

import rclpy
from rclpy.node import Node
from std_msgs.msg import UInt8MultiArray


class LightroverMicPublisher(Node):
    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__("lightrover_mic_publisher")
        self.publisher = self.create_publisher(UInt8MultiArray, args.topic, 20)
        self.chunk_bytes = int(args.rate * args.channels * 2 * args.chunk_ms / 1000)
        self.process = subprocess.Popen(
            [
                "arecord",
                "-D",
                args.device,
                "-q",
                "-f",
                "S16_LE",
                "-r",
                str(args.rate),
                "-c",
                str(args.channels),
                "-t",
                "raw",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._stderr_thread = threading.Thread(target=self._log_stderr, daemon=True)
        self._stderr_thread.start()
        self.timer = self.create_timer(max(args.chunk_ms / 1000 / 2, 0.005), self.publish_chunk)
        self.get_logger().info(
            f"publishing {args.device} as PCM s16le {args.rate}Hz {args.channels}ch to {args.topic}"
        )

    def _log_stderr(self) -> None:
        if not self.process.stderr:
            return
        for line in self.process.stderr:
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                self.get_logger().warning(text)

    def publish_chunk(self) -> None:
        if self.process.poll() is not None:
            self.get_logger().error(f"arecord exited with code {self.process.returncode}")
            self.destroy_timer(self.timer)
            return
        if not self.process.stdout:
            return
        data = self.process.stdout.read(self.chunk_bytes)
        if not data:
            return
        msg = UInt8MultiArray()
        msg.data = array("B", data)
        self.publisher.publish(msg)

    def destroy_node(self) -> bool:
        if self.process.poll() is None:
            self.process.terminate()
        return super().destroy_node()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish Lightrover microphone PCM audio as a ROS2 topic.")
    parser.add_argument("--topic", default="/lightrover/audio/pcm_s16le")
    parser.add_argument("--device", default="default", help="ALSA device, for example default or plughw:1,0")
    parser.add_argument("--rate", type=int, default=16000)
    parser.add_argument("--channels", type=int, default=1)
    parser.add_argument("--chunk-ms", type=int, default=20)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    rclpy.init()
    node = LightroverMicPublisher(args)
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
