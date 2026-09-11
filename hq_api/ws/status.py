"""/ws/status — DMP 互換 WebSocket (Phase X-2).

dmp/backend/app/routers/websocket.py の websocket_status を移植。
idle() イベント駆動で、曲変化時に履歴自動追加も実施。
"""
import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from hqmplayer_core.mpd import mpd_connection, MPD_HOST, MPD_PORT

logger = logging.getLogger(__name__)

router = APIRouter()


async def _get_full_status(client) -> dict:
    """DMP 互換の full status dict を返す."""
    status = await client.status()
    current_song = None
    try:
        song = await client.currentsong()
        if song:
            current_song = {
                "id": song.get("id", ""),
                "title": song.get("title", ""),
                "artist": song.get("artist", ""),
                "album": song.get("album", ""),
                "duration": int(float(song.get("duration", 0) or 0)),
                "track": song.get("track", ""),
                "file": song.get("file", ""),
            }
    except Exception:
        pass

    return {
        "state": status.get("state", "stop"),
        "current_track": current_song,
        "position": int(float(status.get("elapsed", 0) or 0)),
        "duration": int(float(status.get("duration", 0) or 0)),
        "queue_length": int(status.get("playlistlength", 0)),
        "random": status.get("random") == "1",
        "repeat": status.get("repeat") == "1",
        "song_id": status.get("songid"),
    }


@router.websocket("/ws/status")
async def websocket_status(websocket: WebSocket):
    """Status WebSocket — DMP:8001 と完全互換.

    idle() イベント駆動で player/mixer/playlist/options 変更を検知し、
    変化時に full status を push。曲変化時は履歴に自動追加。
    """
    await websocket.accept()
    logger.info("DMP-compat WebSocket 接続確立")

    try:
        async with mpd_connection() as status_client:
            initial = await _get_full_status(status_client)
        await websocket.send_text(json.dumps(initial))
    except Exception as e:
        logger.warning("ws_status initial push failed: %s", e)
        await websocket.close()
        return

    prev_song_id = initial.get("song_id")

    try:
        async with mpd_connection(purpose="playback") as idle_client:
            async for changed in idle_client.idle(["player", "mixer", "playlist", "options"]):
                try:
                    async with mpd_connection() as client:
                        status_data = await _get_full_status(client)
                        status_data["changed"] = list(changed)
                        current_song_id = status_data.get("song_id")
                        # 曲変化時の履歴自動追加
                        if (
                            "player" in changed
                            and current_song_id != prev_song_id
                            and status_data.get("current_track")
                        ):
                            try:
                                # history_service は hq_api では DMP 由来を使う
                                import sys
                                dmp_backend = "/home/tysbox/HQ_Linux_Music_Player_v2-/dmp/backend"
                                if dmp_backend not in sys.path:
                                    sys.path.insert(0, dmp_backend)
                                from app.services.history_service import add_to_history
                                from app.models.track import Track
                                track = Track(**status_data["current_track"])
                                add_to_history(track)
                                logger.debug(f"履歴追加: {track.title}")
                            except Exception as e:
                                logger.warning(f"履歴追加失敗: {e}")
                        prev_song_id = current_song_id
                        # song_id は送らない（DMP 互換）
                        status_data.pop("song_id", None)
                        await websocket.send_text(json.dumps(status_data))
                except Exception as e:
                    logger.warning("ws_status iteration error: %s", e)
                    await asyncio.sleep(1)
    except WebSocketDisconnect:
        logger.info("DMP-compat WebSocket 切断")
    except Exception as e:
        logger.error(f"ws_status error: {e}")
        try:
            await websocket.send_text(json.dumps({"type": "error", "message": str(e)}))
        except Exception:
            pass
