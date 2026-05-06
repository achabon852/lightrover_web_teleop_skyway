from __future__ import annotations
import base64, math, os, queue, time
from multiprocessing import Queue
from typing import Any

import cv2
import numpy as np


def yaw_from_quat(q) -> float:
    x, y, z, w = q.x, q.y, q.z, q.w
    siny_cosp = 2.0 * (w * z + x * y)
    cosy_cosp = 1.0 - 2.0 * (y * y + z * z)
    return math.atan2(siny_cosp, cosy_cosp)


def normalize_angle(rad: float) -> float:
    return math.atan2(math.sin(rad), math.cos(rad))


def run_ros_worker(robot_dict: dict[str, Any], cmd_q: Queue, event_q: Queue) -> None:
    # This function runs in an independent process. ROS_DOMAIN_ID must be set before importing rclpy.
    os.environ["ROS_DOMAIN_ID"] = str(robot_dict["ros_domain_id"])
    os.environ.setdefault("RMW_IMPLEMENTATION", "rmw_cyclonedds_cpp")
    os.environ.setdefault("ROS_LOCALHOST_ONLY", "0")

    import rclpy
    from rclpy.node import Node
    from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
    from geometry_msgs.msg import PoseWithCovarianceStamped, Twist
    from rcl_interfaces.msg import Parameter, ParameterType, ParameterValue
    from rcl_interfaces.srv import SetParameters
    from sensor_msgs.msg import CompressedImage, Image
    from std_msgs.msg import UInt8MultiArray
    from nav_msgs.msg import OccupancyGrid, Odometry
    from tf2_ros import Buffer, TransformListener

    class Worker(Node):
        def __init__(self) -> None:
            super().__init__(f"{robot_dict['id']}_web_teleop_bridge")
            self.robot_id = robot_dict["id"]
            self.command_topic = robot_dict.get("cmd_vel_topic", "/rover_twist_cmd")
            self.cmd_pub = self.create_publisher(Twist, self.command_topic, 10)
            self.initial_pose_pub = self.create_publisher(PoseWithCovarianceStamped, "/initialpose", 10)
            self.last_image_sent = 0.0
            self.camera_period = 1.0 / max(1, int(robot_dict.get("camera_fps", 10)))
            self.jpeg_quality = int(robot_dict.get("jpeg_quality", 70))
            self.audio_enabled = bool(robot_dict.get("audio_enabled", True))
            self.audio_sample_rate = int(robot_dict.get("audio_sample_rate", 16000))
            self.audio_channels = int(robot_dict.get("audio_channels", 1))
            self.audio_topic = robot_dict.get("audio_topic", "/lightrover/audio/pcm_s16le")
            self.last_map_sent = 0.0
            self.latest_cmd = Twist()
            self.last_cmd_time = 0.0
            self.last_pose_time = 0.0
            self.last_odom_pose_sent = 0.0
            self.last_map_pose_received = 0.0
            self.latest_odom_pose: tuple[float, float, float] | None = None
            self.odom_anchor: tuple[float, float, float] | None = None
            self.map_anchor: tuple[float, float, float] | None = None
            self.timeout = float(robot_dict.get("watchdog_timeout_sec", 0.35))
            self.watchdog_enabled = bool(robot_dict.get("watchdog_enabled", True))
            self.watchdog_service_name = robot_dict.get(
                "watchdog_service_name",
                "/lightrover_safety_watchdog/set_parameters",
            )
            self.watchdog_param_client = self.create_client(SetParameters, self.watchdog_service_name)
            self.watchdog_set_futures = []
            self.pending_watchdog_enabled = self.watchdog_enabled
            self.last_watchdog_set_attempt = 0.0
            self.tf_buffer = Buffer()
            self.tf_listener = TransformListener(self.tf_buffer, self)

            if robot_dict.get("image_type", "compressed") == "raw":
                self.create_subscription(Image, robot_dict["image_topic"], self.on_image_raw, 5)
            else:
                self.create_subscription(CompressedImage, robot_dict["image_topic"], self.on_image_compressed, 5)
            if self.audio_enabled:
                self.create_subscription(UInt8MultiArray, self.audio_topic, self.on_audio_pcm, 20)
            map_topic = robot_dict.get("map_topic", "/map")
            transient_map_qos = QoSProfile(
                depth=1,
                reliability=ReliabilityPolicy.RELIABLE,
                durability=DurabilityPolicy.TRANSIENT_LOCAL,
            )
            volatile_map_qos = QoSProfile(
                depth=1,
                reliability=ReliabilityPolicy.RELIABLE,
                durability=DurabilityPolicy.VOLATILE,
            )
            self.map_subscriptions = [
                self.create_subscription(OccupancyGrid, map_topic, self.on_map, transient_map_qos),
                self.create_subscription(OccupancyGrid, map_topic, self.on_map, volatile_map_qos),
            ]
            amcl_topic = robot_dict.get("amcl_pose_topic", "/amcl_pose")
            amcl_transient_qos = QoSProfile(
                depth=1,
                reliability=ReliabilityPolicy.RELIABLE,
                durability=DurabilityPolicy.TRANSIENT_LOCAL,
            )
            amcl_volatile_qos = QoSProfile(
                depth=10,
                reliability=ReliabilityPolicy.RELIABLE,
                durability=DurabilityPolicy.VOLATILE,
            )
            self.amcl_subscriptions = [
                self.create_subscription(PoseWithCovarianceStamped, amcl_topic, self.on_amcl_pose, amcl_transient_qos),
                self.create_subscription(PoseWithCovarianceStamped, amcl_topic, self.on_amcl_pose, amcl_volatile_qos),
            ]
            self.create_subscription(
                Odometry,
                robot_dict.get("odom_topic", "/odom"),
                self.on_odom,
                10,
            )
            self.create_timer(0.03, self.on_timer)
            event_q.put({
                "type": "status",
                "robot_id": self.robot_id,
                "message": "ros worker started",
                "domain": robot_dict["ros_domain_id"],
                "map_topic": map_topic,
                "cmd_vel_topic": self.command_topic,
                "audio_topic": self.audio_topic if self.audio_enabled else None,
            })
            self.publish_watchdog_state("local")

        def on_timer(self) -> None:
            while True:
                try:
                    msg = cmd_q.get_nowait()
                except queue.Empty:
                    break
                if msg.get("type") == "cmd":
                    self.latest_cmd = self.make_twist(msg.get("direction", "stop"))
                    self.last_cmd_time = time.monotonic()
                    self.publish_cmd(self.latest_cmd)
                elif msg.get("type") == "stop":
                    self.latest_cmd = Twist()
                    self.last_cmd_time = 0.0
                    self.publish_cmd(self.latest_cmd)
                elif msg.get("type") == "initial_pose":
                    self.publish_initial_pose(msg)
                elif msg.get("type") == "watchdog":
                    self.set_watchdog_enabled(bool(msg.get("enabled", True)))

            # PC-side watchdog as backup. Lightrover side also has the authoritative safety watchdog.
            if self.watchdog_enabled and self.last_cmd_time and (time.monotonic() - self.last_cmd_time) > self.timeout:
                self.last_cmd_time = 0.0
                self.publish_cmd(Twist())

            self.flush_watchdog_parameter()

            now = time.monotonic()
            if now - self.last_pose_time > 0.1:
                self.last_pose_time = now
                self.publish_pose()

        def set_watchdog_enabled(self, enabled: bool) -> None:
            self.watchdog_enabled = enabled
            self.pending_watchdog_enabled = enabled
            self.latest_cmd = Twist()
            self.last_cmd_time = 0.0
            self.publish_cmd(self.latest_cmd)
            self.publish_watchdog_state("pending")

        def publish_watchdog_state(self, remote: str) -> None:
            event_q.put({
                "type": "watchdog",
                "robot_id": self.robot_id,
                "enabled": self.watchdog_enabled,
                "remote": remote,
                "service": self.watchdog_service_name,
                "timeout_sec": self.timeout,
            })

        def flush_watchdog_parameter(self) -> None:
            remaining = []
            for future, enabled in self.watchdog_set_futures:
                if not future.done():
                    remaining.append((future, enabled))
                    continue
                try:
                    response = future.result()
                    ok = bool(response.results) and all(result.successful for result in response.results)
                    remote = "ok" if ok else "rejected"
                except Exception as e:
                    remote = f"error: {e}"
                if self.pending_watchdog_enabled == enabled:
                    self.pending_watchdog_enabled = None
                self.publish_watchdog_state(remote)
            self.watchdog_set_futures = remaining

            if self.pending_watchdog_enabled is None:
                return
            now = time.monotonic()
            if now - self.last_watchdog_set_attempt < 1.0:
                return
            self.last_watchdog_set_attempt = now
            if not self.watchdog_param_client.service_is_ready():
                self.publish_watchdog_state("waiting_service")
                return

            value = ParameterValue(
                type=ParameterType.PARAMETER_BOOL,
                bool_value=self.pending_watchdog_enabled,
            )
            req = SetParameters.Request()
            req.parameters = [Parameter(name="enabled", value=value)]
            self.watchdog_set_futures.append(
                (self.watchdog_param_client.call_async(req), self.pending_watchdog_enabled)
            )

        def make_twist(self, direction: str) -> Twist:
            t = Twist()
            lin = float(robot_dict.get("linear_speed", 0.18))
            ang = float(robot_dict.get("angular_speed", 0.7))
            if direction == "forward":
                t.linear.x = lin
            elif direction == "backward":
                t.linear.x = -lin
            elif direction == "left":
                t.angular.z = ang
            elif direction == "right":
                t.angular.z = -ang
            return t

        def publish_cmd(self, twist: Twist) -> None:
            self.cmd_pub.publish(twist)

        def publish_initial_pose(self, msg: dict[str, Any]) -> None:
            x = float(msg.get("x", 0.0))
            y = float(msg.get("y", 0.0))
            yaw = float(msg.get("yaw", 0.0))
            pose = PoseWithCovarianceStamped()
            pose.header.frame_id = robot_dict.get("map_frame", "map")
            pose.header.stamp = self.get_clock().now().to_msg()
            pose.pose.pose.position.x = x
            pose.pose.pose.position.y = y
            pose.pose.pose.orientation.z = math.sin(yaw / 2.0)
            pose.pose.pose.orientation.w = math.cos(yaw / 2.0)
            pose.pose.covariance[0] = 0.25
            pose.pose.covariance[7] = 0.25
            pose.pose.covariance[35] = 0.0685
            self.initial_pose_pub.publish(pose)
            self.map_anchor = (x, y, yaw)
            self.odom_anchor = self.latest_odom_pose
            event_q.put({
                "type": "status",
                "robot_id": self.robot_id,
                "message": f"initial pose set x={pose.pose.pose.position.x:.2f} y={pose.pose.pose.position.y:.2f}",
            })
            event_q.put({
                "type": "pose",
                "robot_id": self.robot_id,
                "x": x,
                "y": y,
                "yaw": yaw,
                "frame_id": robot_dict.get("map_frame", "map"),
                "stamp": time.time(),
                "source": "initial_pose",
                "approximate": False,
            })

        def on_image_compressed(self, msg: CompressedImage) -> None:
            now = time.monotonic()
            if now - self.last_image_sent < self.camera_period:
                return
            self.last_image_sent = now
            b64 = base64.b64encode(bytes(msg.data)).decode("ascii")
            event_q.put({"type": "image", "robot_id": self.robot_id, "mime": "image/jpeg", "data": b64})

        def on_image_raw(self, msg: Image) -> None:
            now = time.monotonic()
            if now - self.last_image_sent < self.camera_period:
                return
            self.last_image_sent = now
            try:
                arr = np.frombuffer(msg.data, dtype=np.uint8)
                if msg.encoding in ("rgb8", "bgr8"):
                    img = arr.reshape((msg.height, msg.width, 3))
                    if msg.encoding == "rgb8":
                        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
                elif msg.encoding in ("mono8", "8UC1"):
                    img = arr.reshape((msg.height, msg.width))
                else:
                    return
                ok, enc = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality])
                if ok:
                    event_q.put({"type": "image", "robot_id": self.robot_id, "mime": "image/jpeg", "data": base64.b64encode(enc).decode("ascii")})
            except Exception as e:
                event_q.put({"type": "error", "robot_id": self.robot_id, "message": f"raw image encode error: {e}"})

        def on_audio_pcm(self, msg: UInt8MultiArray) -> None:
            if not msg.data:
                return
            try:
                event_q.put({
                    "type": "audio_pcm",
                    "robot_id": self.robot_id,
                    "format": "s16le",
                    "sample_rate": self.audio_sample_rate,
                    "channels": self.audio_channels,
                    "data": base64.b64encode(bytes(msg.data)).decode("ascii"),
                })
            except Exception as e:
                event_q.put({"type": "error", "robot_id": self.robot_id, "message": f"audio bridge error: {e}"})

        def on_map(self, msg: OccupancyGrid) -> None:
            now = time.monotonic()
            if now - self.last_map_sent < 1.0:
                return
            self.last_map_sent = now
            try:
                w, h = msg.info.width, msg.info.height
                data = np.array(msg.data, dtype=np.int16).reshape((h, w))
                img = np.zeros((h, w), dtype=np.uint8)
                img[data == -1] = 205
                img[data == 0] = 254
                img[data > 50] = 0
                img = np.flipud(img)
                ok, png = cv2.imencode(".png", img)
                if not ok:
                    return
                event_q.put({
                    "type": "map",
                    "robot_id": self.robot_id,
                    "data": base64.b64encode(png).decode("ascii"),
                    "width": w,
                    "height": h,
                    "resolution": msg.info.resolution,
                    "stamp": time.time(),
                    "origin": {
                        "x": msg.info.origin.position.x,
                        "y": msg.info.origin.position.y,
                        "yaw": yaw_from_quat(msg.info.origin.orientation),
                    },
                })
            except Exception as e:
                event_q.put({"type": "error", "robot_id": self.robot_id, "message": f"map convert error: {e}"})

        def publish_pose(self) -> None:
            try:
                tr = self.tf_buffer.lookup_transform(robot_dict.get("map_frame", "map"), robot_dict.get("base_frame", "base_link"), rclpy.time.Time())
                p = tr.transform.translation
                q = tr.transform.rotation
                event_q.put({
                    "type": "pose",
                    "robot_id": self.robot_id,
                    "x": p.x,
                    "y": p.y,
                    "yaw": yaw_from_quat(q),
                    "frame_id": robot_dict.get("map_frame", "map"),
                    "stamp": time.time(),
                    "source": "tf",
                    "approximate": False,
                })
                self.last_map_pose_received = time.monotonic()
            except Exception:
                pass

        def on_amcl_pose(self, msg: PoseWithCovarianceStamped) -> None:
            self.last_map_pose_received = time.monotonic()
            p = msg.pose.pose.position
            q = msg.pose.pose.orientation
            yaw = yaw_from_quat(q)
            self.map_anchor = (p.x, p.y, yaw)
            self.odom_anchor = self.latest_odom_pose
            event_q.put({
                "type": "pose",
                "robot_id": self.robot_id,
                "x": p.x,
                "y": p.y,
                "yaw": yaw,
                "frame_id": msg.header.frame_id or robot_dict.get("map_frame", "map"),
                "stamp": time.time(),
                "source": "amcl_pose",
                "approximate": False,
            })

        def on_odom(self, msg: Odometry) -> None:
            if time.monotonic() - self.last_map_pose_received < 5.0:
                return
            now = time.monotonic()
            if now - self.last_odom_pose_sent < 0.1:
                return
            self.last_odom_pose_sent = now
            p = msg.pose.pose.position
            q = msg.pose.pose.orientation
            odom_pose = (p.x, p.y, yaw_from_quat(q))
            self.latest_odom_pose = odom_pose
            anchored_pose = self.odom_to_anchored_map_pose(odom_pose)
            if anchored_pose:
                x, y, yaw = anchored_pose
                event_q.put({
                    "type": "pose",
                    "robot_id": self.robot_id,
                    "x": x,
                    "y": y,
                    "yaw": yaw,
                    "frame_id": robot_dict.get("map_frame", "map"),
                    "stamp": time.time(),
                    "source": "odom_anchor",
                    "approximate": False,
                })
                return
            event_q.put({
                "type": "pose",
                "robot_id": self.robot_id,
                "x": p.x,
                "y": p.y,
                "yaw": odom_pose[2],
                "frame_id": msg.header.frame_id or robot_dict.get("odom_frame", "odom"),
                "stamp": time.time(),
                "source": "odom",
                "approximate": True,
            })

        def odom_to_anchored_map_pose(self, odom_pose: tuple[float, float, float]) -> tuple[float, float, float] | None:
            if not self.map_anchor or not self.odom_anchor:
                return None
            map_x, map_y, map_yaw = self.map_anchor
            odom_x0, odom_y0, odom_yaw0 = self.odom_anchor
            odom_x, odom_y, odom_yaw = odom_pose
            dx = odom_x - odom_x0
            dy = odom_y - odom_y0
            yaw_offset = map_yaw - odom_yaw0
            cos_o = math.cos(yaw_offset)
            sin_o = math.sin(yaw_offset)
            x = map_x + cos_o * dx - sin_o * dy
            y = map_y + sin_o * dx + cos_o * dy
            yaw = normalize_angle(map_yaw + normalize_angle(odom_yaw - odom_yaw0))
            return x, y, yaw

    rclpy.init()
    node = Worker()
    try:
        rclpy.spin(node)
    finally:
        node.publish_cmd(Twist())
        node.destroy_node()
        rclpy.shutdown()
