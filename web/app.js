import { SkyWayContext, SkyWayRoom } from 'https://cdn.jsdelivr.net/npm/@skyway-sdk/room/+esm';

let ws;
let robots = [];
let currentRobot = null;
let repeatTimer = null;
let lastMap = null;
let lastPose = null;
let watchdogStates = {};
let mapCount = 0;
let setPoseMode = false;
let poseDragStart = null;
let poseDragEnd = null;
let lastMapLayout = null;
let mapImage = null;
let pendingMapSrc = null;
let drawScheduled = false;
let mapView = { zoom: 1.0, panX: 0, panY: 0, rotation: 0 };
let mapViewByRobot = {};
let mapPanDrag = null;
let mapRotateDrag = null;
const MAP_ZOOM_MIN = 0.25;
const MAP_ZOOM_MAX = 8.0;
const MAP_ZOOM_STEP = 1.25;
const MAP_ZOOM_SLIDER_STEP = 0.05;
const MAP_ROTATION_DRAG_SCALE = 0.01;
let skyJoined = false;
let skyContext = null;
let skyRoom = null;
let skyMember = null;
let skyRemoteStream = null;
let cameraSettings = { zoom: 1.0, brightness: 1.0 };
let cameraSettingsByRobot = {};
let suppressCameraSliderSend = false;
let audioSettings = { volume: 1.0, muted: false };

const $ = (id) => document.getElementById(id);

function currentRobotConfig() {
  return robots.find((robot) => robot.id === currentRobot);
}

function cameraControlsFor(robot) {
  const c = robot?.camera_controls || {};
  return {
    zoom: { default: 1.0, min: 1.0, max: 3.0, step: 0.1, ...(c.zoom || {}) },
    brightness: { default: 1.0, min: 0.5, max: 1.5, step: 0.05, ...(c.brightness || {}) },
  };
}

function clampNumber(value, min, max) {
  return Math.min(Math.max(Number(value), Number(min)), Number(max));
}

function formatZoom(value) {
  return `${Number(value).toFixed(1)}x`;
}

function formatBrightness(value) {
  return `${Math.round(Number(value) * 100)}%`;
}

function timestampForFilename(date = new Date()) {
  const pad = (value) => String(value).padStart(2, '0');
  return [
    date.getFullYear(),
    pad(date.getMonth() + 1),
    pad(date.getDate()),
    '_',
    pad(date.getHours()),
    pad(date.getMinutes()),
    pad(date.getSeconds()),
  ].join('');
}

function updateCameraControlLabels() {
  $('zoomValue').textContent = formatZoom(cameraSettings.zoom);
  $('brightnessValue').textContent = formatBrightness(cameraSettings.brightness);
  $('cameraSettingsState').textContent = `camera: zoom=${formatZoom(cameraSettings.zoom)} brightness=${formatBrightness(cameraSettings.brightness)}`;
}

function updateAudioControls() {
  const volumePercent = Math.round(audioSettings.volume * 100);
  $('volumeValue').textContent = `${volumePercent}%`;
  $('volumeSlider').value = String(volumePercent);
  $('muteToggle').checked = audioSettings.muted;
  const video = $('remoteVideo');
  video.volume = audioSettings.volume;
  video.muted = audioSettings.muted;
}

function videoSourceLabel(publisherName, publisherMetadata = '') {
  if (publisherName.startsWith('skyway_ros_bridge-') || publisherMetadata.includes('skyway-ros-bridge')) {
    return `V9 skyway_ros_bridge (${publisherName || 'unknown'})`;
  }
  if (publisherName.startsWith('camera_gateway-')) {
    return `V8 camera_gateway (${publisherName})`;
  }
  return `unknown (${publisherName || 'no publisher name'})`;
}

