from fastapi import FastAPI, BackgroundTasks, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from fastapi.responses import Response, RedirectResponse, JSONResponse
from pydantic import BaseModel
from mpd import MPDClient
from camilladsp import CamillaClient
import subprocess, requests, yaml, os, re, time, json, asyncio, threading, wave, shutil
import math
from urllib.parse import urlparse, parse_qs

# Phase 1a: 共通 MPD クライアントを取り込む
from hqmplayer_core.mpd import (
    MPD_HOST as _MPD_HOST,
    MPD_PORT as _MPD_PORT,
    mpd_connection,
)

# Phase 1b: Now Playing 整形ロジックを共通化
from hqmplayer_core.meta import format_now_playing

# Phase 1d: アルバムアート解決を共通化
from hqmplayer_core.art import resolve_art

MPD_HOST = os.getenv("MPD_HOST", "127.0.0.1")
try:
    MPD_PORT = int(os.getenv("MPD_PORT", "6600"))
except ValueError:
    MPD_PORT = 6600

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SWITCH_AUDIO_SCRIPT = os.path.join(BASE_DIR, "scripts", "switch_audio.sh")


# ─────────────────────────────────────────────────────────────────────────────
# MPD 接続ヘルパー
# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 修正: async 関数として共通モジュールの mpd_connection を直接使う。
# sync_* 系は廃止（マルチイベントループの競合を避けるため）。

async def mpd_status() -> dict:
    """MPD の status() を取得（async）。"""
    async with mpd_connection() as c:
        return await c.status()


async def mpd_currentsong() -> dict:
    """MPD の currentsong() を取得（async）。"""
    async with mpd_connection() as c:
        return await c.currentsong()


async def mpd_idle(*subsystems: str):
    """MPD の idle() を実行（変更サブシステムのリストを返す）。"""
    async with mpd_connection() as c:
        result = []
        async for changed in c.idle(subsystems=tuple(subsystems)):
            result.append(changed)
            break
        return result


async def mpd_readpicture(uri: str):
    """MPD の readpicture() を実行（失敗時は None）。"""
    async with mpd_connection() as c:
        try:
            return await c.readpicture(uri)
        except Exception:
            return None


async def mpd_albumart(uri: str):
    """MPD の albumart() を実行（失敗時は None）。"""
    async with mpd_connection() as c:
        try:
            return await c.albumart(uri)
        except Exception:
            return None


# 後方互換のため旧名の関数を残しておく（Phase 2 で削除予定）
def mpd_connect(timeout=3, retries=2):
    """旧 API（接続オブジェクトを返す）。Phase 2 時点では未使用。

    新規コードは async 版の mpd_status / mpd_currentsong / mpd_idle を使うこと。
    旧呼び出し箇所（Phase 2 で撤去予定）のために暫定的に残している。
    """
    last_err = None
    for attempt in range(retries + 1):
        try:
            c = MPDClient()
            c.timeout = timeout
            c.idletimeout = timeout
            c.connect(MPD_HOST, MPD_PORT)
            return c
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(0.5)
    raise last_err


async def _playback_watchdog():
    """5秒ごとに MPD 状態を確認 — 共通モジュールの mpd_connection を直接使う"""
    from hqmplayer_core.mpd import mpd_connection as _mpd_conn
    was_playing = False

    while True:
        try:
            await asyncio.sleep(5)

            # Phase 2 修正: メインループ上で直接 mpd_connection を await
            async with _mpd_conn() as c:
                st = await c.status()
            state = st.get("state", "stop")

            if state == "play":
                was_playing = True
            elif state == "stop" and was_playing:
                # 再生再開（DSP backend のみの挙動を維持）
                async with _mpd_conn() as c:
                    await c.play()
                was_playing = False
            elif state in ("pause", "stop"):
                was_playing = False

        except Exception:
            # 接続断時は共通モジュール側で自動再接続される
            await asyncio.sleep(5)


LAST_CONFIG_PATH = os.path.expanduser("~/.config/audiophile/last_config.json")


def _default_audio_config() -> dict:
    return {
        "mode": "pure",
        "device": "",
        "volume": -5.0,
        "music_type": "none",
        "eq_output": "none",
        "crossfeed": "none",
        "crossfeed_intensity": 5,
        "hum_noise": "none",
        "reverb": "none",
        "reverb_intensity": 5,
    }


