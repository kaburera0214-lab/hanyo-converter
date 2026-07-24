# -*- coding: utf-8 -*-
"""
Chatworkへの通知（メッセージ／タスク作成）。

アラートは見逃さないよう**タスク化**する（担当者に割り当て・期限つき）。
Secrets（Streamlit Cloud）:
  CHATWORK_API_TOKEN      … Chatwork APIトークン
  CHATWORK_ALERT_ROOM_ID  … 通知先ルームID（例: soukoルーム）
  CHATWORK_ALERT_TO_IDS   … 担当者のアカウントID（カンマ区切り or リスト）
未設定なら送信しない（Falseを返す）。
"""
import datetime

import requests

API = "https://api.chatwork.com/v2"
TIMEOUT = 20


def _secret(key):
    import streamlit as st
    v = st.secrets.get(key, "")
    return v


def _token():
    return str(_secret("CHATWORK_API_TOKEN")).strip()


def _room():
    return str(_secret("CHATWORK_ALERT_ROOM_ID")).strip()


def _to_ids():
    raw = _secret("CHATWORK_ALERT_TO_IDS")
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x).strip()]
    return [x.strip() for x in str(raw).split(",") if x.strip()]


def is_configured():
    """タスク作成に必要なトークン・ルーム・担当者が揃っているか。"""
    return bool(_token()) and bool(_room()) and bool(_to_ids())


def create_task(body, to_ids=None, room_id=None, limit_days=3):
    """Chatworkにタスクを作成する（担当者に割り当て）。成功でTrue。
    未設定・失敗時はFalse（例外は投げない＝呼び出し元の処理を妨げない）。"""
    try:
        token, room = _token(), (room_id or _room())
        ids = to_ids or _to_ids()
        if not token or not room or not ids:
            return False
        data = {"body": body, "to_ids": ",".join(str(i) for i in ids)}
        if limit_days:
            due = datetime.datetime.now() + datetime.timedelta(days=limit_days)
            data["limit"] = str(int(due.timestamp()))
            data["limit_type"] = "date"
        r = requests.post(f"{API}/rooms/{room}/tasks",
                          headers={"X-ChatWorkToken": token}, data=data, timeout=TIMEOUT)
        return r.status_code < 300
    except Exception:  # noqa: BLE001
        return False