function applyCameraControlRanges() {
  const robot = currentRobotConfig();
  if (!robot) return;
  const c = cameraControlsFor(robot);
  const saved = cameraSettingsByRobot[currentRobot] || {
    zoom: c.zoom.default,
    brightness: c.brightness.default,
  };
  cameraSettings = {
    zoom: clampNumber(saved.zoom, c.zoom.min, c.zoom.max),
    brightness: clampNumber(saved.brightness, c.brightness.min, c.brightness.max),
  };
  suppressCameraSliderSend = true;
  $('zoomSlider').min = c.zoom.min;
  $('zoomSlider').max = c.zoom.max;
  $('zoomSlider').step = c.zoom.step;
  $('zoomSlider').value = cameraSettings.zoom;
  $('brightnessSlider').min = c.brightness.min;
  $('brightnessSlider').max = c.brightness.max;
  $('brightnessSlider').step = c.brightness.step;
  $('brightnessSlider').value = cameraSettings.brightness;
  updateCameraControlLabels();
  suppressCameraSliderSend = false;
}

function sendCameraSettings(source = 'operator_slider') {
  if (!currentRobot) return;
  const robot = currentRobotConfig();
  const c = cameraControlsFor(robot);
  cameraSettings = {
    zoom: clampNumber(cameraSettings.zoom, c.zoom.min, c.zoom.max),
    brightness: clampNumber(cameraSettings.brightness, c.brightness.min, c.brightness.max),
  };
  cameraSettingsByRobot[currentRobot] = { ...cameraSettings };
  updateCameraControlLabels();
  send({ type: 'camera_settings', ...cameraSettings, source });
}

function applyRemoteCameraSettings(message) {
  if (!message.robot_id) return;
  cameraSettingsByRobot[message.robot_id] = {
    zoom: Number(message.zoom),
    brightness: Number(message.brightness),
  };
  if (message.robot_id !== currentRobot) return;
  const robot = currentRobotConfig();
  const c = cameraControlsFor(robot);
  cameraSettings = {
    zoom: clampNumber(message.zoom, c.zoom.min, c.zoom.max),
    brightness: clampNumber(message.brightness, c.brightness.min, c.brightness.max),
  };
  suppressCameraSliderSend = true;
  $('zoomSlider').value = cameraSettings.zoom;
  $('brightnessSlider').value = cameraSettings.brightness;
  updateCameraControlLabels();
  suppressCameraSliderSend = false;
}

function connectWs() {
  ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
  ws.onopen = () => {
    $('wsState').textContent = 'WS: connected';
  };
  ws.onclose = () => {
    $('wsState').textContent = 'WS: disconnected';
    setTimeout(connectWs, 1000);
  };
  ws.onmessage = (event) => {
    const message = JSON.parse(event.data);
    if (message.type === 'robots') {
      robots = message.robots;
      fillRobots();
    }
    if (message.type === 'map' && message.robot_id === currentRobot) {
      lastMap = message;
      mapCount += 1;
      $('mapStatus').textContent = `map: received ${message.width}x${message.height} count=${mapCount}`;
      scheduleDrawMap();
    }
    if (message.type === 'pose' && message.robot_id === currentRobot) {
      if (message.approximate && lastPose && !lastPose.approximate) return;
      lastPose = message;
      scheduleDrawMap();
    }
    if (message.type === 'status') {
      $('robotState').textContent = `${message.robot_id}: ${message.message}`;
    }
    if (message.type === 'watchdog') {
      watchdogStates[message.robot_id] = message;
      if (message.robot_id === currentRobot) updateWatchdogUi();
    }
    if (message.type === 'error') {
      $('robotState').textContent = `${message.robot_id || 'server'}: ${message.message}`;
    }
    if (message.type === 'camera_settings') {
      applyRemoteCameraSettings(message);
    }
  };
}

