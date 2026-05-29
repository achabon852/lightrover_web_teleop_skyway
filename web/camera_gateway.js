import { SkyWayContext, SkyWayRoom, LocalAudioStream, LocalVideoStream } from 'https://cdn.jsdelivr.net/npm/@skyway-sdk/room/+esm';

const robotSelect = document.getElementById('robotSelect');
const startBtn = document.getElementById('startBtn');
const stopBtn = document.getElementById('stopBtn');
const canvas = document.getElementById('cameraCanvas');
const statusEl = document.getElementById('gatewayStatus');
const wsState = document.getElementById('wsState');
const skyState = document.getElementById('skyState');
const fpsText = document.getElementById('fpsText');
const audioState = document.getElementById('audioState');
const robotInfo = document.getElementById('robotInfo');
const zoomSlider = document.getElementById('zoomSlider');
const zoomValue = document.getElementById('zoomValue');
const brightnessSlider = document.getElementById('brightnessSlider');
const brightnessValue = document.getElementById('brightnessValue');
const resetCameraBtn = document.getElementById('resetCameraBtn');
const ctx2d = canvas.getContext('2d', { alpha: false, desynchronized: true });
const audioOnlyMode = new URLSearchParams(location.search).get('audio_only') === '1';

let robots = [];
let ws = null;
let context = null;
let room = null;
let member = null;
let publication = null;
let audioPublication = null;
let canvasStream = null;
let audioContext = null;
let audioDestination = null;
let nextAudioTime = 0;
let frameCount = 0;
let lastFrameAt = 0;
let fpsTimer = null;
let started = false;
let latestImage = null;
let cameraSettings = { zoom: 1.0, brightness: 1.0 };
let settingsByRobot = {};
let suppressSliderSend = false;

function setStatus(msg) {
  statusEl.textContent = msg;
}

function setCanvasVisible(visible) {
  canvas.hidden = !visible;
}

function selectedRobot() {
  return robots.find((r) => r.id === robotSelect.value);
}

function roomNameFor(robot) {
  return robot.skyway_room || `${robot.id}-camera`;
}

function labelFor(robot) {
  return robot.label || robot.name || robot.id;
}

function controlsFor(robot) {
  const c = robot?.camera_controls || {};
  return {
    zoom: { default: 1.0, min: 1.0, max: 3.0, step: 0.1, ...(c.zoom || {}) },
    brightness: { default: 1.0, min: 0.5, max: 1.5, step: 0.05, ...(c.brightness || {}) },
  };
}

function updateRobotInfo() {
  const r = selectedRobot();
  robotInfo.textContent = r
    ? `ROS_DOMAIN_ID=${r.ros_domain_id} / image=${r.image_topic} (${r.image_type}) / audio=${r.audio_topic || '-'} / room=${roomNameFor(r)} / fps<=${r.camera_fps}`
    : '';
  applyControlRanges(r);
}

function clamp(value, min, max) {
  return Math.min(Math.max(Number(value), Number(min)), Number(max));
}

function formatZoom(v) {
  return `${Number(v).toFixed(1)}x`;
}

function formatBrightness(v) {
  return `${Math.round(Number(v) * 100)}%`;
}

function applyControlRanges(robot) {
  if (!robot) return;
  const c = controlsFor(robot);
  const saved = settingsByRobot[robot.id] || {
    zoom: c.zoom.default,
    brightness: c.brightness.default,
  };
  cameraSettings = {
    zoom: clamp(saved.zoom, c.zoom.min, c.zoom.max),
    brightness: clamp(saved.brightness, c.brightness.min, c.brightness.max),
  };
  suppressSliderSend = true;
  zoomSlider.min = c.zoom.min;
  zoomSlider.max = c.zoom.max;
  zoomSlider.step = c.zoom.step;
  zoomSlider.value = cameraSettings.zoom;
  brightnessSlider.min = c.brightness.min;
  brightnessSlider.max = c.brightness.max;
  brightnessSlider.step = c.brightness.step;
  brightnessSlider.value = cameraSettings.brightness;
  updateControlLabels();
  suppressSliderSend = false;
  drawLatestImage();
}

function updateControlLabels() {
  zoomValue.textContent = formatZoom(cameraSettings.zoom);
  brightnessValue.textContent = formatBrightness(cameraSettings.brightness);
}

function sendCameraSettings(source = 'camera_gateway') {
  const r = selectedRobot();
  if (!r) return;
  settingsByRobot[r.id] = { ...cameraSettings };
  updateControlLabels();
  drawLatestImage();
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({
      type: 'camera_settings',
      robot_id: r.id,
      zoom: cameraSettings.zoom,
      brightness: cameraSettings.brightness,
      source,
    }));
  }
}

