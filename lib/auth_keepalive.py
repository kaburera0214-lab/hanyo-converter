# -*- coding: utf-8 -*-
"""
認可（OAuth）の延命を、接続先を問わない共通の仕組みとして扱う。

【なぜ必要か】
どの接続先も、一定期間APIを呼ばないとリフレッシュトークンごと失効し、
ブラウザでの再認可が必要になる。価格改定も入荷登録も「都度」の不定期利用なので、
放っておけば必ず空白期間ができて失効する。使いたいときに止まる。

  ネクストエンジン … refresh_token は 3日
  Yahoo           … refresh_token は 28日（ストアクリエイターProに公開鍵登録済みの場合。
                     未登録は12時間。公開鍵は2026-05-06発行・2027-05-06まで有効）

そこで毎日1回、各接続先のトークンを転がして期限を巻き直す。

【自動再認可はできない】
どちらも認可コードフローで、リフレッシュトークンが死んだ後の復帰には
「人間がブラウザでログインして同意する」操作が必須（API側の仕様）。
自動化できるのは「切らさないこと」と「切れたら即座に知らせること」まで。

【楽天RMSを入れていない理由】
RMSはserviceSecret/licenseKeyの固定値で、転がして延命する仕組みがない
（期限が来たらRMS管理画面で更新するしかない）。延命の対象にはならないので、
ここでは扱わず、期限監視は稼働監視ダッシュボード側の課題とする。
"""
import datetime

ALERT_COOLDOWN_DAYS = 3      # 失効通知の再送間隔（毎日タスクが増えるのを防ぐ）


def _folder():
    from lib import master_store
    return master_store.folder_id()


def load_state(state_name):
    """前回の実行結果。読めなければ空dict（表示・判定側で「不明」扱い）。"""
    try:
        from lib.invoice import drive_master
        f = drive_master.find_file(state_name, _folder())
        if f:
            import json
            return json.loads(drive_master.download_bytes(f["id"]).decode("utf-8"))
    except Exception:  # noqa: BLE001
        pass
    return {}


def save_state(state_name, state):
    """実行結果を保存する（失敗しても本処理は妨げない）。"""
    try:
        import json
        from lib.invoice import drive_master
        drive_master.upload_or_replace(
            json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8"),
            state_name, _folder(), mimetype="application/json")
        return True
    except Exception:  # noqa: BLE001
        return False


def should_alert(state, today=None, cooldown_days=ALERT_COOLDOWN_DAYS):
    """失効を通知すべきか（純関数・テスト対象）。
    直近 cooldown_days 日以内に通知済みなら送らない。"""
    today = today or datetime.date.today()
    last = str(state.get("last_alert_date", "")).strip()
    if not last:
        return True
    try:
        prev = datetime.date.fromisoformat(last)
    except ValueError:
        return True
    return (today - prev).days >= cooldown_days


# ---------------------------------------------------------------- 接続先の定義

def _ne_provider():
    from lib.ne_api import client, keepalive as ne_keepalive
    return {
        "key": "ne",
        "label": "ネクストエンジン",
        "state_name": ne_keepalive.STATE_NAME,   # 既存の ne_keepalive.json を使い続ける
        "auth_error": client.NEAuthError,
        "touch": client.keep_alive,
        "is_configured": lambda: True,           # NEは常に必須（未設定なら失敗として出す）
        "lifetime": "3日",
        # 再認可はNEのID・パスワードでできる＝倉庫スタッフが自分で完結できる
        "reauth_audience": "staff",
    }


def _yahoo_provider():
    from lib.yahoo_api import client
    return {
        "key": "yahoo",
        "label": "Yahoo",
        "state_name": "yahoo_keepalive.json",
        "auth_error": client.YahooAuthError,
        "touch": client.keep_alive,
        "is_configured": client.is_configured,
        "lifetime": "28日",
        # Yahooの再認可には「店舗オーナーのYahoo ID」が要る。倉庫スタッフは
        # 持っていないので現場に投げても動けない＝管理者宛にする。
        "reauth_audience": "admin",
    }


