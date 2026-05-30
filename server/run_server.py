from __future__ import annotations
import asyncio, os, sys, uuid
from pathlib import Path
from multiprocessing import Process, Queue
from typing import Any

import base64
import cv2
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.config_loader import load_config, RobotConfig
from server.ros_worker import run_ros_worker
from server.skyway_token import create_skyway_token

CONFIG_PATH = Path(os.environ.get("ROBOTS_CONFIG", ROOT / "config" / "robots.yaml"))
ROBOTS = load_config(CONFIG_PATH)
ROBOT_BY_ID = {r.id: r for r in ROBOTS}
APP_MODE = os.environ.get("APP_MODE", "local").strip().lower()
AZURE_BFF_MODE = APP_MODE == "azure_bff"
EDGE_TIMEOUT_SEC = float(os.environ.get("FACTORY_EDGE_TIMEOUT_SEC", "3.0"))

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

app = FastAPI(title=f"Lightrover Web Teleop ({APP_MODE})")
app.mount("/static", StaticFiles(directory=str(ROOT / "web")), name="static")


class CmdVelRequest(BaseModel):
    linear_x: float = Field(default=0.0, ge=-1.0, le=1.0)
    angular_z: float = Field(default=0.0, ge=-3.14, le=3.14)
    duration_ms: int = Field(default=200, ge=0, le=5000)


class CameraSettingsRequest(BaseModel):
    zoom: float = 1.0
    brightness: float = 1.0
    source: str = "api"


class WatchdogRequest(BaseModel):
    enabled: bool = True


class InitialPoseRequest(BaseModel):
    x: float = 0.0
    y: float = 0.0
    yaw: float = 0.0


class NavGoalRequest(BaseModel):
    x: float
    y: float
    yaw: float = 0.0


def skyway_room_for(robot: RobotConfig) -> str:
    explicit_room = getattr(robot, "skyway_room", None)
    if explicit_room:
        return explicit_room
    room_prefix = os.environ.get("SKYWAY_ROOM_PREFIX", "lightrover")
    return f"{room_prefix}-{robot.id}"


def edge_base_url_for(robot: RobotConfig) -> str:
    url = os.environ.get("FACTORY_EDGE_BASE_URL") or getattr(robot, "edge_base_url", None)
    if not url:
        raise HTTPException(
            status_code=500,
            detail="FACTORY_EDGE_BASE_URL or robot.edge_base_url is required in azure_bff mode",
        )
    return url.rstrip("/")


async def edge_request(
    robot_id: str,
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    robot = ROBOT_BY_ID.get(robot_id)
    if not robot:
        raise HTTPException(status_code=404, detail=f"unknown robot_id: {robot_id}")
    url = f"{edge_base_url_for(robot)}{path}"
    try:
        async with httpx.AsyncClient(timeout=EDGE_TIMEOUT_SEC) as client:
            response = await client.request(method, url, json=json)
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=e.response.status_code,
            detail=f"factory edge returned {e.response.status_code}: {e.response.text}",
        )
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"factory edge unreachable: {e}") from e
    if not response.content:
        return {"ok": True}
    return response.json()


def cmd_vel_from_direction(robot: RobotConfig, direction: str) -> CmdVelRequest:
    linear_speed = float(getattr(robot, "linear_speed", 0.18))
    angular_speed = float(getattr(robot, "angular_speed", 0.7))
    if direction == "forward":
        return CmdVelRequest(linear_x=linear_speed, angular_z=0.0, duration_ms=200)
    if direction == "backward":
        return CmdVelRequest(linear_x=-linear_speed, angular_z=0.0, duration_ms=200)
    if direction == "left":
        return CmdVelRequest(linear_x=0.0, angular_z=angular_speed, duration_ms=200)
    if direction == "right":
        return CmdVelRequest(linear_x=0.0, angular_z=-angular_speed, duration_ms=200)
    return CmdVelRequest(linear_x=0.0, angular_z=0.0, duration_ms=0)