function fillRobots() {
  const select = $('robotSelect');
  select.innerHTML = '';
  for (const robot of robots) {
    const option = document.createElement('option');
    option.value = robot.id;
    option.textContent = `${robot.name || robot.label || robot.id} (domain ${robot.ros_domain_id})`;
    select.appendChild(option);
    if (!cameraSettingsByRobot[robot.id]) {
      const c = cameraControlsFor(robot);
      cameraSettingsByRobot[robot.id] = { zoom: c.zoom.default, brightness: c.brightness.default };
    }
    if (!watchdogStates[robot.id]) {
      watchdogStates[robot.id] = {
        robot_id: robot.id,
        enabled: robot.watchdog_enabled !== false,
        remote: 'config',
        timeout_sec: robot.watchdog_timeout_sec,
      };
    }
  }
  currentRobot = select.value;
  applyCameraControlRanges();
  updateWatchdogUi();
  select.onchange = () => {
    stop();
    disconnectSkyway();
    currentRobot = select.value;
    $('robotState').textContent = `Robot: ${currentRobot}`;
    lastMap = null;
    lastPose = null;
    loadMapViewForRobot();
    mapCount = 0;
    $('mapStatus').textContent = 'map: waiting';
    applyCameraControlRanges();
    updateWatchdogUi();
    scheduleDrawMap();
  };
}

function send(payload) {
  if (ws && ws.readyState === WebSocket.OPEN && currentRobot) {
    ws.send(JSON.stringify({ ...payload, robot_id: currentRobot }));
  }
}

function sendInitialPose(x, y, yaw) {
  send({ type: 'initial_pose', x, y, yaw });
  lastPose = {
    type: 'pose',
    robot_id: currentRobot,
    x,
    y,
    yaw,
    source: 'initial_pose',
    approximate: false,
  };
  $('poseText').textContent = `initial pose sent: x=${x.toFixed(2)} y=${y.toFixed(2)} yaw=${yaw.toFixed(2)} rad`;
  scheduleDrawMap();
}


function normalizeAngle(rad) {
  let value = Number(rad) || 0;
  while (value > Math.PI) value -= Math.PI * 2;
  while (value <= -Math.PI) value += Math.PI * 2;
  return value;
}

function saveMapViewForRobot() {
  if (!currentRobot) return;
  mapViewByRobot[currentRobot] = { ...mapView };
}

function loadMapViewForRobot() {
  mapView = { ...(mapViewByRobot[currentRobot] || { zoom: 1.0, panX: 0, panY: 0, rotation: 0 }) };
  updateMapViewUi();
}

function updateMapViewUi() {
  const zoomText = $('mapZoomValue');
  const zoomSlider = $('mapZoomSlider');
  const rotationText = $('mapRotationValue');
  const status = $('mapViewStatus');
  if (!zoomText || !zoomSlider || !rotationText || !status) return;
  zoomText.textContent = `${mapView.zoom.toFixed(2)}x`;
  zoomSlider.value = mapView.zoom.toFixed(2);
  rotationText.textContent = `${Math.round(mapView.rotation * 180 / Math.PI)}°`;
  status.textContent = `view: zoom=${mapView.zoom.toFixed(2)}x pan=(${Math.round(mapView.panX)}, ${Math.round(mapView.panY)}) rotate=${Math.round(mapView.rotation * 180 / Math.PI)}°`;
}

function applyMapView(nextView) {
  mapView = {
    zoom: clampNumber(nextView.zoom ?? mapView.zoom, MAP_ZOOM_MIN, MAP_ZOOM_MAX),
    panX: Number(nextView.panX ?? mapView.panX) || 0,
    panY: Number(nextView.panY ?? mapView.panY) || 0,
    rotation: normalizeAngle(nextView.rotation ?? mapView.rotation),
  };
  saveMapViewForRobot();
  updateMapViewUi();
  scheduleDrawMap();
}

