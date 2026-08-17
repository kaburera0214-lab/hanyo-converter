# -*- coding: utf-8 -*-
"""
NEトークンの延命（keep-alive）と、その稼働状態の記録。

NEのrefresh_tokenは発行から3日で切れる（access_tokenは1日）。入荷登録は「初入荷の都度」の
不定期利用、マスタ同期は週次なので、放っておくと3日の空白ができて必ず失効する
＝現場が使いたいときに「002004 認証が切れています」になる。
NE公式も、バッチ用途では2日より前に定期的にAPIを呼んで期限を切らさない運用を推奨している。

そこで軽いAPIを毎日1回だけ呼んでトークンを転がし続ける（batch/ne_keepalive.py）。
実行結果は Drive の ne_keepalive.json に残し、
  - 管理画面（入荷登録ページの🔐）で「延命バッチが生きているか」を表示
  - 失効したときだけChatworkへ通知（人手の再認可が必要なのはこのときだけ）
に使う。※NEの再認可はブラウザでのログインが必須で、完全自動化はできない。
"""
import datetime
import json

STATE_NAME = "ne_keepalive.json"     # Drive（PRODUCT_MASTER_FOLDER_ID）に保存
ALERT_COOLDOWN_DAYS = 3              # 失効通知の再送間隔（毎日タスクが増えるのを防ぐ）


def _folder():
    from lib import master_store
    return master_store.folder_id()


def load_state():
    """前回の実行結果。読めなければ空dict（表示・判定側で「不明」扱い）。"""
    try:
        from lib.invoice import drive_master
        f = drive_master.find_file(STATE_NAME, _folder())
        if f:
            return json.loads(drive_master.download_bytes(f["id"]).decode("utf-8"))
    except Exception:  # noqa: BLE001
        pass
    return {}


def save_state(state):
    """実行結果を保存する（失敗しても本処理は妨げない）。"""
    try:
        from lib.invoice import drive_master
        drive_master.upload_or_replace(
            json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8"),
            STATE_NAME, _folder(), mimetype="application/json")
        return True
    except Exception:  # noqa: BLE001
        return False


def should_alert(state, today=None):
    """失効を通知すべきか（純関数・テスト対象）。
    直近ALERT_COOLDOWN_DAYS日以内に通知済みなら送らない。"""
    today = today or datetime.date.today()
    last = str(state.get("last_alert_date", "")).strip()
    if not last:
        return True
    try:
        prev = datetime.date.fromisoformat(last)
    except ValueError:
        return True
    return (today - prev).days >= ALERT_COOLDOWN_DAYS


def run(now=None):
    """延命APIを1回呼び、結果をDriveへ記録して返す。
    返り値: {ok, rotated, message, state}。例外は投げない（バッチ側で終了コードに変換）。"""
    from . import client
    now = now or datetime.datetime.now()
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    state = load_state()
    try:
        info = client.keep_alive()
        state.update({"last_ok": stamp, "last_result": "ok",
                      "rotated": info.get("rotated", False), "last_error": ""})
        state.pop("last_alert_date", None)   # 復旧したら次の失効で必ず通知できるようにする
        save_state(state)
        return {"ok": True, "rotated": info.get("rotated", False),
                "message": ("トークンを更新しました" if info.get("rotated")
                            else "トークンは有効です（更新不要）"),
                "state": state}
    except client.NEAuthError as e:
        state.update({"last_run": stamp, "last_result": "auth_error", "last_error": str(e)})
        alert = should_alert(state, now.date())
        if alert:
            state["last_alert_date"] = now.date().isoformat()
        save_state(state)
        return {"ok": False, "auth": True, "alert": alert, "message": str(e), "state": state}
    except Exception as e:  # noqa: BLE001
        state.update({"last_run": stamp, "last_result": "error", "last_error": str(e)})
        save_state(state)
        return {"ok": False, "auth": False, "alert": True, "message": str(e), "state": state}
