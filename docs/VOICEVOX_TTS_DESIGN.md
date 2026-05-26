# VOICEVOX テキスト音声合成機能 設計書

**対象バージョン**: V9 (skyway_ros_bridge 対応版)  
**対応フレームワーク**: ROS2 Jazzy  
**デバイス**: Lightrover + PC WebUI

---

## 1. 要件

- [x] Web操作画面でテキスト入力
- [x] VOICEVOX で音声合成
- [x] ロボット側スピーカーで再生
- [x] ROS2 Jazzy 対応
- [x] 複数ロボットに対応可能な汎用設計

---

## 2. 現在の音声配信の仕組み

### 2.1 マイク音声配信（V8）

```
Lightrover
  └─ lightrover_mic_publisher.py
     ├─ arecord でマイクを開く
     └─ PCM s16le データを /lightrover/audio/pcm_s16le に publish (16kHz, mono)
        
PC ros_worker.py
  ├─ /lightrover/audio/pcm_s16le を subscribe
  └─ event_q に PCM チャンク { type: 'audio_pcm', data: base64, ... } で流す

PC WebSocket (Operator)
  └─ 上記チャンクをブラウザに送信

Browser (camera_gateway)
  ├─ PCM チャンクを Web Audio で復号
  └─ SkyWay へ AudioStream として publish
```

---

## 3. テキスト音声合成の追加設計

### 3.1 全体フロー

```
Operator Web UI
  └─ テキスト入力 + 送信ボタン
     └─ WebSocket で PC に { type: 'tts_request', text: '...', robot_id: '...', speaker_id: 1 }

PC Server (run_server.py)
  ├─ VOICEVOX API (/audio_query, /synthesis) を呼び出し
  ├─ WAV を PCM bytes に変換
  └─ 指定 robot_id に対応する cmd_q に publish トピック指令を送信

PC ROS Worker (ros_worker.py) per robot
  ├─ 指令を受け取る
  └─ /lightrover/tts/audio/pcm_s16le (または /lightrover/audio/tts など) に publish

Lightrover
  ├─ /lightrover/tts/audio/pcm_s16le を subscribe
  └─ PCM を alsa または pulseaudio でスピーカーに再生
```

---

## 4. コンポーネント詳細設計

### 4.1 Web UI (`web/index.html` / `web/app.js`)

#### 4.1.1 HTML 追加

- `<section class="card ttsCard">` を追加（Camera / SkyWay エリア下部など）
  - テキスト入力フィールド（`<textarea>` または `<input type="text">`）
  - 音色選択ドロップダウン（Speaker ID: 0=四国めたん など）
  - 「送信」ボタン
  - 状態表示（送信中、完了、エラー）
  
#### 4.1.2 JavaScript ロジック

```javascript
// TTS設定
let ttsSettings = { 
  speaker_id: 1,        // VOICEVOX speaker (1=四国めたん, 2=ずんだもん など)
  speed_scale: 1.0,     // 再生速度 0.5-2.0
  pitch_scale: 0.0      // ピッチ シフト -0.15 ~ 0.15
};

// テキスト送信
function sendTtsRequest(text) {
  if (!text.trim()) return;
  send({
    type: 'tts_request',
    text: text.trim(),
    speaker_id: ttsSettings.speaker_id,
    speed_scale: ttsSettings.speed_scale,
    pitch_scale: ttsSettings.pitch_scale,
    robot_id: currentRobot
  });
  // UI: 送信中表示
}

// 結果フィードバック
function onTtsResponse(message) {
  if (message.status === 'ok') {
    // UI: 再生予定の音声時間を表示
  } else if (message.status === 'error') {
    // UI: エラーメッセージを表示
  }
}
```

#### 4.1.3 WebSocket メッセージ仕様

**Request (Operator → Server)**
```json
{
  "type": "tts_request",
  "robot_id": "lightrover1",
  "text": "こんにちは",
  "speaker_id": 1,
  "speed_scale": 1.0,
  "pitch_scale": 0.0
}
```

**Response (Server → Operator)**
```json
{
  "type": "tts_response",
  "robot_id": "lightrover1",
  "status": "ok",
  "duration_sec": 2.5,
  "message": "音声送信完了"
}
```

or

```json
{
  "type": "tts_response",
  "robot_id": "lightrover1",
  "status": "error",
  "message": "VOICEVOX API エラー"
}
```

---

### 4.2 PC Server (`server/run_server.py`)

#### 4.2.1 VOICEVOX クライアント

