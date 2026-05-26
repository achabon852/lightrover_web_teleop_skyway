#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.config_loader import RobotConfig, load_config


JOIN_ROOM_SERVICE = "/join_room"
PUBLISH_IMAGE_SERVICE = "/publish_video_stream_to_skyway_by_image"
ROS_DISCOVERY_SPIN_SEC = "2.0"


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def run_checked(args: Sequence[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(args), flush=True)
    return subprocess.run(
        list(args),
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )


def run_output(args: Sequence[str], *, timeout: float | None = None) -> str:
    completed = run_checked(args, timeout=timeout)
    if completed.stdout:
        print(completed.stdout, end="" if completed.stdout.endswith("\n") else "\n", flush=True)
    return completed.stdout


def select_robot(config_path: Path, robot_id: str | None) -> RobotConfig:
    robots = load_config(config_path)
    if robot_id is None:
        return robots[0]
    for robot in robots:
        if robot.id == robot_id:
            return robot
    available = ", ".join(robot.id for robot in robots)
    raise SystemExit(f"unknown robot_id: {robot_id}. available: {available}")


def skyway_room_for(robot: RobotConfig) -> str:
    explicit_room = getattr(robot, "skyway_room", None)
    if explicit_room:
        return explicit_room
    room_prefix = os.environ.get("SKYWAY_ROOM_PREFIX", "lightrover")
    return f"{room_prefix}-{robot.id}"


def wait_for_service(service_name: str, timeout_sec: float) -> None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            output = subprocess.run(
                ["ros2", "service", "list", "--no-daemon", "--spin-time", ROS_DISCOVERY_SPIN_SEC],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=5,
            ).stdout
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            time.sleep(1.0)
            continue
        if service_name in output.splitlines():
            return
        time.sleep(1.0)
    raise TimeoutError(f"service not available: {service_name}")


def topic_type(topic_name: str) -> str:
    output = run_output(
        ["ros2", "topic", "type", "--no-daemon", "--spin-time", ROS_DISCOVERY_SPIN_SEC, topic_name],
        timeout=8,
    ).strip()
    if not output:
        raise RuntimeError(f"topic has no type: {topic_name}")
    for line in reversed(output.splitlines()):
        candidate = line.strip()
        if "/msg/" in candidate:
            return candidate
    raise RuntimeError(f"topic has no ROS message type in output for {topic_name}: {output}")


def bool_for_image_topic(robot: RobotConfig, topic_name: str) -> bool:
    explicit = getattr(robot, "skyway_bridge_image_compressed", None)
    if explicit is not None:
        return bool(explicit)
    if topic_name.endswith("/compressed") or getattr(robot, "image_type", "compressed") == "compressed":
        return True
    return False


def validate_image_topic(topic_name: str, is_compressed: bool) -> None:
    expected = "sensor_msgs/msg/CompressedImage" if is_compressed else "sensor_msgs/msg/Image"
    actual = topic_type(topic_name)
    if actual != expected:
        raise RuntimeError(
            f"{topic_name} type mismatch: expected {expected}, got {actual}. "
            "Check config/robots.yaml skyway_bridge_image_topic and skyway_bridge_image_compressed."
        )


def service_call(service_name: str, service_type: str, request: str) -> None:
    output = run_output(["ros2", "service", "call", service_name, service_type, request], timeout=30)
    lowered = output.lower()
    if "success=false" in lowered or "successful: false" in lowered:
        raise RuntimeError(f"service returned failure: {service_name}")


def start_bridge_process() -> subprocess.Popen[str]:
    print("+ ros2 run skyway_ros_bridge skyway", flush=True)
    return subprocess.Popen(
        ["ros2", "run", "skyway_ros_bridge", "skyway"],
        text=True,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Start and configure skyway_ros_bridge for Lightrover V9 on the PC side.",
    )
    parser.add_argument("--robot-id", default=None, help="Robot id from config/robots.yaml. Defaults to the first robot.")
    parser.add_argument("--config", default=str(ROOT / "config" / "robots.yaml"), help="Path to robots.yaml.")
    parser.add_argument("--env-file", default=str(ROOT / ".env"), help="Path to SkyWay .env file.")
    parser.add_argument("--start-bridge", action="store_true", help="Start `ros2 run skyway_ros_bridge skyway` before configuring it.")
    parser.add_argument("--wait-sec", type=float, default=30.0, help="Timeout while waiting for skyway_ros_bridge services.")
    parser.add_argument("--keep-alive", action="store_true", help="Keep this process alive while the bridge process runs.")
    parser.add_argument("--skip-topic-check", action="store_true", help="Skip ROS image topic type validation.")
    args = parser.parse_args()

    from server.skyway_token import create_skyway_token

    load_env_file(Path(args.env_file))
    robot = select_robot(Path(args.config), args.robot_id)

    os.environ["ROS_DOMAIN_ID"] = str(robot.ros_domain_id)
    os.environ.setdefault("RMW_IMPLEMENTATION", "rmw_cyclonedds_cpp")
    os.environ.setdefault("ROS_AUTOMATIC_DISCOVERY_RANGE", "SUBNET")

    image_topic = getattr(robot, "skyway_bridge_image_topic", None) or robot.image_topic
    image_compressed = bool_for_image_topic(robot, image_topic)
    member_name = getattr(robot, "skyway_bridge_member_name", None) or f"skyway_ros_bridge-{robot.id}"
    room_name = skyway_room_for(robot)

    try:
        app_id = os.environ["SKYWAY_APP_ID"]
        secret_key = os.environ["SKYWAY_SECRET_KEY"]
    except KeyError as exc:
        raise SystemExit(f"{exc.args[0]} is required in {args.env_file} or environment") from exc

    print(
        "V9 skyway_ros_bridge configuration:\n"
        f"  robot_id: {robot.id}\n"
        f"  ROS_DOMAIN_ID: {robot.ros_domain_id}\n"
        f"  room: {room_name}\n"
        f"  member: {member_name}\n"
        f"  image_topic: {image_topic}\n"
        f"  is_compressed: {str(image_compressed).lower()}",
        flush=True,
    )

    bridge_process: subprocess.Popen[str] | None = None
    try:
        run_output(["ros2", "pkg", "prefix", "skyway_ros_bridge"], timeout=10)
        if not args.skip_topic_check:
            validate_image_topic(image_topic, image_compressed)

        if args.start_bridge:
            bridge_process = start_bridge_process()

        wait_for_service(JOIN_ROOM_SERVICE, args.wait_sec)
        wait_for_service(PUBLISH_IMAGE_SERVICE, args.wait_sec)

        token = create_skyway_token(
            app_id=app_id,
            secret_key=secret_key,
            room_name=room_name,
            member_name=member_name,
            can_publish=True,
            can_subscribe=False,
        )

        join_request = (
            "{"
            f"skyway_auth_token: '{token}', "
            f"room_name: '{room_name}', "
            f"member_name: '{member_name}', "
            "member_metadata: 'lightrover-v9-skyway-ros-bridge'"
            "}"
        )
        service_call(JOIN_ROOM_SERVICE, "skyway_ros_bridge_msgs/srv/JoinRoom", join_request)

        publish_request = (
            "{"
            f"topic_name: '{image_topic}', "
            f"is_compressed: {str(image_compressed).lower()}, "
            "metadata: 'lightrover-v9-skyway-ros-bridge-video'"
            "}"
        )
        service_call(
            PUBLISH_IMAGE_SERVICE,
            "skyway_ros_bridge_msgs/srv/PublishVideoStreamToSkywayByImage",
            publish_request,
        )

        print("V9 skyway_ros_bridge is publishing the ROS image topic to SkyWay.", flush=True)

        if bridge_process is not None and args.keep_alive:
            while bridge_process.poll() is None:
                time.sleep(1.0)
            return bridge_process.returncode or 0
        return 0
    except KeyboardInterrupt:
        return 130
    finally:
        if bridge_process is not None and bridge_process.poll() is None:
            bridge_process.send_signal(signal.SIGINT)
            try:
                bridge_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                bridge_process.terminate()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        if exc.stdout:
            print(exc.stdout, end="" if exc.stdout.endswith("\n") else "\n", file=sys.stderr)
        print(
            f"ERROR: command failed with exit code {exc.returncode}: {' '.join(exc.cmd)}\n"
            "Check that ROS 2 is sourced, skyway_ros_bridge is built and sourced, and the target ROS graph is reachable.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    except subprocess.TimeoutExpired as exc:
        print(
            f"ERROR: command timed out after {exc.timeout}s: {' '.join(exc.cmd)}\n"
            "If `ros2 topic list --no-daemon` works, retry after confirming the exact topic type with:\n"
            "  ros2 topic type --no-daemon --spin-time 2.0 /image_raw/compressed\n"
            "You can bypass only this preflight check with:\n"
            "  ./scripts/launch_skyway_ros_bridge_pc.sh --robot-id lightrover1 --skip-topic-check",
            file=sys.stderr,
        )
        raise SystemExit(1)
    except (RuntimeError, TimeoutError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