async def send_edge_event(robot_id: str, msg: dict[str, Any]) -> dict[str, Any] | None:
    robot = ROBOT_BY_ID[robot_id]
    typ = msg.get("type")
    if typ == "cmd":
        cmd = cmd_vel_from_direction(robot, str(msg.get("direction", "stop")))
        return await edge_request(robot_id, "POST", f"/api/robots/{robot_id}/cmd_vel", json=cmd.model_dump())
    if typ == "stop":
        return await edge_request(robot_id, "POST", f"/api/robots/{robot_id}/stop")
    if typ == "initial_pose":
        pose = InitialPoseRequest(x=float(msg.get("x", 0.0)), y=float(msg.get("y", 0.0)), yaw=float(msg.get("yaw", 0.0)))
        return await edge_request(robot_id, "POST", f"/api/robots/{robot_id}/initial_pose", json=pose.model_dump())
    if typ == "watchdog":
        watchdog = WatchdogRequest(enabled=bool(msg.get("enabled", True)))
        return await edge_request(robot_id, "POST", f"/api/robots/{robot_id}/watchdog", json=watchdog.model_dump())
    if typ == "camera_settings":
        settings = clamp_camera_settings(robot_id, msg)
        return await edge_request(
            robot_id,
            "POST",
            f"/api/robots/{robot_id}/camera/settings",
            json={
                "zoom": settings["zoom"],
                "brightness": settings["brightness"],
                "source": settings["source"],
            },
        )
    return None


def clamp_camera_settings(robot_id: str, msg: dict[str, Any]) -> dict[str, Any]:
    r = ROBOT_BY_ID[robot_id]
    zmin = float(getattr(r, "camera_zoom_min", 1.0))
    zmax = float(getattr(r, "camera_zoom_max", 3.0))
    bmin = float(getattr(r, "camera_brightness_min", 0.5))
    bmax = float(getattr(r, "camera_brightness_max", 1.5))
    zoom = min(max(float(msg.get("zoom", getattr(r, "camera_zoom_default", 1.0))), zmin), zmax)
    brightness = min(max(float(msg.get("brightness", getattr(r, "camera_brightness_default", 1.0))), bmin), bmax)
    return {
        "type": "camera_settings",
        "robot_id": robot_id,
        "zoom": zoom,
        "brightness": brightness,
        "source": msg.get("source", "web"),
    }


def robot_payload(robot: RobotConfig) -> dict[str, Any]:
    payload = robot.__dict__.copy()
    payload.setdefault("label", payload.get("name", robot.id))
    payload["skyway_room"] = skyway_room_for(robot)
    if AZURE_BFF_MODE:
        payload["edge_base_url"] = edge_base_url_for(robot)
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
    if not AZURE_BFF_MODE:
        for r in ROBOTS:
            q: Queue = Queue()
            cmd_queues[r.id] = q
            p = Process(target=run_ros_worker, args=(r.__dict__, q, event_q), daemon=True)
            p.start()
            processes.append(p)
    asyncio.create_task(event_pump())
    if AZURE_BFF_MODE:
        asyncio.create_task(edge_status_pump())

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
        "mode": APP_MODE,
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


@app.get("/api/robots/{robot_id}/status")
async def robot_status(robot_id: str):
    if AZURE_BFF_MODE:
        return await edge_request(robot_id, "GET", f"/api/robots/{robot_id}/status")
    if robot_id not in ROBOT_BY_ID:
        raise HTTPException(status_code=404, detail=f"unknown robot_id: {robot_id}")
    return {
        "status": "ok",
        "robot_id": robot_id,
        "latest": {
            "status": latest_status.get(robot_id),
            "error": latest_error.get(robot_id),
            "pose": latest_pose.get(robot_id),
            "watchdog": latest_watchdog.get(robot_id),
            "camera_settings": latest_camera_settings.get(robot_id),
        },
    }