def _load_last_config() -> dict:
    config = _default_audio_config()
    try:
        if os.path.exists(LAST_CONFIG_PATH):
            with open(LAST_CONFIG_PATH) as f:
                data = json.load(f)
            if isinstance(data, dict):
                config.update(data)
    except Exception:
        pass
    return config


def _save_last_config(config_dict: dict):
    os.makedirs(os.path.dirname(LAST_CONFIG_PATH), exist_ok=True)
    with open(LAST_CONFIG_PATH, "w") as f:
        json.dump(config_dict, f)


def _update_last_config(patch: dict):
    config = _load_last_config()
    config.update(patch)
    _save_last_config(config)


def _config_requires_restart(config: "AudioConfig", last_config: dict | None) -> bool:
    if last_config is None:
        return True
    for key in [
        "mode",
        "device",
        "music_type",
        "eq_output",
        "crossfeed",
        "crossfeed_intensity",
        "hum_noise",
        "reverb",
        "reverb_intensity",
    ]:
        if last_config.get(key) != getattr(config, key):
            return True
    return False


def _normalize_config_for_device(config: "AudioConfig", requested_mode: str | None = None) -> "AudioConfig":
    """Bluetooth を pure で選択した場合は DSP でパススルーし、処理をすべて無効化する。"""
    if "bluealsa" in config.device and requested_mode == "pure":
        return AudioConfig(
            mode="dsp",
            device=config.device,
            volume=config.volume,
            music_type="none",
            eq_output="none",
            crossfeed="none",
            hum_noise="none",
            reverb="none",
            reverb_intensity=5,
        )
    if "bluealsa" in config.device:
        config.mode = "dsp"
    return config


def _has_loopback_capture_device() -> bool:
    capture_path = "/proc/asound/Loopback/pcm1c/info"
    return os.path.exists(capture_path)


def _ensure_dsp_prerequisites(config: "AudioConfig"):
    if config.mode != "dsp":
        return
    if not _has_loopback_capture_device():
        raise HTTPException(
            status_code=503,
            detail="ALSA Loopback device is unavailable. Load snd-aloop and retry.",
        )


def _detect_alsa_cards() -> tuple[str | None, str | None]:
    """① 共通: aplay -l を解析して (usb_card, pch_card) のカード番号を返す。"""
    usb_card = None
    pch_card = None
    try:
        env = os.environ.copy()
        env["LC_ALL"] = "C"
        res = subprocess.run(["aplay", "-l"], capture_output=True, text=True, env=env)
        for line in res.stdout.splitlines():
            line_up = line.upper()
            m = re.search(r'(?:card|カード)\s+(\d+)', line, re.IGNORECASE)
            if not m:
                continue
            card_num = m.group(1)
            if "USB" in line_up and usb_card is None:
                usb_card = card_num
            if pch_card is None:
                if ("PCH" in line_up or ("HDA" in line_up and "HDMI" not in line_up) or "CS4208" in line_up):
                    pch_card = card_num
    except Exception:
        pass
    return usb_card, pch_card


def _get_available_devices() -> list[dict]:
    """① 共通: 利用可能なオーディオデバイス一覧を返す。"""
    usb_card, pch_card = _detect_alsa_cards()
    devices = []
    if usb_card:
        devices.append({"id": f"plughw:{usb_card},0", "name": f"USB DAC (hw:{usb_card},0)"})
    if pch_card:
        devices.append({"id": f"plughw:{pch_card},0", "name": f"PC Speaker (hw:{pch_card},0)"})
    devices.append({"id": "plug:bluealsa", "name": "Bluetooth (A2DP)"})
    return devices if devices else [{"id": "plughw:1,0", "name": "PC Speaker (hw:1,0)"}]


def _get_first_valid_device(exclude_bluetooth: bool = True) -> str:
    """Get the first available non-bluetooth device, or first bluetooth if none found"""
    devices = _get_available_devices()
    for dev in devices:
        if exclude_bluetooth and "bluealsa" in dev["id"]:
            continue
        if dev["id"] != "none" and dev["id"] != "error":
            return dev["id"]
    for dev in devices:
        if dev["id"] != "none" and dev["id"] != "error":
            return dev["id"]
    return "plughw:1,0"