- 新規クラス `VoicevoxClient` を作成
  - `__init__(base_url='http://localhost:50021')`
  - `async def synthesize(text: str, speaker_id: int, speed_scale: float, pitch_scale: float) -> bytes`
    - `/audio_query` で音声クエリを作成
    - `/synthesis` で WAV を取得
    - WAV → PCM (s16le, 16kHz mono) に変換して返す

#### 4.2.2 WebSocket メッセージハンドラ

```python
async def handle_message(msg):
    if msg['type'] == 'tts_request':
        robot_id = msg.get('robot_id')
        text = msg.get('text')
        speaker_id = msg.get('speaker_id', 1)
        speed_scale = msg.get('speed_scale', 1.0)
        pitch_scale = msg.get('pitch_scale', 0.0)
        
        try:
            pcm_bytes = await voicevox_client.synthesize(
                text, speaker_id, speed_scale, pitch_scale
            )
            # cmd_q に publish 指令を送信
            cmd_queues[robot_id].put({
                'action': 'publish_tts_audio',
                'pcm_data': pcm_bytes,
                'format': 's16le',
                'sample_rate': 16000,
                'channels': 1,
            })
            # クライアントに OK 応答
            await broadcast({
                'type': 'tts_response',
                'robot_id': robot_id,
                'status': 'ok',
                'duration_sec': len(pcm_bytes) / (16000 * 2)  # 16bit mono
            })
        except Exception as e:
            # エラー応答
            await broadcast({
                'type': 'tts_response',
                'robot_id': robot_id,
                'status': 'error',
                'message': str(e)
            })
```

---

### 4.3 PC ROS Worker (`server/ros_worker.py`)

#### 4.3.1 初期化

```python
class Worker(Node):
    def __init__(self):
        # ... 既存コード ...
        self.tts_enabled = bool(robot_dict.get('tts_enabled', True))
        if self.tts_enabled:
            self.tts_pub = self.create_publisher(
                UInt8MultiArray,
                '/lightrover/tts/audio/pcm_s16le',  # TTS専用トピック
                20
            )
```

#### 4.3.2 TTS オーディオ publish

```python
def publish_tts_audio(self, pcm_bytes: bytes) -> None:
    """TTS PCMデータをROS2トピックにpublish"""
    if not self.tts_enabled:
        return
    msg = UInt8MultiArray()
    msg.data = array('B', pcm_bytes)
    self.tts_pub.publish(msg)
    self.get_logger().info(f"published TTS audio: {len(pcm_bytes)} bytes")

def handle_command(self, cmd: dict) -> None:
    """... 既存の handle_command に以下を追加 ..."""
    if cmd.get('action') == 'publish_tts_audio':
        self.publish_tts_audio(cmd.get('pcm_data', b''))
```

---

### 4.4 Lightrover 側 (`lightrover_tts_player.py`)

新規スクリプト。Lightrover に配置。

#### 4.4.1 役割

- `/lightrover/tts/audio/pcm_s16le` を subscribe
- PCM s16le データをスピーカーに再生（`alsaaudio` または `pyaudio`）

#### 4.4.2 実装概要

```python
#!/usr/bin/env python3
import rclpy
from rclpy.node import Node
from std_msgs.msg import UInt8MultiArray
import alsaaudio  # or pyaudio
import numpy as np

class LightroverTtsPlayer(Node):
    def __init__(self):
        super().__init__('lightrover_tts_player')
        self.subscription = self.create_subscription(
            UInt8MultiArray,
            '/lightrover/tts/audio/pcm_s16le',
            self.play_audio_callback,
            20
        )
        self.device = alsaaudio.PCM(
            alsaaudio.PCM_PLAYBACK,
            channels=1,
            rate=16000,
            format=alsaaudio.PCM_FORMAT_S16_LE,
            periodsize=1024
        )
        self.get_logger().info('TTS player started')

    def play_audio_callback(self, msg: UInt8MultiArray):
        pcm_bytes = bytes(msg.data)
        self.device.write(pcm_bytes)
        self.get_logger().debug(f'Playing {len(pcm_bytes)} bytes of TTS audio')

def main():
    rclpy.init()
    node = LightroverTtsPlayer()
    rclpy.spin(node)

if __name__ == '__main__':
    main()
```

---

## 5. ROS2 トピック設計

### 5.1 新規トピック

| トピック名 | 型 | 方向 | 説明 |
|---|---|---|---|
| `/lightrover/tts/audio/pcm_s16le` | `std_msgs/UInt8MultiArray` | pub (PC) → sub (ロボット) | TTS音声 PCM チャンク |

