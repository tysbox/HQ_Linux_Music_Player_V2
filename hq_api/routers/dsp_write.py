"""DSP 書き込み系ルータ（Phase X-3: 段階的移植）.

backend/main.py から副作用付き API を移植。リスクの低い順に:

1. POST /api/presets/save  ← ファイル I/O のみ ✅ X-3-1
2. POST /api/presets/{name} (DELETE)  ← ファイル I/O のみ ✅ X-3-1
3. POST /api/dsp_restart  ← CamillaDSP 再起動（中リスク）✅ X-3-2
4. POST /api/volume  ← CamillaClient 接続（中リスク）✅ X-3-2
5. POST /api/apply  ← CamillaDSP 再起動 + ALSA 切替（高音圧）X-3-3
"""
import json
import os
import sys
import time

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from hqmplayer_core.mpd import mpd_connection

# backend.main._update_last_config を使うため backend/ を sys.path に追加
_BACKEND_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "backend",
)
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

router = APIRouter()

# 設定ファイルパス（DSP 側と共有）
PRESETS_PATH = os.path.expanduser("~/.config/audiophile/presets.json")
LAST_CONFIG_PATH = os.path.expanduser("~/.config/audiophile/last_config.json")
SWITCH_AUDIO_SCRIPT = os.path.expanduser(
    "/home/tysbox/HQ_Linux_Music_Player_v2-/backend/scripts/switch_audio.sh"
)


# ─────────────────────────────────────────────────────────────────────────────
# Phase X-3-1: ファイル I/O のみ（低リスク）
# ─────────────────────────────────────────────────────────────────────────────
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


class PresetSave(BaseModel):
    name: str
    config: dict


@router.post("/api/presets/save")
def save_preset(body: PresetSave):
    """DSP:8000 と完全互換のプリセット保存."""
    if not body.name.strip():
        return {"status": "error", "message": "名前を入力してください"}
    presets = load_presets()
    presets[body.name.strip()] = body.config
    save_presets(presets)
    return {"status": "success", "presets": presets}


@router.delete("/api/presets/{name}")
def delete_preset(name: str):
    """DSP:8000 と完全互換のプリセット削除."""
    presets = load_presets()
    if name in presets:
        del presets[name]
        save_presets(presets)
    return {"status": "success", "presets": presets}


# ─────────────────────────────────────────────────────────────────────────────
# Phase X-3-2: CamillaClient 経由（中リスク）
# ─────────────────────────────────────────────────────────────────────────────
class VolumeControl(BaseModel):
    volume: float


@router.post("/api/volume")
def set_volume(vol: VolumeControl):
    """DSP:8000 と完全互換のボリューム設定.

    CamillaDSP のメイン音量を即座に変更。再生は途切れない。
    CamillaDSP 未起動時は最大3回 (各200ms) リトライ。

    2026-09-06 課題 2: DSP_LOCK で /api/apply /api/dsp_update と同時実行を直列化。
    """
    from hq_api.main import DSP_LOCK
    with DSP_LOCK:
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                from camilladsp import CamillaClient
                c = CamillaClient("127.0.0.1", 1234)
                c.connect()
                c.volume.set_main_volume(vol.volume)
                c.disconnect()
                # last_config.volume を更新 (DSP:8000 /api/volume と同じ挙動)。
                # HANDOVER0907 §3 の「Apply時に直前の音量に戻る」仕様を維持するため
                # ユーザー指定音量を永続化する。
                try:
                    from backend.main import _update_last_config
                    _update_last_config({"volume": float(vol.volume)})
                except Exception:
                    pass
                return {"status": "success", "attempts": attempt + 1}
            except Exception as e:
                last_err = e
                time.sleep(0.2)
        # 最終失敗 — 503 で返却 (CamillaDSP 未起動は 422 より 503 が適切)
        raise HTTPException(status_code=503, detail=f"CamillaDSP unreachable after 3 retries: {last_err}")


# Phase 2-A: 旧ローカル再実装は backend/main.py に一本化されたため削除済み。