function applyRemoteCameraSettings(msg) {
  const r = selectedRobot();
  if (!r || msg.robot_id !== r.id) return;
  const c = controlsFor(r);
  cameraSettings = {
    zoom: clamp(msg.zoom, c.zoom.min, c.zoom.max),
    brightness: clamp(msg.brightness, c.brightness.min, c.brightness.max),
  };
  settingsByRobot[r.id] = { ...cameraSettings };
  suppressSliderSend = true;
  zoomSlider.value = cameraSettings.zoom;
  brightnessSlider.value = cameraSettings.brightness;
  updateControlLabels();
  suppressSliderSend = false;
  drawLatestImage();
}

async function loadRobots() {
  const res = await fetch('/api/robots');
  if (!res.ok) throw new Error(await res.text());
  const data = await res.json();
  robots = Array.isArray(data) ? data : data.robots;
  robotSelect.innerHTML = robots.map((r) => `<option value="${r.id}">${labelFor(r)}</option>`).join('');
  for (const r of robots) {
    const c = controlsFor(r);
    settingsByRobot[r.id] = { zoom: c.zoom.default, brightness: c.brightness.default };
  }
  updateRobotInfo();
}

function connectImageWs(robotId) {
  if (ws) ws.close();
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${scheme}://${location.host}/ws`);
  ws.onopen = () => {
    wsState.textContent = 'WS: connected';
    setStatus('ROS2画像WebSocket: 接続済み。フレーム待機中...');
    sendCameraSettings('camera_gateway_open');
  };
  ws.onerror = () => {
    wsState.textContent = 'WS: error';
    setStatus('ROS2画像WebSocket: エラー');
  };
  ws.onclose = () => {
    wsState.textContent = 'WS: disconnected';
    if (started) setTimeout(() => connectImageWs(robotId), 1000);
  };
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === 'camera_settings') {
      applyRemoteCameraSettings(msg);
      return;
    }
    if (msg.type === 'audio_pcm' && msg.robot_id === robotId) {
      playPcmAudioChunk(msg);
      return;
    }
    if (msg.type !== 'image' || msg.robot_id !== robotId) return;
    const img = new Image();
    img.onload = () => {
      latestImage = img;
      drawLatestImage();
      lastFrameAt = performance.now();
      frameCount += 1;
    };
    img.src = `data:${msg.mime || 'image/jpeg'};base64,${msg.data}`;
  };
}

function drawLatestImage() {
  if (!latestImage) return;
  const w = latestImage.naturalWidth || 640;
  const h = latestImage.naturalHeight || 480;
  if (canvas.width !== w) canvas.width = w;
  if (canvas.height !== h) canvas.height = h;

  const zoom = Math.max(1.0, Number(cameraSettings.zoom) || 1.0);
  const srcW = Math.max(1, w / zoom);
  const srcH = Math.max(1, h / zoom);
  const sx = (w - srcW) / 2;
  const sy = (h - srcH) / 2;

  ctx2d.save();
  ctx2d.filter = `brightness(${Math.round((Number(cameraSettings.brightness) || 1.0) * 100)}%)`;
  ctx2d.drawImage(latestImage, sx, sy, srcW, srcH, 0, 0, w, h);
  ctx2d.restore();
}

async function stopPublish({ quiet = false } = {}) {
  started = false;
  try { if (publication && member) await member.unpublish(publication.id); } catch {}
  try { if (audioPublication && member) await member.unpublish(audioPublication.id); } catch {}
  try { if (member) await member.leave(); } catch {}
  try { if (room) await room.dispose?.(); } catch {}
  try { if (context) await context.dispose?.(); } catch {}
  try { if (ws) ws.close(); } catch {}
  if (canvasStream) canvasStream.getTracks().forEach((t) => t.stop());
  if (audioDestination) audioDestination.stream.getTracks().forEach((t) => t.stop());
  try { if (audioContext) await audioContext.close(); } catch {}
  if (fpsTimer) clearInterval(fpsTimer);
  publication = null;
  audioPublication = null;
  member = null;
  room = null;
  context = null;
  ws = null;
  canvasStream = null;
  audioContext = null;
  audioDestination = null;
  nextAudioTime = 0;
  fpsTimer = null;
  skyState.textContent = 'SkyWay: stopped';
  audioState.textContent = 'Audio: stopped';
  if (!quiet) setCanvasVisible(false);
  if (!quiet) setStatus('配信停止');
}

function decodeBase64Bytes(b64) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