function zoomMapAtCanvasPoint(nextZoom, canvasX = null, canvasY = null) {
  if (!lastMapLayout || !lastMap) {
    applyMapView({ zoom: nextZoom });
    return;
  }
  const canvas = $('mapCanvas');
  const focusX = canvasX ?? canvas.width / 2;
  const focusY = canvasY ?? canvas.height / 2;
  const before = mapPixelFromCanvasPoint(focusX, focusY);
  const oldView = { ...mapView };
  mapView.zoom = clampNumber(nextZoom, MAP_ZOOM_MIN, MAP_ZOOM_MAX);
  const afterLayout = computeMapLayout();
  const after = before ? canvasPointFromMapPixel(before.x, before.y, afterLayout) : null;
  if (after) {
    mapView.panX += focusX - after.x;
    mapView.panY += focusY - after.y;
  }
  applyMapView({ ...mapView });
  if (!before) applyMapView({ ...oldView, zoom: mapView.zoom });
}

function startMove(direction) {
  stop(false);
  send({ type: 'cmd', direction });
  repeatTimer = setInterval(() => send({ type: 'cmd', direction }), 70);
}

function stop(sendStop = true) {
  if (repeatTimer) {
    clearInterval(repeatTimer);
    repeatTimer = null;
  }
  if (sendStop) send({ type: 'stop' });
}

function updateWatchdogUi() {
  const state = watchdogStates[currentRobot];
  const toggle = $('watchdogToggle');
  toggle.disabled = !currentRobot;
  toggle.checked = state ? Boolean(state.enabled) : true;
  if (!currentRobot) {
    $('watchdogState').textContent = 'watchdog: -';
    return;
  }
  if (!state) {
    $('watchdogState').textContent = 'watchdog: loading';
    return;
  }
  const remote = state.remote ? ` remote=${state.remote}` : '';
  const timeout = Number.isFinite(state.timeout_sec) ? ` timeout=${state.timeout_sec.toFixed(2)}s` : '';
  $('watchdogState').textContent = `watchdog: ${state.enabled ? 'ON' : 'OFF'}${remote}${timeout}`;
}

$('watchdogToggle').onchange = () => {
  const enabled = $('watchdogToggle').checked;
  watchdogStates[currentRobot] = {
    ...(watchdogStates[currentRobot] || {}),
    robot_id: currentRobot,
    enabled,
    remote: 'sending',
  };
  updateWatchdogUi();
  stop();
  send({ type: 'watchdog', enabled });
};

for (const button of document.querySelectorAll('.move')) {
  button.addEventListener('pointerdown', (event) => {
    event.preventDefault();
    button.setPointerCapture(event.pointerId);
    startMove(button.dataset.dir);
  });
  button.addEventListener('pointerup', () => stop());
  button.addEventListener('pointercancel', () => stop());
  button.addEventListener('pointerleave', () => stop());
}

$('stopBtn').onclick = () => stop();
$('capturePhotoBtn').onclick = () => capturePhoto();
window.addEventListener('blur', () => stop());
window.addEventListener('beforeunload', () => stop());

function scheduleDrawMap() {
  if (drawScheduled) return;
  drawScheduled = true;
  requestAnimationFrame(() => {
    drawScheduled = false;
    drawMap();
  });
}

function ensureMapImage() {
  if (!lastMap) return;
  const src = `data:image/png;base64,${lastMap.data}`;
  if (mapImage && pendingMapSrc === src) return;
  pendingMapSrc = src;
  const img = new Image();
  img.onload = () => {
    if (pendingMapSrc !== src) return;
    mapImage = img;
    scheduleDrawMap();
  };
  img.src = src;
}

