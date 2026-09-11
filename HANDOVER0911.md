# HANDOVER 2026-09-11 — 環境構築・DSP/アート復旧・未着手課題

> **作成**: 2026-09-11 17:10 JST
> **対象**: `HQ_Linux_Music_Player_v2-` / `clean-refac` ブランチ
> **前回**: [HANDOVER0907.md](HANDOVER0907.md)（Phase 1-5 完了）

---

## 1. 本日実施した修正（コミット対象）

### 1-1. 環境構築（Loopback/DSP経路）

| 項目 | 修正内容 | 検証 |
|---|---|---|
| `snd-aloop` | `modprobe snd-aloop` + `/etc/modules-load.d/snd-aloop.conf` 配置 | `Loopback` card1 認識、再起動後も有効 |
| `/etc/asound.conf` | `config/asound.conf` を配置 | `aplay -l` で確認 |
| `/etc/mpd/mpd_local.conf` | 新規作成: `ALSA Loopback (hw:Loopback,0,0, 192kHz:32:2)` + `PC Speaker (plughw:0,0)` | `mpc outputs` で2出力確認 |
| `/etc/mpd.conf` | `include_optional "mpd_local.conf"` → `include_optional "/etc/mpd/mpd_local.conf"` 絶対パス化 | MPD再起動で出力が有効化 |
| MPD/Loopback/CamillaDSP | 全て `192kHz/S32_LE` 統一 | `hw_params` で `192000/S32_LE` 確認 |

### 1-2. コード修正（5ファイル）

| ファイル | 修正内容 | 緊急度 |
|---|---|---|
| `backend/main.py` | `has_reverb` → `has_reverb_effective` 再評価（IR失敗時に `mix` mixer が残るバグ修正） | 高 |
| `backend/main.py` | `resampler: AsyncPoly/Cubic` → `AsyncSinc/Balanced` | 中 |
| `hq_api/routers/dsp_write.py:36` | `HQ_Linux_Music_Player` → `HQ_Linux_Music_Player_v2-` | 高（次回起動でDSP切替失敗） |
| `hq_api/ws/status.py:88` | 同上 | 高（履歴追加が毎回失敗） |
| `hqmplayer_core/meta/formatting.py` | `format_now_playing` に `artwork_url` 追加（WebSocket経由でアートが届かないバグ修正） | 高 |
| `hq_api/hq-api.service` | `Requires=mpd` 削除（`Wants` のみに）、`Restart=on-failure` → `Restart=always` | 高（MPD再起動でhq-api連動停止） |

### 1-3. 前回コミット（clean-refac）

| 項目 | 内容 |
|---|---|
| `frontend/` `dmp/frontend/` `.git.bak.*` 削除 | 旧3000/3001フロントエンド除去 |
| `hq_api/hq-api.service` パス修正 | `HQ_Linux_Music_Player` → `_v2-`, `backend/venv` → `.venv` |
| `unified-shell.service` 新規作成 | port 3002 standalone |
| `tsconfig.json` | `playwright.config.ts`/`e2e` を exclude |
| `architecture-refactor-d` 21 commits マージ | DSP_LOCK, volume強制復帰, iTunes art, IR 4種等 |

---

## 2. 検証済み動作（2026-09-11 17:00 時点）

| シナリオ | 結果 |
|---|---|
| MPD → Loopback → CamillaDSP → PCH (DSP mode) | ✅ `192kHz/S32_LE` で再生、Loopback capture 確立 |
| MPD → PCH 直接 (Pure mode) | ✅ 再生 |
| Apply (DSP, reverb=none, crossfeed=light) | ✅ `mix` なしで正常起動（修正前は `mix` エラーで無音） |
| Apply (DSP, reverb=hall等 IR不足) | ✅ `mix` なしで正常起動（修正前は無音） |
| アート表示（UPnP queue） | ✅ `artwork_url` が WebSocket 経由で届く |
| `mpd restart` 後の `hq-api` | ✅ `active` 維持（修正前は連動停止） |
| `hq-api:8002` / `unified-shell:3002` / `mpd` / `upmpdcli` | ✅ 全て `active` |

---

## 3. 未着手の課題一覧（全23件）

### 3-1. 高優先度（次回起動・動作に影響）— 3件

| # | 区分 | ファイル | 内容 | 対応案 |
|---|---|---|---|---|
| H-1 | conflict | `backend/audiophile-backend.service` `dmp/backend/dmp-backend.service` | 旧パス `HQ_Linux_Music_Player` のまま（未enableのため現状影響なしだがリポジトリ不整合） | パスを `_v2-` に修正、または削除（hq-apiに統合済みのため不要なら削除） |
| H-2 | error | `hq_api` 全体 | `except Exception: pass` の無ログ握りつぶし多数（`hqmplayer_core/mpd/client.py:66,88`, `backend/main.py:127,158` 等） | `logger.warning` 追加 |
| H-3 | error | `hq_api/main.py:138` | `/health` がMPD切断時も `200 degraded`（`503` であるべき） | ステータスコード修正 |

### 3-2. 中優先度（リファクタ・保守性）— 10件

