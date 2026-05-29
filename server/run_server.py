from __future__ import annotations
import asyncio, os, sys, uuid
from pathlib import Path
from multiprocessing import Process, Queue
from typing import Any

import base64
import cv2
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.config_loader import load_config, RobotConfig
from server.ros_worker import run_ros_worker
from server.skyway_token import create_skyway_token

CONFIG_PATH = Path(os.environ.get("ROBOTS_CONFIG", ROOT / "config" / "robots.yaml"))
ROBOTS = load_config(CONFIG_PATH)
ROBOT_BY_ID = {r.id: r for r in ROBOTS}

cmd_queues: dict[str, Queue] = {}
event_q: Queue = Queue()
processes: list[Process] = []
clients: set[WebSocket] = set()
latest_map: dict[str, dict[str, Any]] = {}
latest_pose: dict[str, dict[str, Any]] = {}
latest_status: dict[str, dict[str, Any]] = {}
latest_error: dict[str, dict[str, Any]] = {}
latest_watchdog: dict[str, dict[str, Any]] = {}
latest_camera_settings: dict[str, dict[str, Any]] = {}

app = FastAPI(title="Lightrover Web Teleop PC-heavy")
app.mount("/static", StaticFiles(directory=str(ROOT / "web")), name="static")


def skyway_room_for(robot: RobotConfig) -> str:
    explicit_room = getattr(robot, "skyway_room", None)
    if explicit_room:
        return explicit_room
    room_prefix = os.environ.get("SKYWAY_ROOM_PREFIX", "lightrover")
    return f"{room_prefix}-{robot.id}"


def robot_payload(robot: RobotConfig) -> dict[str, Any]:
    payload = robot.__dict__.copy()
    payload.setdefault("label", payload.get("name", robot.id))
    payload["skyway_room"] = skyway_room_for(robot)
    payload["camera_controls"] = {
        "zoom": {
            "default": float(getattr(robot, "camera_zoom_default", 1.0)),
            "min": float(getattr(robot, "camera_zoom_min", 1.0)),
            "max": float(getattr(robot, "camera_zoom_max", 3.0)),
            "step": float(getattr(robot, "camera_zoom_step", 0.1)),
        },
        "brightness": {
            "default": float(getattr(robot, "camera_brightness_default", 1.0)),
            "min": float(getattr(robot, "camera_brightness_min", 0.5)),
            "max": float(getattr(robot, "camera_brightness_max", 1.5)),
            "step": float(getattr(robot, "camera_brightness_step", 0.05)),
        },
    }
    return payload


def load_static_map(robot: RobotConfig) -> dict[str, Any] | None:
    map_yaml = getattr(robot, "map_yaml", None) or os.environ.get("MAP_YAML")
    if not map_yaml:
        return None

    yaml_path = Path(map_yaml).expanduser()
    if not yaml_path.exists():
        return None

    import yaml
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    image_path = Path(data.get("image", ""))
    if not image_path.is_absolute():
        image_path = yaml_path.parent / image_path
    image = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        return None
    ok, png = cv2.imencode(".png", image)
    if not ok:
        return None
    origin = data.get("origin") or [0.0, 0.0, 0.0]
    return {
        "type": "map",
        "robot_id": robot.id,
        "data": base64.b64encode(png).decode("ascii"),
        "width": int(image.shape[1]),
        "height": int(image.shape[0]),
        "resolution": float(data.get("resolution", 0.05)),
        "origin": {
            "x": float(origin[0]),
            "y": float(origin[1]),
            "yaw": float(origin[2]) if len(origin) > 2 else 0.0,
        },
        "stamp": 0,
        "source": "map_yaml",
    }

@app.on_event("startup")
async def startup() -> None:
    for r in ROBOTS:
        static_map = load_static_map(r)
        if static_map:
            latest_map[r.id] = static_map
    for r in ROBOTS:
        latest_camera_settings[r.id] = {
            "type": "camera_settings",
            "robot_id": r.id,
            "zoom": float(getattr(r, "camera_zoom_default", 1.0)),
            "brightness": float(getattr(r, "camera_brightness_default", 1.0)),
            "source": "config",
        }
    for r in ROBOTS:
        q: Queue = Queue()
        cmd_queues[r.id] = q
        p = Process(target=run_ros_worker, args=(r.__dict__, q, event_q), daemon=True)
        p.start()
        processes.append(p)
    asyncio.create_task(event_pump())

@app.on_event("shutdown")
async def shutdown() -> None:
    for q in cmd_queues.values():
        q.put({"type": "stop"})
    for p in processes:
        if p.is_alive():
            p.terminate()

@app.get("/")
def index():
    return FileResponse(ROOT / "web" / "index.html")

@app.get("/camera-gateway")
def camera_gateway():
    return FileResponse(ROOT / "web" / "camera_gateway.html")

@app.get("/api/robots")
def robots():
    return {"robots": [robot_payload(r) for r in ROBOTS]}


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "robots": [robot_payload(r) for r in ROBOTS],
        "processes": [
            {"pid": p.pid, "alive": p.is_alive(), "exitcode": p.exitcode}
            for p in processes
        ],
        "latest": {
            robot_id: {
                "status": latest_status.get(robot_id),
                "error": latest_error.get(robot_id),
                "map": {
                    "received": robot_id in latest_map,
                    "width": latest_map.get(robot_id, {}).get("width"),
                    "height": latest_map.get(robot_id, {}).get("height"),
                    "stamp": latest_map.get(robot_id, {}).get("stamp"),
                },
                "pose": {
                    "received": robot_id in latest_pose,
                    "x": latest_pose.get(robot_id, {}).get("x"),
                    "y": latest_pose.get(robot_id, {}).get("y"),
                    "source": latest_pose.get(robot_id, {}).get("source"),
                    "approximate": latest_pose.get(robot_id, {}).get("approximate"),
                    "stamp": latest_pose.get(robot_id, {}).get("stamp"),
                },
                "watchdog": latest_watchdog.get(robot_id),
                "camera_settings": latest_camera_settings.get(robot_id),
            }
            for robot_id in ROBOT_BY_ID
        },
    }