function drawMap() {
  const canvas = $('mapCanvas');
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = '#111';
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  if (!lastMap) {
    ctx.fillStyle = '#ddd';
    ctx.fillText('waiting /map ...', 20, 30);
    $('mapStatus').textContent = `map: waiting for ${currentRobot || '-'}`;
    return;
  }

  ensureMapImage();
  if (!mapImage) {
    ctx.fillStyle = '#ddd';
    ctx.fillText('loading map ...', 20, 30);
    return;
  }

  lastMapLayout = computeMapLayout(canvas);
  ctx.save();
  ctx.translate(lastMapLayout.centerX, lastMapLayout.centerY);
  ctx.rotate(lastMapLayout.rotation);
  ctx.scale(lastMapLayout.scale, lastMapLayout.scale);
  ctx.drawImage(mapImage, -mapImage.width / 2, -mapImage.height / 2);
  ctx.restore();

  drawMapCrosshair(ctx);

  if (lastPose && lastPose.robot_id === currentRobot) {
    drawPoseArrow(ctx, lastPose, lastPose.approximate ? '#f59e0b' : 'red');
    const source = lastPose.source || lastPose.frame_id || 'tf';
    const note = lastPose.approximate ? ' approximate' : '';
    $('poseText').textContent = `pose: x=${lastPose.x.toFixed(2)} y=${lastPose.y.toFixed(2)} yaw=${lastPose.yaw.toFixed(2)} rad source=${source}${note}`;
  }

  if (setPoseMode && poseDragStart && poseDragEnd) {
    const yaw = Math.atan2(poseDragEnd.y - poseDragStart.y, poseDragEnd.x - poseDragStart.x);
    drawPoseArrow(ctx, { ...poseDragStart, yaw }, '#2563eb');
  }
}

function computeMapLayout(canvas = $('mapCanvas')) {
  const baseScale = mapImage ? Math.min(canvas.width / mapImage.width, canvas.height / mapImage.height) : 1;
  return {
    scale: baseScale * mapView.zoom,
    baseScale,
    centerX: canvas.width / 2 + mapView.panX,
    centerY: canvas.height / 2 + mapView.panY,
    rotation: mapView.rotation,
    width: mapImage?.width || lastMap?.width || 0,
    height: mapImage?.height || lastMap?.height || 0,
  };
}

function canvasPointFromMapPixel(mapPixelX, mapPixelY, layout = lastMapLayout) {
  if (!layout) return null;
  const localX = (mapPixelX - layout.width / 2) * layout.scale;
  const localY = (mapPixelY - layout.height / 2) * layout.scale;
  const cos = Math.cos(layout.rotation);
  const sin = Math.sin(layout.rotation);
  return {
    x: layout.centerX + localX * cos - localY * sin,
    y: layout.centerY + localX * sin + localY * cos,
  };
}

function mapPixelFromCanvasPoint(canvasX, canvasY, layout = lastMapLayout) {
  if (!layout) return null;
  const dx = canvasX - layout.centerX;
  const dy = canvasY - layout.centerY;
  const cos = Math.cos(-layout.rotation);
  const sin = Math.sin(-layout.rotation);
  const localX = dx * cos - dy * sin;
  const localY = dx * sin + dy * cos;
  return {
    x: localX / layout.scale + layout.width / 2,
    y: localY / layout.scale + layout.height / 2,
  };
}

function canvasPointFromMapPoint(point) {
  if (!lastMap || !lastMapLayout) return null;
  const poseX = (point.x - lastMap.origin.x) / lastMap.resolution;
  const poseY = lastMap.height - (point.y - lastMap.origin.y) / lastMap.resolution;
  return canvasPointFromMapPixel(poseX, poseY);
}

function drawMapCrosshair(ctx) {
  if (!lastMapLayout) return;
  ctx.save();
  ctx.strokeStyle = 'rgba(255,255,255,0.22)';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(lastMapLayout.centerX - 10, lastMapLayout.centerY);
  ctx.lineTo(lastMapLayout.centerX + 10, lastMapLayout.centerY);
  ctx.moveTo(lastMapLayout.centerX, lastMapLayout.centerY - 10);
  ctx.lineTo(lastMapLayout.centerX, lastMapLayout.centerY + 10);
  ctx.stroke();
  ctx.restore();
}

