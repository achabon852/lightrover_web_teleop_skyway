from __future__ import annotations

import math
import os
import threading
import time
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


class CmdVel(BaseModel):
    linear_x: float = Field(default=0.0, ge=-1.0, le=1.0)
    angular_z: float = Field(default=0.0, ge=-3.14, le=3.14)
    duration_ms: int = Field(default=200, ge=0, le=5000)


class CameraSettings(BaseModel):
    zoom: float = 1.0
    brightness: float = 1.0
    source: str = "edge_api"


class WatchdogSettings(BaseModel):
    enabled: bool = True


class InitialPose(BaseModel):
    x: float = 0.0
    y: float = 0.0
    yaw: float = 0.0


class NavGoal(BaseModel):
    x: float
    y: float
    yaw: float = 0.0


app = FastAPI(title="Lightrover Factory Edge Gateway")

ROS_NODE: Any | None = None
ROS_IMPORT_ERROR: str | None = None
LAST_CMD: dict[str, Any] | None = None
LAST_CMD_SEQ = 0
LAST_CAMERA_SETTINGS: dict[str, Any] = {
    "type": "camera_settings",
    "zoom": 1.0,
    "brightness": 1.0,
    "source": "edge_default",
}
WATCHDOG_STATE: dict[str, Any] = {
    "type": "watchdog",
    "enabled": True,
    "remote": "edge_api",
    "timeout_sec": float(os.environ.get("WATCHDOG_TIMEOUT_SEC", "0.35")),
}
LAST_POSE: dict[str, Any] | None = None


def require_ros_node() -> Any:
    if ROS_NODE is None:
        detail = "ROS node is not available"
        if ROS_IMPORT_ERROR:
            detail = f"{detail}: {ROS_IMPORT_ERROR}"
        raise HTTPException(status_code=503, detail=detail)
    return ROS_NODE


def schedule_stop_after_duration(node: Any, duration_ms: int, seq: int) -> None:
    if duration_ms <= 0:
        return

    def stop_if_latest() -> None:
        if seq != LAST_CMD_SEQ:
            return
        node.publish_cmd(CmdVel(linear_x=0.0, angular_z=0.0, duration_ms=0))

    timer = threading.Timer(duration_ms / 1000.0, stop_if_latest)
    timer.daemon = True
    timer.start()


def yaw_to_quat_z_w(yaw: float) -> tuple[float, float]:
    return math.sin(yaw / 2.0), math.cos(yaw / 2.0)


@app.on_event("startup")
def startup() -> None:
    global ROS_NODE, ROS_IMPORT_ERROR
    if os.environ.get("EDGE_GATEWAY_ENABLE_ROS", "true").strip().lower() in {"0", "false", "no"}:
        ROS_IMPORT_ERROR = "ROS disabled by EDGE_GATEWAY_ENABLE_ROS"
        return
    try:
        import rclpy
        from rclpy.node import Node
        from geometry_msgs.msg import PoseWithCovarianceStamped, Twist
        from rcl_interfaces.msg import Parameter, ParameterType, ParameterValue
        from rcl_interfaces.srv import SetParameters
    except Exception as e:  # pragma: no cover - depends on ROS environment
        ROS_IMPORT_ERROR = str(e)
        return

    os.environ.setdefault("RMW_IMPLEMENTATION", "rmw_cyclonedds_cpp")
    if not rclpy.ok():
        rclpy.init()

    class EdgeNode(Node):
        def __init__(self) -> None:
            super().__init__("lightrover_factory_edge_gateway")
            self.cmd_vel_topic = os.environ.get("CMD_VEL_TOPIC", "/rover_twist_cmd")
            self.initial_pose_topic = os.environ.get("INITIAL_POSE_TOPIC", "/initialpose")
            self.map_frame = os.environ.get("MAP_FRAME", "map")
            self.watchdog_service_name = os.environ.get(
                "WATCHDOG_SERVICE_NAME",
                "/lightrover_safety_watchdog/set_parameters",
            )
            self.cmd_pub = self.create_publisher(Twist, self.cmd_vel_topic, 10)
            self.initial_pose_pub = self.create_publisher(PoseWithCovarianceStamped, self.initial_pose_topic, 10)
            self.watchdog_client = self.create_client(SetParameters, self.watchdog_service_name)

        def publish_cmd(self, cmd: CmdVel) -> None:
            twist = Twist()
            twist.linear.x = cmd.linear_x
            twist.angular.z = cmd.angular_z
            self.cmd_pub.publish(twist)

        def publish_initial_pose(self, pose: InitialPose) -> None:
            msg = PoseWithCovarianceStamped()
            msg.header.frame_id = self.map_frame
            msg.header.stamp = self.get_clock().now().to_msg()
            msg.pose.pose.position.x = pose.x
            msg.pose.pose.position.y = pose.y
            z, w = yaw_to_quat_z_w(pose.yaw)
            msg.pose.pose.orientation.z = z
            msg.pose.pose.orientation.w = w
            msg.pose.covariance[0] = 0.25
            msg.pose.covariance[7] = 0.25
            msg.pose.covariance[35] = 0.0685
            self.initial_pose_pub.publish(msg)

        def set_watchdog_enabled(self, enabled: bool) -> str:
            if not self.watchdog_client.service_is_ready():
                return "waiting_service"
            value = ParameterValue(type=ParameterType.PARAMETER_BOOL, bool_value=enabled)
            req = SetParameters.Request()
            req.parameters = [Parameter(name="enabled", value=value)]
            future = self.watchdog_client.call_async(req)
            deadline = time.monotonic() + 1.0
            while not future.done() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not future.done():
                return "timeout"
            response = future.result()
            ok = bool(response.results) and all(result.successful for result in response.results)
            return "ok" if ok else "rejected"

    ROS_NODE = EdgeNode()
    thread = threading.Thread(target=rclpy.spin, args=(ROS_NODE,), daemon=True)
    thread.start()