@app.get("/api/skyway/token")
def skyway_token(member: str = "operator"):
    # Backward-compatible endpoint for older local pages. Prefer /api/skyway-token.
    try:
        robot = ROBOTS[0]
        token = create_skyway_token(
            app_id=os.environ["SKYWAY_APP_ID"],
            secret_key=os.environ["SKYWAY_SECRET_KEY"],
            room_name=skyway_room_for(robot),
            member_name=member,
            can_publish=member.startswith("camera"),
            can_subscribe=member.startswith("operator"),
        )
        return {"token": token, "room": skyway_room_for(robot), "member": member}
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except KeyError as e:
        raise HTTPException(status_code=500, detail=f"{e.args[0]} is required")


@app.get("/api/skyway-token")
def scoped_skyway_token(
    robot_id: str = Query(...),
    role: str = Query(..., pattern="^(camera_gateway|operator)$"),
):
    robot = ROBOT_BY_ID.get(robot_id)
    if not robot:
        raise HTTPException(status_code=404, detail=f"unknown robot_id: {robot_id}")
    try:
        member_name = f"{role}-{robot.id}-{uuid.uuid4().hex[:8]}"
        token = create_skyway_token(
            app_id=os.environ["SKYWAY_APP_ID"],
            secret_key=os.environ["SKYWAY_SECRET_KEY"],
            room_name=skyway_room_for(robot),
            member_name=member_name,
            can_publish=(role == "camera_gateway"),
            can_subscribe=(role == "operator"),
        )
        return {"token": token, "room": skyway_room_for(robot), "member": member_name}
    except KeyError as e:
        raise HTTPException(status_code=500, detail=f"{e.args[0]} is required")


async def broadcast(evt: dict[str, Any]) -> None:
    dead = []
    for ws in list(clients):
        try:
            await ws.send_json(evt)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    await ws.send_json({"type": "robots", "robots": [robot_payload(r) for r in ROBOTS]})
    for robot_id in ROBOT_BY_ID:
        if robot_id in latest_map:
            await ws.send_json(latest_map[robot_id])
        if robot_id in latest_pose:
            await ws.send_json(latest_pose[robot_id])
        if robot_id in latest_watchdog:
            await ws.send_json(latest_watchdog[robot_id])
        if robot_id in latest_camera_settings:
            await ws.send_json(latest_camera_settings[robot_id])
    try:
        while True:
            msg = await ws.receive_json()
            typ = msg.get("type")
            robot_id = msg.get("robot_id")
            if robot_id not in ROBOT_BY_ID:
                await ws.send_json({"type": "error", "message": f"unknown robot_id: {robot_id}"})
                continue
            if typ == "cmd":
                cmd_queues[robot_id].put(msg)
            elif typ == "stop":
                cmd_queues[robot_id].put({"type": "stop"})
            elif typ == "initial_pose":
                cmd_queues[robot_id].put(msg)
            elif typ == "watchdog":
                cmd_queues[robot_id].put(msg)
            elif typ == "camera_settings":
                r = ROBOT_BY_ID[robot_id]
                zmin = float(getattr(r, "camera_zoom_min", 1.0))
                zmax = float(getattr(r, "camera_zoom_max", 3.0))
                bmin = float(getattr(r, "camera_brightness_min", 0.5))
                bmax = float(getattr(r, "camera_brightness_max", 1.5))
                zoom = min(max(float(msg.get("zoom", getattr(r, "camera_zoom_default", 1.0))), zmin), zmax)
                brightness = min(max(float(msg.get("brightness", getattr(r, "camera_brightness_default", 1.0))), bmin), bmax)
                evt = {
                    "type": "camera_settings",
                    "robot_id": robot_id,
                    "zoom": zoom,
                    "brightness": brightness,
                    "source": msg.get("source", "web"),
                }
                latest_camera_settings[robot_id] = evt
                await broadcast(evt)
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)
        for q in cmd_queues.values():
            q.put({"type": "stop"})

async def event_pump() -> None:
    while True:
        try:
            evt: dict[str, Any] = event_q.get_nowait()
        except Exception:
            await asyncio.sleep(0.01)
            continue
        robot_id = evt.get("robot_id")
        if evt.get("type") == "map" and robot_id:
            latest_map[robot_id] = evt
        elif evt.get("type") == "pose" and robot_id:
            latest_pose[robot_id] = evt
        elif evt.get("type") == "status" and robot_id:
            latest_status[robot_id] = evt
        elif evt.get("type") == "error" and robot_id:
            latest_error[robot_id] = evt
        elif evt.get("type") == "watchdog" and robot_id:
            latest_watchdog[robot_id] = evt
        elif evt.get("type") == "camera_settings" and robot_id:
            latest_camera_settings[robot_id] = evt
        await broadcast(evt)

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    host = os.environ.get("WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("WEB_PORT", "8080"))
    uvicorn.run("server.run_server:app", host=host, port=port, reload=False)