function drawPoseArrow(ctx, pose, color) {
  const point = canvasPointFromMapPoint(pose);
  if (!point) return;
  ctx.save();
  ctx.translate(point.x, point.y);
  ctx.rotate((lastMapLayout?.rotation || 0) - pose.yaw);
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.moveTo(18, 0);
  ctx.lineTo(-11, 10);
  ctx.lineTo(-11, -10);
  ctx.closePath();
  ctx.fill();
  ctx.strokeStyle = 'white';
  ctx.lineWidth = 2;
  ctx.stroke();
  ctx.restore();
}

function mapPointFromCanvasEvent(event) {
  if (!lastMap || !lastMapLayout) return null;
  const canvas = $('mapCanvas');
  const rect = canvas.getBoundingClientRect();
  const canvasX = (event.clientX - rect.left) * (canvas.width / rect.width);
  const canvasY = (event.clientY - rect.top) * (canvas.height / rect.height);
  const mapPixel = mapPixelFromCanvasPoint(canvasX, canvasY);
  if (!mapPixel) return null;
  if (mapPixel.x < 0 || mapPixel.y < 0 || mapPixel.x > lastMap.width || mapPixel.y > lastMap.height) return null;
  const origin = lastMap.origin;
  return {
    x: origin.x + mapPixel.x * lastMap.resolution,
    y: origin.y + (lastMap.height - mapPixel.y) * lastMap.resolution,
  };
}

function canvasCoordsFromPointerEvent(event) {
  const canvas = $('mapCanvas');
  const rect = canvas.getBoundingClientRect();
  return {
    x: (event.clientX - rect.left) * (canvas.width / rect.width),
    y: (event.clientY - rect.top) * (canvas.height / rect.height),
  };
}

function capturePhoto() {
  const video = $('remoteVideo');
  const status = $('captureStatus');
  const width = video.videoWidth;
  const height = video.videoHeight;
  if (!video.srcObject || !width || !height || video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) {
    status.textContent = 'photo: SkyWay映像を受信してから撮影してください';
    return;
  }

  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext('2d', { alpha: false });
  ctx.drawImage(video, 0, 0, width, height);
  canvas.toBlob((blob) => {
    if (!blob) {
      status.textContent = 'photo: 保存用画像の作成に失敗しました';
      return;
    }
    const filename = `${currentRobot || 'lightrover'}_${timestampForFilename()}.png`;
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    status.textContent = `photo: saved ${filename}`;
  }, 'image/png');
}

async function disconnectSkyway() {
  try { if (skyMember) await skyMember.leave(); } catch {}
  try { if (skyRoom) await skyRoom.dispose?.(); } catch {}
  try { if (skyContext) await skyContext.dispose?.(); } catch {}
  skyContext = null;
  skyRoom = null;
  skyMember = null;
  skyRemoteStream = null;
  skyJoined = false;
  $('remoteVideo').srcObject = null;
  $('skyState').textContent = 'SkyWay: disconnected';
  $('videoSourceState').textContent = 'video: -';
}

async function subscribePublication(publication, me, roomName) {
  if (!publication || !me || !me.id) return;
  const publisher = publication?.publisher;
  if (!publisher || !publisher.id) return;
  if (publisher.id === me.id) return;
  const publisherName = publisher.name || '';
  const publisherMetadata = String(publisher.metadata || '');
  const isCameraGateway = publisherName.startsWith('camera_gateway-');
  const isSkywayRosBridge = publisherName.startsWith('skyway_ros_bridge-') || publisherMetadata.includes('skyway-ros-bridge');
  if (!isCameraGateway && !isSkywayRosBridge) return;

  const { stream } = await me.subscribe(publication.id);
  const video = $('remoteVideo');
  if (!skyRemoteStream) {
    skyRemoteStream = new MediaStream();
    video.srcObject = skyRemoteStream;
    updateAudioControls();
  }
  if (stream.contentType === 'video') {
    skyRemoteStream.getVideoTracks().forEach((track) => skyRemoteStream.removeTrack(track));
    skyRemoteStream.addTrack(stream.track);
    $('skyState').textContent = `SkyWay: subscribed video ${roomName}`;
    $('videoSourceState').textContent = `video: ${videoSourceLabel(publisherName, publisherMetadata)}`;
  } else if (stream.contentType === 'audio') {
    skyRemoteStream.getAudioTracks().forEach((track) => skyRemoteStream.removeTrack(track));
    skyRemoteStream.addTrack(stream.track);
    $('skyState').textContent = `SkyWay: subscribed audio ${roomName}`;
  }
  await video.play().catch(() => {});
}