@app.on_event("shutdown")
def shutdown() -> None:
    global ROS_NODE
    if ROS_NODE is None:
        return
    try:
        ROS_NODE.destroy_node()
    finally:
        ROS_NODE = None


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "ros_available": ROS_NODE is not None,
        "ros_error": ROS_IMPORT_ERROR,
    }


@app.get("/api/robots/{robot_id}/status")
def status(robot_id: str) -> dict[str, Any]:
    return {
        "status": {
            "type": "status",
            "robot_id": robot_id,
            "message": "edge gateway running" if ROS_NODE else "edge gateway running without ROS",
            "stamp": time.time(),
            "ros_available": ROS_NODE is not None,
        },
        "pose": LAST_POSE,
        "watchdog": {**WATCHDOG_STATE, "robot_id": robot_id},
        "camera_settings": {**LAST_CAMERA_SETTINGS, "robot_id": robot_id},
        "last_cmd": LAST_CMD,
    }


@app.post("/api/robots/{robot_id}/cmd_vel")
def cmd_vel(robot_id: str, cmd: CmdVel) -> dict[str, Any]:
    global LAST_CMD, LAST_CMD_SEQ
    node = require_ros_node()
    LAST_CMD_SEQ += 1
    node.publish_cmd(cmd)
    schedule_stop_after_duration(node, cmd.duration_ms, LAST_CMD_SEQ)
    LAST_CMD = {
        "robot_id": robot_id,
        "linear_x": cmd.linear_x,
        "angular_z": cmd.angular_z,
        "duration_ms": cmd.duration_ms,
        "stamp": time.time(),
    }
    return {"status": "ok", **LAST_CMD}


@app.post("/api/robots/{robot_id}/stop")
def stop(robot_id: str) -> dict[str, Any]:
    global LAST_CMD_SEQ
    node = require_ros_node()
    LAST_CMD_SEQ += 1
    node.publish_cmd(CmdVel(linear_x=0.0, angular_z=0.0, duration_ms=0))
    return {"status": "stopped", "robot_id": robot_id, "stamp": time.time()}


@app.post("/api/robots/{robot_id}/camera/settings")
def camera_settings(robot_id: str, settings: CameraSettings) -> dict[str, Any]:
    global LAST_CAMERA_SETTINGS
    LAST_CAMERA_SETTINGS = {
        "type": "camera_settings",
        "robot_id": robot_id,
        "zoom": settings.zoom,
        "brightness": settings.brightness,
        "source": settings.source,
    }
    return LAST_CAMERA_SETTINGS


@app.post("/api/robots/{robot_id}/watchdog")
def watchdog(robot_id: str, settings: WatchdogSettings) -> dict[str, Any]:
    global WATCHDOG_STATE
    node = require_ros_node()
    remote = node.set_watchdog_enabled(settings.enabled)
    WATCHDOG_STATE = {
        "type": "watchdog",
        "robot_id": robot_id,
        "enabled": settings.enabled,
        "remote": remote,
        "timeout_sec": float(os.environ.get("WATCHDOG_TIMEOUT_SEC", "0.35")),
    }
    return WATCHDOG_STATE


@app.post("/api/robots/{robot_id}/initial_pose")
def initial_pose(robot_id: str, pose: InitialPose) -> dict[str, Any]:
    global LAST_POSE
    node = require_ros_node()
    node.publish_initial_pose(pose)
    LAST_POSE = {
        "type": "pose",
        "robot_id": robot_id,
        "x": pose.x,
        "y": pose.y,
        "yaw": pose.yaw,
        "frame_id": os.environ.get("MAP_FRAME", "map"),
        "source": "initial_pose",
        "stamp": time.time(),
    }
    return LAST_POSE


@app.post("/api/robots/{robot_id}/nav_goal")
def nav_goal(robot_id: str, goal: NavGoal) -> dict[str, Any]:
    # Placeholder for Nav2 action integration. The endpoint exists so Azure BFF and UI wiring are stable.
    return {
        "status": "not_implemented",
        "robot_id": robot_id,
        "x": goal.x,
        "y": goal.y,
        "yaw": goal.yaw,
    }