| # | 区分 | ファイル | 内容 | 対応案 |
|---|---|---|---|---|
| M-1 | redundancy | `backend/main.py` vs `hq_api/routers/dsp_apply.py` | `AudioConfig`/`VolumeControl`/`PresetSave` 重複 | `hqmplayer_core/models.py` に一元化 |
| M-2 | redundancy | `backend/main.py:228` vs `hq_api/routers/dsp.py:22` | `_detect_alsa_cards()` コピペ重複 | `hqmplayer_core` に移動 |
| M-3 | redundancy | `backend/main.py:500` | `generate_camilladsp_yaml()` が `backend` のみに存在、`hq_api` は `sys.path` ハックで import | `hqmplayer_core/dsp/yaml.py` に移動 |
| M-4 | inefficiency | `hq_api/routers/dsp_apply.py:31` 等4箇所 | `sys.path.insert` 分散 | `hq_api/__init__.py` に集約 or `pip -e .` |
| M-5 | inefficiency | `hq_api/main.py:47` | `DSP_LOCK = threading.Lock()` が `async def` と混在でイベントループをブロック | `asyncio.Lock` に置換（async化が必要） |
| M-6 | inefficiency | `backend/main.py:724` | `_schedule_init_vol` が毎回 `threading.Thread(daemon=True)` + `sleep(0.05)*200` | スレッドプール化 or `asyncio` 化 |
| M-7 | conflict | `backend/requirements.txt` vs `dmp/backend/requirements.txt` | `requests`/`PyYAML`/`camilladsp` が `dmp` に欠落、`hq_api` は `requirements.txt` なし | `hq_api/requirements.txt` 作成、統一 |
| M-8 | conflict | `.venv` vs `backend/venv` vs `dmp/backend/venv` | 3 venv併存、`pyproject.toml` なし | 一元化 |
| M-9 | inefficiency | `unified-shell/src/app/page.tsx:280` | VUメーター `requestAnimationFrame` 60fpsで全体再レンダー | canvas/ref に分離 |
| M-10 | inefficiency | `unified-shell/src/app/page.tsx:52` | `SeekBar`/`TBtn` は `memo` だが親が毎回新しいアローを渡し無効 | `useCallback` 化 |

### 3-3. 低優先度（ドキュメント・テスト・軽微）— 10件

| # | 区分 | ファイル | 内容 | 対応案 |
|---|---|---|---|---|
| L-1 | conflict | `docs/` `README.md` 多数 | 旧パス `HQ_Linux_Music_Player` / 旧ポート `8000/3000` の記述残存 | 一括置換 |
| L-2 | redundancy | `unified-shell/package.json` | `is-mobile`/`react-draggable`/`framer-motion`/`clsx`/`tailwind-merge` 未使用のままインストール | `npm prune` / `depcheck` |
| L-3 | conflict | `unified-shell/package.json` | `playwright ^1.52.0` と `@playwright/test ^1.63.0` 二重 | `@playwright/test` のみに統一 |
| L-4 | conflict | `unified-shell/tsconfig.json` | `strict:false`/`allowJs:true`/`skipLibCheck:true` で型安全性低下 | `strict:true` 化 |
| L-5 | redundancy | `hq_api/routers/dsp_write.py:16` 等 | `BackgroundTasks` import未使用 | 削除 |
| L-6 | redundancy | `backend/main.py:96` | `mpd_connect()` レガシー同期ラッパー残存 | 削除 |
| L-7 | redundancy | `unified-shell/src/components/NowPlayingBar.tsx` | `return null` のデッドファイル | 削除 |
| L-8 | inefficiency | `tests/snapshots/` 17ファイル | `dmp__`/`dsp__` プレフィックスのまま、`hq_api:8002` 統合後のスナップショットなし | 更新 |
| L-9 | inefficiency | `tests/` | `mpd`/`queue`/`history`/`dsp`/`websocket` カバレッジなし | 追加 |
| L-10 | redundancy | `unified-shell/src/lib/api.ts` vs `upnpApi.ts` | `BASE`/`req()` 二重実装 | `lib/fetcher.ts` に統一 |

---

## 4. 環境メモ

| 項目 | 値 |
|---|---|
| OS | Debian trixie / MX Linux |
| Python | 3.13.5, `.venv` at `HQ_Linux_Music_Player_v2-/.venv` |
| Node | v20.19.2 |
| MPD | 0.24.4, `mpd_local.conf` で Loopback/PC Speaker 定義 |
| upmpdcli | 1.9.17 (lesbonscomptes trixie) |
| CamillaDSP | 4.0.0 (b0ae57d), `/usr/local/bin/camilladsp` |
| ALSA | Loopback card1, PCH card0, 全て 192kHz/S32_LE 統一 |
| 起動 | `mpd` → `hq-api:8002` → `unified-shell:3002` / `upmpdcli` (systemd enable済み) |
| CamillaDSP | オンデマンド（`switch_audio.sh` 経由、DSP mode時のみ） |

---

## 5. 次回作業時の確認コマンド

```bash
# 全サービス状態
systemctl is-active mpd hq-api unified-shell upmpdcli && ps aux | grep camilla | grep -v grep
# MPD出力
mpc outputs && mpc status | head -5
# Loopback
cat /proc/asound/Loopback/pcm0p/sub0/hw_params | head -5; cat /proc/asound/Loopback/pcm1c/sub0/hw_params | head -5
# hq_api
curl -sf http://localhost:8002/health | python3 -m json.tool
# CamillaDSP YAML
cat /tmp/camilladsp/active_dsp.yml | head -20
```