async function joinSkyway() {
  if (skyJoined) return;
  const robot = currentRobot;
  if (!robot) return;

  $('skyState').textContent = 'SkyWay: connecting';
  const tokenRes = await fetch(`/api/skyway-token?robot_id=${encodeURIComponent(robot)}&role=operator`);
  if (!tokenRes.ok) throw new Error(await tokenRes.text());
  const { token, room: roomName, member: memberName } = await tokenRes.json();

  skyContext = await SkyWayContext.Create(token);
  skyRoom = await SkyWayRoom.FindOrCreate(skyContext, { name: roomName });
  skyMember = await skyRoom.join({ name: memberName, metadata: 'operator' });

  for (const publication of skyRoom.publications) {
    await subscribePublication(publication, skyMember, roomName);
  }
  skyRoom.onStreamPublished.add(async (event) => {
    await subscribePublication(event.publication, skyMember, roomName);
  });

  skyJoined = true;
  $('skyState').textContent = `SkyWay: joined ${roomName}`;
}

$('joinSkywayBtn').onclick = () => joinSkyway().catch((error) => {
  disconnectSkyway().finally(() => {
    $('skyState').textContent = `SkyWay: error ${error}`;
  });
});

$('volumeSlider').addEventListener('input', () => {
  audioSettings.volume = clampNumber($('volumeSlider').value, 0, 100) / 100;
  updateAudioControls();
});

$('muteToggle').addEventListener('change', () => {
  audioSettings.muted = $('muteToggle').checked;
  updateAudioControls();
});

$('zoomSlider').addEventListener('input', () => {
  if (suppressCameraSliderSend) return;
  cameraSettings.zoom = Number($('zoomSlider').value);
  sendCameraSettings('operator_slider');
});

$('brightnessSlider').addEventListener('input', () => {
  if (suppressCameraSliderSend) return;
  cameraSettings.brightness = Number($('brightnessSlider').value);
  sendCameraSettings('operator_slider');
});

$('resetCameraBtn').addEventListener('click', () => {
  const robot = currentRobotConfig();
  const c = cameraControlsFor(robot);
  cameraSettings = { zoom: c.zoom.default, brightness: c.brightness.default };
  suppressCameraSliderSend = true;
  $('zoomSlider').value = cameraSettings.zoom;
  $('brightnessSlider').value = cameraSettings.brightness;
  suppressCameraSliderSend = false;
  sendCameraSettings('operator_reset');
});


$('mapZoomSlider').min = MAP_ZOOM_MIN;
$('mapZoomSlider').max = MAP_ZOOM_MAX;
$('mapZoomSlider').step = MAP_ZOOM_SLIDER_STEP;
$('mapZoomSlider').addEventListener('input', () => {
  zoomMapAtCanvasPoint(Number($('mapZoomSlider').value));
});

$('mapCanvas').addEventListener('wheel', (event) => {
  if (!lastMap) return;
  event.preventDefault();
  const point = canvasCoordsFromPointerEvent(event);
  const factor = event.deltaY < 0 ? MAP_ZOOM_STEP : 1 / MAP_ZOOM_STEP;
  zoomMapAtCanvasPoint(mapView.zoom * factor, point.x, point.y);
}, { passive: false });