def _extract_alsa_card_number(device_id: str) -> str | None:
    """Extract ALSA card number from device ids like plughw:2,0 / hw:2,0."""
    if not device_id:
        return None
    m = re.search(r"(?:^|:)(?:plughw|hw):(\d+),\d+", device_id, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def _ensure_ir_192k(ir_path: str, target_rate: int = 192000) -> str:
    """IR ファイルが target_rate でなければ SoX で変換してキャッシュに保存し、キャッシュパスを返す。

    - 192kHz 済みなら即リターン（ゼロコスト）
    - 変換済みキャッシュがあればそれを返す
    - SoX があれば自動変換してキャッシュに保存
    - SoX がなければ手動変換コマンドを示して RuntimeError
    """
    try:
        with wave.open(ir_path, "r") as wf:
            rate = wf.getframerate()
    except Exception:
        return ir_path  # ヘッダが読めない場合は元のパスを返す（CamillaDSP に任せる）

    if rate == target_rate:
        return ir_path  # 既に目標レート

    # キャッシュディレクトリ
    cache_dir = os.path.expanduser("~/.cache/audiophile/ir")
    os.makedirs(cache_dir, exist_ok=True)
    
    # キャッシュファイル名（元ファイル名 + ターゲットレート）
    basename = os.path.basename(ir_path)
    name, ext = os.path.splitext(basename)
    cache_path = os.path.join(cache_dir, f"{name}_{target_rate}{ext}")

    # キャッシュが存在し、元ファイルより新しければキャッシュを返す
    if os.path.exists(cache_path) and os.path.getmtime(cache_path) >= os.path.getmtime(ir_path):
        return cache_path

    # 変換が必要
    if not shutil.which("sox"):
        raise RuntimeError(
            f"IR ファイル {ir_path} は {rate}Hz です（{target_rate}Hz 必要）。\n"
            f"一度だけ以下を実行してください:\n"
            f"  sox '{ir_path}' -r {target_rate} '{cache_path}'"
        )

    tmp = cache_path + "._converting.wav"
    try:
        subprocess.run(["sox", ir_path, "-r", str(target_rate), tmp],
                       check=True, capture_output=True)
        os.replace(tmp, cache_path)  # アトミックにキャッシュ保存
        return cache_path
    except Exception as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise RuntimeError(f"IR 変換失敗 ({rate}Hz → {target_rate}Hz): {e}")


def _restore_last_config():
    """起動時に前回の設定を復元してスクリプト経由で適用。デバイスが無効な場合は自動検出。"""
    try:
        if not os.path.exists(LAST_CONFIG_PATH):
            return
        d = _load_last_config()
        cfg = AudioConfig(**d)
        cfg = _normalize_config_for_device(cfg, requested_mode=d.get("mode"))
        
        # デバイスが無効または空の場合、利用可能なデバイスを自動検出
        if not cfg.device or cfg.device == "none" or cfg.device == "error":
            cfg.device = _get_first_valid_device(exclude_bluetooth=True)
        
        if cfg.mode == "dsp":
            yp = generate_camilladsp_yaml(cfg)
            subprocess.Popen(["bash", SWITCH_AUDIO_SCRIPT, "dsp", cfg.device, yp])
            _schedule_init_vol(cfg.volume)
        else:
            subprocess.Popen(["bash", SWITCH_AUDIO_SCRIPT, "pure", cfg.device, "none"])
    except Exception as e:
        try:
            with open("/tmp/hq_api_apply.log", "a") as lof:
                lof.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] _restore_last_config failed: {e}\n")
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(_playback_watchdog())
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _restore_last_config)
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────────────────────────
# DSP プリセット保存先
# ─────────────────────────────────────────────────────────────────────────────
PRESETS_PATH = os.path.expanduser("~/.config/audiophile/presets.json")