PROVIDER_BUILDERS = {"ne": _ne_provider, "yahoo": _yahoo_provider}


def providers(keys=None):
    """有効な接続先の定義を返す。import自体が失敗しても他を止めない。"""
    out = []
    for key in (keys or list(PROVIDER_BUILDERS)):
        try:
            out.append(PROVIDER_BUILDERS[key]())
        except Exception as exc:  # noqa: BLE001
            out.append({"key": key, "label": key, "broken": str(exc)})
    return out


# ---------------------------------------------------------------- 実行

def run_one(provider, now=None):
    """1つの接続先を延命する。例外は投げない（呼び出し側で終了コードに変換）。

    返り値: {key, label, ok, skipped, auth, alert, rotated, message, state}
    """
    now = now or datetime.datetime.now()
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    key, label = provider["key"], provider["label"]

    if provider.get("broken"):
        return {"key": key, "label": label, "ok": False, "auth": False,
                "alert": True, "message": f"モジュールを読み込めません: {provider['broken']}",
                "state": {}}

    # 未設定は「異常」ではない（Yahoo未導入の環境でも動くように）。
    # ただし黙って成功にはせず、skipped として結果に残す。
    try:
        if not provider["is_configured"]():
            return {"key": key, "label": label, "ok": True, "skipped": True,
                    "message": "未設定のためスキップしました", "state": {}}
    except Exception as exc:  # noqa: BLE001
        return {"key": key, "label": label, "ok": False, "auth": False, "alert": True,
                "message": f"設定を確認できません: {exc}", "state": {}}

    state_name = provider["state_name"]
    state = load_state(state_name)

    try:
        info = provider["touch"]() or {}
        rotated = bool(info.get("rotated"))
        state.update({"last_ok": stamp, "last_result": "ok",
                      "rotated": rotated, "last_error": ""})
        if info.get("expires_at"):
            state["access_token_expires_at"] = info["expires_at"]
        # 復旧したら、次に失効したとき必ず通知できるようにする
        state.pop("last_alert_date", None)
        save_state(state_name, state)
        return {"key": key, "label": label, "ok": True, "rotated": rotated,
                "message": ("トークンを更新しました" if rotated
                            else "トークンは有効です（更新不要）"),
                "state": state}
    except provider["auth_error"] as exc:
        # 失効。ブラウザでの再認可が必要＝現場スタッフが自分でできる
        state.update({"last_run": stamp, "last_result": "auth_error",
                      "last_error": str(exc)})
        alert = should_alert(state, now.date())
        if alert:
            state["last_alert_date"] = now.date().isoformat()
        save_state(state_name, state)
        return {"key": key, "label": label, "ok": False, "auth": True,
                "alert": alert, "message": str(exc), "state": state,
                "reauth_audience": provider.get("reauth_audience", "staff")}
    except Exception as exc:  # noqa: BLE001
        # バッチ側の不具合。スタッフには直せないので管理者へ
        state.update({"last_run": stamp, "last_result": "error", "last_error": str(exc)})
        save_state(state_name, state)
        return {"key": key, "label": label, "ok": False, "auth": False,
                "alert": True, "message": str(exc), "state": state}


def run_all(keys=None, now=None):
    """全接続先を延命する。1つが失敗しても他は必ず実行する。"""
    return [run_one(p, now=now) for p in providers(keys)]


def summarize(results):
    """終了コードと通知の判断に使う集計。"""
    return {
        "total": len(results),
        "ok": sum(1 for r in results if r.get("ok") and not r.get("skipped")),
        "skipped": sum(1 for r in results if r.get("skipped")),
        "auth_error": sum(1 for r in results if r.get("auth")),
        "error": sum(1 for r in results if not r.get("ok") and not r.get("auth")),
    }
