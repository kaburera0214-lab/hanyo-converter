# -*- coding: utf-8 -*-
"""
Chatworkへの通知（メッセージ／タスク作成）。

アラートは見逃さないよう**タスク化**する（担当者に割り当て・期限つき）。
宛先は audience で分ける（既定はSTAFF＝souko）:
  STAFF … 現場スタッフが自分で直せること（NEの再認可など）だけ
  ADMIN … バッチの不具合・無料枠超過など開発担当者しか直せないもの

Secrets（Streamlit Cloud）:
  CHATWORK_API_TOKEN      … Chatwork APIトークン
  CHATWORK_ALERT_ROOM_ID  … スタッフ向け通知先ルームID（既定: soukoのDM）
  CHATWORK_ALERT_TO_IDS   … スタッフ側の担当者アカウントID（カンマ区切り or リスト）
  CHATWORK_ADMIN_ROOM_ID  … 管理者向け通知先ルームID（既定: 犬飼翔太のDM）
  CHATWORK_ADMIN_TO_IDS   … 管理者のアカウントID
未設定なら送信しない（Falseを返す）。
"""
import datetime

import requests

API = "https://api.chatwork.com/v2"
TIMEOUT = 20

# 既存のchatwork-checkと同じ運用の既定値（IDは秘密情報でないためコードに置く。Secretsで上書き可）。
# souko puppy land = account_id 6858076 / DMルーム 270228986（members.json より）。
DEFAULT_ROOM_ID = "270228986"
DEFAULT_TO_IDS = ["6858076"]

# 通知の宛先は「誰が直せるか」で分ける。soukoは現場スタッフが見るアカウントなので、
# スタッフが自分で直せること（NEの再認可＝ブラウザでログインするだけ）だけを送る。
# バッチの不具合・無料枠超過など開発者しか直せないものは管理者へ送る（スタッフを困らせない）。
# 犬飼翔太 = account_id 4003238 / DMルーム 155989580（members.json より）。
ADMIN_ROOM_ID = "155989580"
ADMIN_TO_IDS = ["4003238"]
STAFF, ADMIN = "staff", "admin"


def _secret(key):
    import streamlit as st
    v = st.secrets.get(key, "")
    return v


def _token():
    return str(_secret("CHATWORK_API_TOKEN")).strip()


def _room(audience=STAFF):
    if audience == ADMIN:
        return str(_secret("CHATWORK_ADMIN_ROOM_ID")).strip() or ADMIN_ROOM_ID
    return str(_secret("CHATWORK_ALERT_ROOM_ID")).strip() or DEFAULT_ROOM_ID


def _to_ids(audience=STAFF):
    key = "CHATWORK_ADMIN_TO_IDS" if audience == ADMIN else "CHATWORK_ALERT_TO_IDS"
    raw = _secret(key)
    if isinstance(raw, (list, tuple)):
        ids = [str(x).strip() for x in raw if str(x).strip()]
    else:
        ids = [x.strip() for x in str(raw).split(",") if x.strip()]
    return ids or list(ADMIN_TO_IDS if audience == ADMIN else DEFAULT_TO_IDS)


def is_configured():
    """タスク作成に必要なトークン・ルーム・担当者が揃っているか。"""
    return bool(_token()) and bool(_room()) and bool(_to_ids())


def create_task(body, to_ids=None, room_id=None, limit_days=3, audience=STAFF):
    """Chatworkにタスクを作成する（担当者に割り当て）。成功でTrue。
    未設定・失敗時はFalse（例外は投げない＝呼び出し元の処理を妨げない）。
    audience=STAFF … 倉庫スタッフ（souko）／ADMIN … 開発担当者。docstring冒頭の方針を参照。"""
    try:
        token, room = _token(), (room_id or _room(audience))
        ids = to_ids or _to_ids(audience)
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