$('setPoseBtn').onclick = () => {
  setPoseMode = !setPoseMode;
  poseDragStart = null;
  poseDragEnd = null;
  $('setPoseBtn').textContent = setPoseMode ? '地図上で押して、向きへドラッグして離してください' : '現在位置と向きを地図ドラッグで設定';
  $('poseText').textContent = setPoseMode ? 'initial pose mode: drag on map to set x/y/yaw' : $('poseText').textContent;
  scheduleDrawMap();
};

$('mapCanvas').addEventListener('pointerdown', (event) => {
  if (!lastMap) return;
  event.preventDefault();
  $('mapCanvas').setPointerCapture(event.pointerId);
  if (!setPoseMode) {
    const point = canvasCoordsFromPointerEvent(event);
    if (event.shiftKey) {
      mapRotateDrag = { pointerId: event.pointerId, x: point.x, rotation: mapView.rotation };
      return;
    }
    mapPanDrag = { pointerId: event.pointerId, x: point.x, y: point.y, panX: mapView.panX, panY: mapView.panY };
    return;
  }
  const point = mapPointFromCanvasEvent(event);
  if (!point) return;
  poseDragStart = point;
  poseDragEnd = point;
  scheduleDrawMap();
});

$('mapCanvas').addEventListener('pointermove', (event) => {
  if (mapRotateDrag && mapRotateDrag.pointerId === event.pointerId && !setPoseMode) {
    const point = canvasCoordsFromPointerEvent(event);
    applyMapView({ rotation: mapRotateDrag.rotation + (point.x - mapRotateDrag.x) * MAP_ROTATION_DRAG_SCALE });
    return;
  }
  if (mapPanDrag && mapPanDrag.pointerId === event.pointerId && !setPoseMode) {
    const point = canvasCoordsFromPointerEvent(event);
    applyMapView({ panX: mapPanDrag.panX + point.x - mapPanDrag.x, panY: mapPanDrag.panY + point.y - mapPanDrag.y });
    return;
  }
  if (!setPoseMode || !poseDragStart) return;
  const point = mapPointFromCanvasEvent(event);
  if (!point) return;
  poseDragEnd = point;
  scheduleDrawMap();
});

$('mapCanvas').addEventListener('pointerup', (event) => {
  if (mapRotateDrag && mapRotateDrag.pointerId === event.pointerId) {
    mapRotateDrag = null;
    return;
  }
  if (mapPanDrag && mapPanDrag.pointerId === event.pointerId) {
    mapPanDrag = null;
    return;
  }
  if (!setPoseMode || !poseDragStart) return;
  const point = mapPointFromCanvasEvent(event) || poseDragEnd || poseDragStart;
  poseDragEnd = point;
  const dx = poseDragEnd.x - poseDragStart.x;
  const dy = poseDragEnd.y - poseDragStart.y;
  const yaw = Math.hypot(dx, dy) > 0.01 ? Math.atan2(dy, dx) : (lastPose?.yaw || 0);
  sendInitialPose(poseDragStart.x, poseDragStart.y, yaw);
  setPoseMode = false;
  poseDragStart = null;
  poseDragEnd = null;
  $('setPoseBtn').textContent = '現在位置と向きを地図ドラッグで設定';
});

$('mapCanvas').addEventListener('pointercancel', (event) => {
  if (mapRotateDrag && mapRotateDrag.pointerId === event.pointerId) mapRotateDrag = null;
  if (mapPanDrag && mapPanDrag.pointerId === event.pointerId) mapPanDrag = null;
  if (!setPoseMode) return;
  poseDragStart = null;
  poseDragEnd = null;
  scheduleDrawMap();
});

updateMapViewUi();
updateAudioControls();
connectWs();
scheduleDrawMap();
