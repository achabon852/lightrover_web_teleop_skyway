from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import yaml

@dataclass(frozen=True)
class RobotConfig:
    id: str
    name: str
    ros_domain_id: int
    cmd_vel_topic: str = "/rover_twist_cmd"
    motor_cmd_vel_topic: str = "/rover_twist"
    image_topic: str = "/image_raw/compressed"
    image_type: str = "compressed"
    map_topic: str = "/map"
    map_yaml: str | None = None
    amcl_pose_topic: str = "/amcl_pose"
    odom_topic: str = "/odom"
    scan_topic: str = "/scan"
    odom_frame: str = "odom"
    map_frame: str = "map"
    base_frame: str = "base_link"
    linear_speed: float = 0.18
    angular_speed: float = 0.7
    command_hz: int = 15
    watchdog_timeout_sec: float = 0.35
    watchdog_enabled: bool = True
    watchdog_service_name: str = "/lightrover_safety_watchdog/set_parameters"
    camera_fps: int = 10
    jpeg_quality: int = 70
    camera_zoom_default: float = 1.0
    camera_zoom_min: float = 1.0
    camera_zoom_max: float = 3.0
    camera_zoom_step: float = 0.1
    camera_brightness_default: float = 1.0
    camera_brightness_min: float = 0.5
    camera_brightness_max: float = 1.5
    camera_brightness_step: float = 0.05
    audio_enabled: bool = True
    audio_topic: str = "/lightrover/audio/pcm_s16le"
    audio_sample_rate: int = 16000
    audio_channels: int = 1

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "RobotConfig":
        return RobotConfig(**d)

def load_config(path: str | Path) -> list[RobotConfig]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"robots config not found: {p}")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    robots = data.get("robots", [])
    if not robots:
        raise ValueError("robots.yaml must contain at least one robot")
    ids = [r.get("id") for r in robots]
    if len(ids) != len(set(ids)):
        raise ValueError("robot id must be unique")
    return [RobotConfig.from_dict(r) for r in robots]