function playPcmAudioChunk(msg) {
  if (!audioContext || !audioDestination || msg.format !== 's16le') return;
  const channels = Math.max(1, Number(msg.channels) || 1);
  const sampleRate = Number(msg.sample_rate) || audioContext.sampleRate;
  const bytes = decodeBase64Bytes(msg.data);
  const samples = new Int16Array(bytes.buffer, bytes.byteOffset, Math.floor(bytes.byteLength / 2));
  const frames = Math.floor(samples.length / channels);
  if (frames <= 0) return;

  const buffer = audioContext.createBuffer(channels, frames, sampleRate);
  for (let ch = 0; ch < channels; ch += 1) {
    const out = buffer.getChannelData(ch);
    for (let i = 0; i < frames; i += 1) {
      out[i] = samples[i * channels + ch] / 32768;
    }
  }

  const source = audioContext.createBufferSource();
  source.buffer = buffer;
  source.connect(audioDestination);
  const now = audioContext.currentTime;
  if (nextAudioTime < now + 0.02) nextAudioTime = now + 0.04;
  source.start(nextAudioTime);
  nextAudioTime += buffer.duration;
  if (nextAudioTime > now + 0.5) nextAudioTime = now + 0.1;
}

async function publishRosAudio() {
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  if (!AudioContextClass) {
    audioState.textContent = 'Audio: unsupported';
    return;
  }

  try {
    audioContext = new AudioContextClass();
    await audioContext.resume();
    audioDestination = audioContext.createMediaStreamDestination();
    const audioTrack = audioDestination.stream.getAudioTracks()[0];
    if (!audioTrack) {
      audioState.textContent = 'Audio: no track';
      return;
    }
    const audioStream = new LocalAudioStream(audioTrack);
    audioPublication = await member.publish(audioStream, { type: 'p2p' });
    audioState.textContent = 'Audio: publishing ROS mic';
  } catch (error) {
    audioState.textContent = `Audio: unavailable ${error.name || error}`;
  }
}

async function startPublish() {
  await stopPublish({ quiet: true });
  const r = selectedRobot();
  if (!r) return;

  started = true;
  setCanvasVisible(!audioOnlyMode);
  connectImageWs(r.id);
  skyState.textContent = 'SkyWay: connecting';
  setStatus(audioOnlyMode ? 'SkyWay音声接続中...' : 'SkyWay接続中...');

  const tokenRes = await fetch(`/api/skyway-token?robot_id=${encodeURIComponent(r.id)}&role=camera_gateway`);
  if (!tokenRes.ok) throw new Error(await tokenRes.text());
  const { token, room: skywayRoom, member: memberName } = await tokenRes.json();
  context = await SkyWayContext.Create(token);
  room = await SkyWayRoom.FindOrCreate(context, { name: skywayRoom });
  member = await room.join({ name: memberName, metadata: 'ros2-camera-gateway' });

  if (!audioOnlyMode) {
    canvasStream = canvas.captureStream(r.camera_fps || 10);
    const track = canvasStream.getVideoTracks()[0];
    const stream = new LocalVideoStream(track);
    publication = await member.publish(stream, {
      type: 'p2p',
      codecCapabilities: [{ mimeType: 'video/vp8' }, { mimeType: 'video/h264' }],
    });
  }
  await publishRosAudio();

  frameCount = 0;
  lastFrameAt = performance.now();
  fpsTimer = setInterval(() => {
    const age = Math.round(performance.now() - lastFrameAt);
    fpsText.textContent = `${frameCount} fps`;
    skyState.textContent = audioOnlyMode ? `SkyWay: publishing audio ${skywayRoom}` : `SkyWay: publishing ${skywayRoom}`;
    setStatus(audioOnlyMode
      ? `SkyWay音声配信中: room=${skywayRoom}`
      : `SkyWay配信中: room=${skywayRoom} / zoom=${formatZoom(cameraSettings.zoom)} / brightness=${formatBrightness(cameraSettings.brightness)} / ROS frames=${frameCount} / last=${age}ms前`);
    frameCount = 0;
  }, 1000);
}

zoomSlider.addEventListener('input', () => {
  if (suppressSliderSend) return;
  cameraSettings.zoom = Number(zoomSlider.value);
  sendCameraSettings('camera_gateway_slider');
});
brightnessSlider.addEventListener('input', () => {
  if (suppressSliderSend) return;
  cameraSettings.brightness = Number(brightnessSlider.value);
  sendCameraSettings('camera_gateway_slider');
});
resetCameraBtn.addEventListener('click', () => {
  const r = selectedRobot();
  const c = controlsFor(r);
  cameraSettings = { zoom: c.zoom.default, brightness: c.brightness.default };
  suppressSliderSend = true;
  zoomSlider.value = cameraSettings.zoom;
  brightnessSlider.value = cameraSettings.brightness;
  suppressSliderSend = false;
  sendCameraSettings('camera_gateway_reset');
});

startBtn.addEventListener('click', () => startPublish().catch((e) => {
  started = false;
  setCanvasVisible(false);
  skyState.textContent = 'SkyWay: error';
  setStatus(`開始失敗: ${e}`);
}));
stopBtn.addEventListener('click', () => stopPublish());
robotSelect.addEventListener('change', () => updateRobotInfo());
window.addEventListener('beforeunload', () => stopPublish());

loadRobots().catch((e) => setStatus(`ロボット設定取得失敗: ${e}`));