@app.post("/api/robots/{robot_id}/cmd_vel")
async def cmd_vel(robot_id: str, cmd: CmdVelRequest):
    if AZURE_BFF_MODE:
        return await edge_request(robot_id, "POST", f"/api/robots/{robot_id}/cmd_vel", json=cmd.model_dump())
    robot = ROBOT_BY_ID.get(robot_id)
    if not robot:
        raise HTTPException(status_code=404, detail=f"unknown robot_id: {robot_id}")
    # HTTP cmd_vel is primarily for Azure mode; local mode maps directly to the ROS worker queue.
    cmd_queues[robot_id].put({"type": "raw_cmd", **cmd.model_dump()})
    return {"status": "queued", "robot_id": robot_id, **cmd.model_dump()}


@app.post("/api/robots/{robot_id}/stop")
async def stop_robot(robot_id: str):
    if AZURE_BFF_MODE:
        return await edge_request(robot_id, "POST", f"/api/robots/{robot_id}/stop")
    if robot_id not in ROBOT_BY_ID:
        raise HTTPException(status_code=404, detail=f"unknown robot_id: {robot_id}")
    cmd_queues[robot_id].put({"type": "stop"})
    return {"status": "stopped", "robot_id": robot_id}


@app.post("/api/robots/{robot_id}/camera/settings")
async def camera_settings(robot_id: str, settings: CameraSettingsRequest):
    if robot_id not in ROBOT_BY_ID:
        raise HTTPException(status_code=404, detail=f"unknown robot_id: {robot_id}")
    evt = clamp_camera_settings(robot_id, {"type": "camera_settings", **settings.model_dump()})
    latest_camera_settings[robot_id] = evt
    await broadcast(evt)
    if AZURE_BFF_MODE:
        await edge_request(
            robot_id,
            "POST",
            f"/api/robots/{robot_id}/camera/settings",
            json={"zoom": evt["zoom"], "brightness": evt["brightness"], "source": evt["source"]},
        )
    return evt


@app.post("/api/robots/{robot_id}/nav_goal")
async def nav_goal(robot_id: str, goal: NavGoalRequest):
    if AZURE_BFF_MODE:
        return await edge_request(robot_id, "POST", f"/api/robots/{robot_id}/nav_goal", json=goal.model_dump())
    raise HTTPException(status_code=501, detail="nav_goal is only proxied in azure_bff mode")

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
                if AZURE_BFF_MODE:
                    await send_edge_event(robot_id, msg)
                else:
                    cmd_queues[robot_id].put(msg)
            elif typ == "stop":
                if AZURE_BFF_MODE:
                    await send_edge_event(robot_id, {"type": "stop"})
                else:
                    cmd_queues[robot_id].put({"type": "stop"})
            elif typ == "initial_pose":
                if AZURE_BFF_MODE:
                    await send_edge_event(robot_id, msg)
                else:
                    cmd_queues[robot_id].put(msg)
            elif typ == "watchdog":
                if AZURE_BFF_MODE:
                    await send_edge_event(robot_id, msg)
                else:
                    cmd_queues[robot_id].put(msg)
            elif typ == "camera_settings":
                evt = clamp_camera_settings(robot_id, msg)
                latest_camera_settings[robot_id] = evt
                await broadcast(evt)
                if AZURE_BFF_MODE:
                    await send_edge_event(robot_id, msg)
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)
        if AZURE_BFF_MODE:
            for robot_id in ROBOT_BY_ID:
                try:
                    await send_edge_event(robot_id, {"type": "stop"})
                except HTTPException:
                    pass
        else:
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


async def edge_status_pump() -> None:
    while True:
        for robot_id in ROBOT_BY_ID:
            try:
                status = await edge_request(robot_id, "GET", f"/api/robots/{robot_id}/status")
            except HTTPException as e:
                await broadcast({
                    "type": "error",
                    "robot_id": robot_id,
                    "message": str(e.detail),
                })
                continue
            for key in ("status", "pose", "watchdog", "camera_settings", "map"):
                evt = status.get(key)
                if isinstance(evt, dict):
                    evt.setdefault("robot_id", robot_id)
                    if "type" not in evt:
                        evt["type"] = key
                    event_q.put(evt)
        await asyncio.sleep(float(os.environ.get("FACTORY_EDGE_POLL_SEC", "0.5")))

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    host = os.environ.get("WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("WEB_PORT", "8080"))
    uvicorn.run("server.run_server:app", host=host, port=port, reload=False)