def load_presets() -> dict:
    try:
        with open(PRESETS_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save_presets(presets: dict):
    os.makedirs(os.path.dirname(PRESETS_PATH), exist_ok=True)
    with open(PRESETS_PATH, "w") as f:
        json.dump(presets, f, ensure_ascii=False, indent=2)


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket 接続マネージャー
# ─────────────────────────────────────────────────────────────────────────────
class WSManager:
    def __init__(self):
        self.clients: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.clients.append(ws)

    def disconnect(self, ws: WebSocket):
        self.clients.remove(ws)

    async def broadcast(self, data: dict):
        dead = []
        for ws in self.clients:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.remove(ws)


ws_manager = WSManager()


class AudioConfig(BaseModel):
    mode: str
    device: str
    volume: float
    music_type: str
    eq_output: str
    crossfeed: str
    crossfeed_intensity: int = 5
    hum_noise: str
    reverb: str
    reverb_intensity: int = 5


class VolumeControl(BaseModel):
    volume: float


class StoredAudioConfig(AudioConfig):
    pass


MUSIC_EQ = {
    "none": [],
    "jazz": [{"freq": 80, "q": 0.9, "gain": 2.5}, {"freq": 300, "q": 1.0, "gain": 1.0}, {"freq": 7000, "q": 0.8, "gain": 0.7}],
    "classical": [{"freq": 60, "q": 0.7, "gain": 1.0}, {"freq": 400, "q": 0.9, "gain": -0.5}, {"freq": 8000, "q": 0.8, "gain": 1.0}],
    "electronic": [{"freq": 50, "q": 0.8, "gain": 4.0}, {"freq": 400, "q": 1.2, "gain": -2.0}, {"freq": 10000, "q": 0.9, "gain": 2.5}],
    "vocal": [{"freq": 150, "q": 1.0, "gain": -1.0}, {"freq": 1000, "q": 0.8, "gain": 3.0}, {"freq": 3000, "q": 0.9, "gain": 2.0}],
}

OUTPUT_EQ = {
    "none": [],
    "studio-monitors": [{"freq": 80, "q": 0.8, "gain": 3.0}, {"freq": 2500, "q": 1.0, "gain": -0.8}, {"freq": 20000, "q": 1.0, "gain": 3.0}],
    "JBL-Speakers": [{"freq": 70, "q": 0.7, "gain": 3.0}, {"freq": 1200, "q": 1.0, "gain": -2.0}, {"freq": 13000, "q": 0.8, "gain": 5.0}],
    "planar-magnetic": [{"freq": 30, "q": 0.7, "gain": 1.0}, {"freq": 180, "q": 0.9, "gain": -1.0}, {"freq": 15000, "q": 0.8, "gain": 1.0}],
    "loud-speaker": [{"freq": 70, "q": 0.7, "gain": 4.0}, {"freq": 300, "q": 1.0, "gain": 1.0}, {"freq": 8000, "q": 0.7, "gain": 4.0}, {"freq": 16000, "q": 0.9, "gain": 2.5}],
    "Tube-Warmth": [{"freq": 200, "q": 0.8, "gain": 2.5}, {"freq": 4000, "q": 1.0, "gain": -1.5}, {"freq": 10000, "q": 0.8, "gain": -2.0}],
    "Crystal-Clarity": [{"freq": 100, "q": 1.2, "gain": -2.0}, {"freq": 8000, "q": 0.7, "gain": 4.0}, {"freq": 16000, "q": 0.9, "gain": 2.5}],
}


# ─────────────────────────────────────────────────────────────────────────────
# デバイス一覧
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/api/devices")
def get_devices():
    devices = []
    try:
        usb_card, pch_card = _detect_alsa_cards()  # ① 共通関数を使用

        if usb_card:
            devices.append({"id": f"plughw:{usb_card},0", "name": f"USB DAC (hw:{usb_card},0)"})
        else:
            devices.append({"id": "none", "name": "USB DAC (Not Connected)"})

        if pch_card:
            devices.append({"id": f"plughw:{pch_card},0", "name": f"PC Speaker / Headphone (hw:{pch_card},0)"})
        else:
            devices.append({"id": "plughw:1,0", "name": "PC Speaker / Headphone (hw:1,0)"})

        devices.append({"id": "plug:bluealsa", "name": "Bluetooth (A2DP)"})

    except Exception as e:
        devices.append({"id": "error", "name": str(e)})

    return devices


# ─────────────────────────────────────────────────────────────────────────────
# CamillaDSP YAML 生成
# ─────────────────────────────────────────────────────────────────────────────
def generate_camilladsp_yaml(config: AudioConfig) -> str:
    is_bt = "bluealsa" in config.device
    usb_card, _ = _detect_alsa_cards()
    selected_card = _extract_alsa_card_number(config.device)
    is_usb = bool(usb_card and selected_card and selected_card == usb_card)

    samplerate = 192000
    pb_format = "S16_LE" if (is_usb or is_bt) else "S32_LE"
    cap_format = "S32_LE"

    pb_device = config.device.replace("hw:", "plughw:") if config.device.startswith("hw:") else config.device

    devices_block = {
        "samplerate": samplerate,
        "enable_rate_adjust": True,
        "chunksize": 4096,
        "capture": {"type": "Alsa", "channels": 2, "device": "hw:Loopback,1,0", "format": cap_format},
        "playback": {"type": "Alsa", "channels": 2, "device": pb_device, "format": pb_format},
    }
    capture_samplerate = 192000
    if capture_samplerate != samplerate:
        devices_block["capture_samplerate"] = capture_samplerate
        devices_block["resampler"] = {"type": "AsyncSinc", "profile": "Balanced"}

    y = {"devices": devices_block, "filters": {}, "pipeline": [], "mixers": {}}

    # Determine if we need parallel DRY/WET paths (reverb enabled)
    has_reverb = config.reverb != "none" and config.reverb_intensity > 0
    
    # ─────────────────────────────────────────────────────────────────────
    # MIXER: Split input to DRY (ch 0-1) and WET (ch 2-3) if reverb enabled
    # ─────────────────────────────────────────────────────────────────────
    if has_reverb:
        y["mixers"]["split"] = {
            "channels": {"in": 2, "out": 4},
            "mapping": [
                {"dest": 0, "sources": [{"channel": 0, "gain": 0.0, "inverted": False}]},  # DRY L: UNCHANGED - preserve signal quality
                {"dest": 1, "sources": [{"channel": 1, "gain": 0.0, "inverted": False}]},  # DRY R: UNCHANGED
                {"dest": 2, "sources": [{"channel": 0, "gain": 0.0, "inverted": False}]},  # WET L input: full level to Conv
                {"dest": 3, "sources": [{"channel": 1, "gain": 0.0, "inverted": False}]},  # WET R input: full level to Conv
            ],
        }
        y["pipeline"].append({"type": "Mixer", "name": "split"})

    # ─────────────────────────────────────────────────────────────────────
    # FILTER: Main DRY path (ch 0-1) or monolithic path (no reverb)
    # ─────────────────────────────────────────────────────────────────────
    filt_dry = {"type": "Filter", "channels": [0, 1] if has_reverb else [0, 1], "names": []}
    
    def add_f_dry(n, d):
        y["filters"][n] = d
        filt_dry["names"].append(n)

    if config.hum_noise in ["50hz", "60hz"] and config.hum_noise != "none":
        add_f_dry("rumble_cut", {"type": "Biquad", "parameters": {"type": "HighpassFO", "freq": 15}})
        freq = 50 if config.hum_noise == "50hz" else 60
        add_f_dry("hum", {"type": "Biquad", "parameters": {"type": "Notch", "freq": freq, "q": 30.0}})

    for i, eq in enumerate(MUSIC_EQ.get(config.music_type, [])):
        add_f_dry(f"m_{i}", {"type": "Biquad", "parameters": {"type": "Peaking", "freq": eq["freq"], "q": eq["q"], "gain": eq["gain"]}})
    for i, eq in enumerate(OUTPUT_EQ.get(config.eq_output, [])):
        add_f_dry(f"o_{i}", {"type": "Biquad", "parameters": {"type": "Peaking", "freq": eq["freq"], "q": eq["q"], "gain": eq["gain"]}})

    # Headroom protection on DRY path - only when NOT using reverb (no parallel processing)
    # When reverb is on, DRY goes through split mixer and headroom is applied AFTER mixing
    if not has_reverb:
        headroom_db = -4.0
        if config.music_type != "none" or config.eq_output != "none":
            add_f_dry("headroom", {"type": "Gain", "parameters": {"gain": headroom_db, "inverted": False, "mute": False}})

    if filt_dry["names"]:
        y["pipeline"].append(filt_dry)

    # ─────────────────────────────────────────────────────────────────────
    # FILTER: WET path (ch 2-3) - Conv + WET gain
    # ─────────────────────────────────────────────────────────────────────
    if has_reverb:
        src_ir = os.path.expanduser(f"~/.config/camilladsp/ir/{config.reverb}.wav")
        try:
            if not os.path.exists(src_ir):
                raise FileNotFoundError(f"IR source missing: {src_ir}")

            # WET path filters: Conv + Gain (on channels 2-3)
            filt_wet = {"type": "Filter", "channels": [2, 3], "names": []}

            def add_f_wet(n, d):
                y["filters"][n] = d
                filt_wet["names"].append(n)

            # IR を 192kHz に変換（既に 192kHz なら即リターン、キャッシュパスを返す）
            cache_ir = _ensure_ir_192k(src_ir, target_rate=192000)

            # Conv: 192kHz に変換済みの IR を直接参照
            add_f_wet("rev", {"type": "Conv", "parameters": {
                "type": "Wav",
                "filename": cache_ir,
            }})
            
            # WET gain: VERY conservative to avoid clipping when mixed with full-level DRY
            # intensity=50 -> -44dB (extremely subtle), intensity=100 -> -32dB (very subtle)
            wet_gain_db = round(-50.0 + (config.reverb_intensity / 100.0) * 18.0, 1)
            add_f_wet("rev_out", {"type": "Gain", "parameters": {"gain": wet_gain_db, "inverted": False, "mute": False}})
            
            y["pipeline"].append(filt_wet)
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            try:
                with open("/tmp/hq_api_apply.log", "a") as lof:
                    lof.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Failed to process IR {config.reverb}: {str(e)}\n")
                    lof.write(tb + "\n")
            except Exception:
                pass
            # ④ Fallback: エラー時に split/mix Mixer が残らないよう、
            # pipeline から split Mixer と後続の mix Mixer を除去してから reverb を無効化
            y["pipeline"] = [p for p in y["pipeline"] if not (p.get("type") == "Mixer" and p.get("name") in ("split", "mix"))]
            y["mixers"].pop("split", None)
            y["mixers"].pop("mix", None)
            config.reverb = "none"
            print(f"Ambience error: {e}")

    # ─────────────────────────────────────────────────────────────────────
    # CROSSFEED mixer (before splitting, so applies to all signals)
    # ─────────────────────────────────────────────────────────────────────
    if config.crossfeed != "none":
        intensity = config.crossfeed_intensity
        intensity_pct = max(0.01, min(1.0, intensity / 100.0))
        if config.crossfeed == "light":
            cf_gain_cross = round(-20 + 6 * intensity_pct, 1)
            cf_gain_direct = round(-1.5 * (1 - intensity_pct), 1)
        else:
            cf_gain_cross = round(-20 + 10.5 * intensity_pct, 1)
            cf_gain_direct = round(-3.5 * (1 - intensity_pct), 1)
        
        y["mixers"]["cf"] = {
            "channels": {"in": 2, "out": 2},
            "mapping": [
                {"dest": 0, "sources": [{"channel": 0, "gain": cf_gain_direct, "inverted": False}, {"channel": 1, "gain": cf_gain_cross, "inverted": False}]},
                {"dest": 1, "sources": [{"channel": 1, "gain": cf_gain_direct, "inverted": False}, {"channel": 0, "gain": cf_gain_cross, "inverted": False}]},
            ],
        }
        y["pipeline"].insert(0, {"type": "Mixer", "name": "cf"})

    # ─────────────────────────────────────────────────────────────────────
    # MIXER: Recombine DRY (ch 0-1) + WET (ch 2-3) back to output (ch 0-1)
    # has_reverb は IR 失敗時に config.reverb="none" に書き換わるため再評価する
    # ─────────────────────────────────────────────────────────────────────
    has_reverb_effective = has_reverb and config.reverb != "none"
    if has_reverb_effective:
        y["mixers"]["mix"] = {
            "channels": {"in": 4, "out": 2},
            "mapping": [
                {"dest": 0, "sources": [
                    {"channel": 0, "gain": 0.0, "inverted": False},  # DRY L
                    {"channel": 2, "gain": 0.0, "inverted": False},  # WET L
                ]},
                {"dest": 1, "sources": [
                    {"channel": 1, "gain": 0.0, "inverted": False},  # DRY R
                    {"channel": 3, "gain": 0.0, "inverted": False},  # WET R
                ]},
            ],
        }
        y["pipeline"].append({"type": "Mixer", "name": "mix"})
        
        # Final headroom after mixing DRY + WET to prevent clipping
        y["filters"]["final_headroom"] = {
            "type": "Gain",
            "parameters": {"gain": -3.0, "inverted": False, "mute": False}
        }
        y["pipeline"].append({
            "type": "Filter",
            "channels": [0, 1],
            "names": ["final_headroom"]
        })

    # Remove empty pipelines
    y["pipeline"] = [p for p in y["pipeline"] if not (p.get("type") == "Filter" and len(p.get("names", [])) == 0)]
    
    # Fallback: ensure at least dummy filter
    if not y["pipeline"]:
        y["filters"]["dummy"] = {"type": "Gain", "parameters": {"gain": 0.0, "inverted": False, "mute": False}}
        y["pipeline"] = [{"type": "Filter", "channels": [0, 1], "names": ["dummy"]}]

    os.makedirs("/tmp/camilladsp", exist_ok=True)
    with open("/tmp/camilladsp/active_dsp.yml", "w") as f:
        yaml.dump(y, f, sort_keys=False)
    return "/tmp/camilladsp/active_dsp.yml"


# ─────────────────────────────────────────────────────────────────────────────
# ボリューム
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/api/volume")
def set_volume(vol: VolumeControl):
    try:
        c = CamillaClient("127.0.0.1", 1234)
        c.connect()
        c.volume.set_main_volume(vol.volume)
        c.disconnect()
        _update_last_config({"volume": vol.volume})
        return {"status": "success"}
    except Exception as e:
        # Surface errors as HTTP 422 so frontend sees non-OK responses
        raise HTTPException(status_code=422, detail=str(e))


def _init_vol(v: float):
    """CamillaDSP への接続を試行し、確立でき次第 main_volume を設定する。

    CamillaDSP を `-s/--statefile` 付きで起動した場合、起動時に statefile から
    main_volume が自動復元されるため、Python 側で fade-in 等の複雑な処理は不要。
    """
    for _ in range(200):  # 最大 10 秒待機
        time.sleep(0.05)
        try:
            c = CamillaClient("127.0.0.1", 1234)
            c.connect()
            c.volume.set_main_volume(v)
            c.disconnect()
            return
        except Exception:
            pass


def _schedule_init_vol(v: float):
    """_init_vol をバックグラウンドスレッドで実行."""
    thread = threading.Thread(target=_init_vol, args=(v,), daemon=True)
    thread.start()


# ─────────────────────────────────────────────────────────────────────────────

# ---- CamillaDSP Health Check & Restart API ----
@app.get("/api/dsp_status")
def get_dsp_status():
    """Check if CamillaDSP is running on port 1234."""
    try:
        c = CamillaClient("127.0.0.1", 1234)
        c.connect()
        version_info = c.cdsp_version
        st = c.general.state()
        c.disconnect()
        return {"status": "running", "version": version_info, "state": st}
    except Exception as e:
        return {"status": "stopped", "error": str(e)}


@app.post("/api/dsp_restart")
def restart_dsp(cfg: AudioConfig):
    """Force restart CamillaDSP with current config."""
    try:
        normalized = _normalize_config_for_device(cfg)
        _ensure_dsp_prerequisites(normalized)
        yp = generate_camilladsp_yaml(normalized)
        result = subprocess.run(
            ["bash", SWITCH_AUDIO_SCRIPT, "dsp", normalized.device, yp],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            return JSONResponse(
                status_code=422,
                content={"status": "error", "message": "switch_audio failed", "stdout": result.stdout, "stderr": result.stderr},
            )
        _schedule_init_vol(normalized.volume)
        _save_last_config(normalized.model_dump())
        return {"status": "success", "stdout": result.stdout, "stderr": result.stderr}
    except Exception as e:
        return JSONResponse(status_code=422, content={"status": "error", "message": str(e)})


# 設定適用
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/api/config", response_model=StoredAudioConfig)
def get_audio_config():
    return StoredAudioConfig(**_load_last_config())


@app.post("/api/apply")
def apply_audio(config: AudioConfig, bt: BackgroundTasks):
    requested_mode = config.mode
    if os.path.exists(LAST_CONFIG_PATH):
        try:
            last_config = _load_last_config()
        except Exception:
            last_config = None
    else:
        last_config = None

    config = _normalize_config_for_device(config, requested_mode=requested_mode)
    
    # デバイスが無効または空の場合、自動的に利用可能なデバイスを選択
    if not config.device or config.device == "none" or config.device == "error":
        config.device = _get_first_valid_device(exclude_bluetooth=True)
    
    _ensure_dsp_prerequisites(config)
    needs_restart = _config_requires_restart(config, last_config)
    try:
        if config.mode == "dsp":
            saved_volume = float(last_config.get("volume", config.volume)) if last_config else config.volume
            config = AudioConfig(**{**config.model_dump(), "volume": saved_volume})
            if needs_restart:
                yp = generate_camilladsp_yaml(config)
                subprocess.Popen(["bash", SWITCH_AUDIO_SCRIPT, config.mode, config.device, yp])
                _schedule_init_vol(config.volume)
            else:
                _schedule_init_vol(config.volume)
        else:
            if needs_restart:
                subprocess.Popen(["bash", SWITCH_AUDIO_SCRIPT, config.mode, config.device, "none"])
        _save_last_config(config.model_dump())
        return {"status": "success"}
    except Exception as e:
        return JSONResponse(status_code=422, content={"status": "error", "message": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# Now Playing
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/api/now_playing")
async def get_now_playing():
    try:
        # Phase 2 修正: async 版 mpd_* を直接 await
        st = await mpd_status()
        so = await mpd_currentsong()
        return format_now_playing(st, so)
    except Exception:
        return JSONResponse(status_code=503, content={"error": "MPD offline"})


# ─────────────────────────────────────────────────────────────────────────────
# DSP プリセット API
# ─────────────────────────────────────────────────────────────────────────────
class PresetSave(BaseModel):
    name: str
    config: dict


@app.get("/api/presets")
def get_presets():
    return load_presets()


@app.post("/api/presets/save")
def save_preset(body: PresetSave):
    if not body.name.strip():
        return {"status": "error", "message": "名前を入力してください"}
    presets = load_presets()
    presets[body.name.strip()] = body.config
    save_presets(presets)
    return {"status": "success", "presets": presets}


@app.delete("/api/presets/{name}")
def delete_preset(name: str):
    presets = load_presets()
    if name in presets:
        del presets[name]
        save_presets(presets)
    return {"status": "success", "presets": presets}


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket — MPD Now Playing（イベント駆動型・ポーリング廃止）
# ─────────────────────────────────────────────────────────────────────────────
# Phase 2: _mpd_current_data は廃止。WebSocket は直接 async で
# mpd_connection を使う（_run_sync を経由しない）ため、
# 同期版の整形関数は不要になった。


@app.websocket("/ws/now_playing")
async def ws_now_playing(ws: WebSocket):
    """Now Playing WebSocket.

    Phase 2 修正: idle の async generator 取り扱いの複雑さを避けるため、
    シンプルな polling（2 秒ごと）に戻した。MPD 接続モデル自体は
    プロセス全体で 1 本の共有接続なので、ポーリングコストは小さい。
    """
    from hqmplayer_core.mpd import mpd_connection as _mpd_conn

    await ws_manager.connect(ws)

    # 前回送信した song_id を保持し、変更があったときだけ push する
    last_song_id: Optional[str] = None
    last_state: Optional[str] = None

    try:
        while True:
            try:
                async with _mpd_conn() as c:
                    st = await c.status()
                    so = await c.currentsong()
            except Exception:
                await asyncio.sleep(2)
                continue

            data = format_now_playing(st, so)
            song_id = data.get("song_id", "")
            state = data.get("state", "")

            # 曲 ID か state が変わったときだけ push（不要な push を抑制）
            if song_id != last_song_id or state != last_state:
                await ws.send_json(data)
                last_song_id = song_id
                last_state = state

            await asyncio.sleep(2)

    except WebSocketDisconnect:
        ws_manager.disconnect(ws)
    except Exception:
        try:
            ws_manager.disconnect(ws)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# アルバムアート
# ─────────────────────────────────────────────────────────────────────────────
import urllib.parse


# Phase 1d: 共通モジュールの _check_local_art を使う（重複を削除）
from hqmplayer_core.art import _check_local_art


@app.get("/api/art")
async def get_art(file: str, artist: str, album: str):
    # Phase 1d: アート解決戦略を共通モジュールに集約。
    # Phase 2 修正: async 化された resolve_art を await。
    result = await resolve_art(
        file=file,
        artist=artist,
        album=album,
        mpd_readpicture=mpd_readpicture,
        mpd_albumart=mpd_albumart,
        http_get=requests.get,
    )

    if result.source == "itunes" and result.redirect_url:
        return RedirectResponse(result.redirect_url)

    return Response(content=result.content, media_type=result.media_type)