### 5.2 既存トピック との併用

- マイク音声 `/lightrover/audio/pcm_s16le` はそのまま利用可能
- 同じフォーマット（s16le 16kHz mono）で統一 → ロボット側で共通再生ロジック利用可能

---

## 6. VOICEVOX API 仕様

### 6.1 必要なエンドポイント

- **POST `/audio_query`**
  - `text`, `speaker` パラメータ
  - 戻り値: JSON (accentPhrase, speedScale, pitchScale など)
  
- **POST `/synthesis`**
  - `text`, `speaker`, `speedScale`, `pitchScale` パラメータ
  - Body: audio_query の JSON
  - 戻り値: WAV バイナリ (Content-Type: audio/wav)

### 6.2 デフォルト設定

| 項目 | 値 |
|---|---|
| ベース URL | `http://localhost:50021` |
| デフォルト Speaker | 1 (四国めたん) |
| Speed Scale | 1.0 (1.0x) |
| Pitch Scale | 0.0 (標準) |
| Audio Format | PCM s16le, 16kHz, mono |

### 6.3 環境変数

```bash
VOICEVOX_API_URL=http://voicevox-server:50021
VOICEVOX_SPEAKER_DEFAULT=1
```

---

## 7. 設定ファイル設計 (`config/robots.yaml`)

### 7.1 新規オプション

```yaml
robots:
  - id: lightrover1
    # ... 既存項目 ...
    
    # TTS 音声合成設定
    tts_enabled: true
    tts_default_speaker_id: 1
    tts_default_speed_scale: 1.0
    tts_default_pitch_scale: 0.0
    tts_max_text_length: 500  # 最大文字数
```

---

## 8. 実装フェーズ

### Phase 1: PC側 VOICEVOX クライアント
- `server/voicevox_client.py` 作成（async WAV→PCM）
- テスト可能に

### Phase 2: PC ROS Worker 統合
- TTS publish エンドポイント追加
- cmd_q 経由でのトリガー追加

### Phase 3: Web UI
- テキスト入力フォーム追加
- Speaker 選択ドロップダウン
- WebSocket メッセージ送受信

### Phase 4: Lightrover TTS Player
- `lightrover_tts_player.py` 作成・配置
- スピーカー再生テスト

### Phase 5: 統合テスト
- E2E フロー確認
- エラーハンドリング改善

---

## 9. 汎用性への配慮

### 9.1 複数スピーカー対応
- VOICEVOX の Speaker ID を Web UI で選択可能に
- ロボット毎にデフォルト選択を `robots.yaml` で指定可能

### 9.2 複数ロボット対応
- `robot_id` を WebSocket メッセージに含める
- cmd_q で個別ロボットへ指令を送信

### 9.3 拡張性
- 将来的に TTS エンジンを切り替え可能な構造（VOICEVOX → OpenAI TTS など）
- 音声パラメータ（速度、ピッチ）を Web UI で調整可能

---

## 10. セキュリティ考慮

- TTS リクエストのテキスト長上限（injection対策）
- VOICEVOX API へのレート制限
- 許可されたロボット ID のみ処理

---

## 11. トラブルシューティング

| 症状 | 原因 | 対処 |
|---|---|---|
| VOICEVOX API 接続エラー | VOICEVOX サーバーが起動していない | `curl http://localhost:50021/speakers` で確認 |
| スピーカーから音が出ない | `/lightrover/tts/audio/pcm_s16le` に受信していない | `ros2 topic echo /lightrover/tts/audio/pcm_s16le` で確認 |
| 音が歪む・フレーム落ち | PC ↔ ロボット通信が遅い | ネットワーク遅延、ROS_DOMAIN_ID 確認 |

---

## 付録: リファレンス

### A. VOICEVOX 起動例

```bash
docker run -d --rm -p 50021:50021 voicevox/voicevox:latest
```

### B. WAV ↔ PCM 変換例

```python
import wave
import io

def wav_to_pcm_s16le(wav_bytes: bytes) -> bytes:
    """WAV -> PCM s16le bytes"""
    with wave.open(io.BytesIO(wav_bytes), 'rb') as wf:
        return wf.readframes(wf.getnframes())

def pcm_s16le_to_wav(pcm_bytes: bytes, sample_rate: int = 16000, channels: int = 1) -> bytes:
    """PCM s16le -> WAV bytes"""
    out = io.BytesIO()
    with wave.open(out, 'wb') as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return out.getvalue()
```

---

**版**: 1.0  
**作成日**: 2026-05-16
