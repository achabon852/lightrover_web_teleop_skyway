from __future__ import annotations
import time
import rclpy
from rclpy.node import Node
from rcl_interfaces.msg import SetParametersResult
from geometry_msgs.msg import Twist

class SafetyWatchdog(Node):
    def __init__(self):
        super().__init__('lightrover_safety_watchdog')
        self.declare_parameter('input_topic', '/rover_twist_cmd')
        self.declare_parameter('output_topic', '/rover_twist')
        self.declare_parameter('timeout_sec', 0.35)
        self.declare_parameter('publish_hz', 20.0)
        self.declare_parameter('enabled', True)
        self.input_topic = self.get_parameter('input_topic').value
        self.output_topic = self.get_parameter('output_topic').value
        self.timeout_sec = float(self.get_parameter('timeout_sec').value)
        self.enabled = bool(self.get_parameter('enabled').value)
        self.latest = Twist()
        self.last_time = 0.0
        self.stopped = True
        self.pub = self.create_publisher(Twist, self.output_topic, 10)
        self.sub = self.create_subscription(Twist, self.input_topic, self.on_cmd, 10)
        self.add_on_set_parameters_callback(self.on_set_parameters)
        hz = float(self.get_parameter('publish_hz').value)
        self.timer = self.create_timer(1.0 / max(1.0, hz), self.on_timer)
        self.get_logger().info(
            f'safety watchdog: {self.input_topic} -> {self.output_topic}, '
            f'timeout={self.timeout_sec}s enabled={self.enabled}'
        )
    def on_set_parameters(self, params):
        for param in params:
            if param.name == 'enabled':
                self.enabled = bool(param.value)
                self.stopped = True
                self.last_time = 0.0
                self.pub.publish(Twist())
                self.get_logger().warn(f'safety watchdog enabled={self.enabled}')
        return SetParametersResult(successful=True)
    def on_cmd(self, msg: Twist):
        self.latest = msg
        self.last_time = time.monotonic()
        self.stopped = False
        self.pub.publish(msg)
    def on_timer(self):
        if not self.enabled:
            return
        if self.last_time == 0.0 or (time.monotonic() - self.last_time) > self.timeout_sec:
            if not self.stopped:
                self.get_logger().warn('cmd timeout: publish zero Twist')
            self.stopped = True
            self.pub.publish(Twist())
        else:
            self.pub.publish(self.latest)

def main():
    rclpy.init()
    node = SafetyWatchdog()
    try:
        rclpy.spin(node)
    finally:
        node.pub.publish(Twist())
        node.destroy_node()
        rclpy.shutdown()

if __name__ == '__main__':
    main()
