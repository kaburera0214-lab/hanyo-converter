# -*- coding: utf-8 -*-
"""
請求書発行機能のデータ永続化。

クライアント情報・保管料種別マスタなどを invoice_data/ 配下のJSONに保存し、
任意でGoogle Driveへバックアップする。Driveの認証は既存ページと同じ
リフレッシュトークン方式（Secretsキーを流用、書き込みは新規フォルダのみ）。

※ 他機能のデータ・設定ファイルには一切触れない。
"""
import json
import os

# このファイル(lib/invoice/)から見たリポジトリ直下の invoice_data/
_BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA_DIR = os.path.join(_BASE_DIR, "invoice_data")

# 保管料種別マスタのTeam-EC初期値（保管料管理シートの実例より）
DEFAULT_CLIENTS = {
    "Team-EC": {
        "略号": "TE",
        "header": {
            "取引先名称": "Team-EC株式会社",
            "件名": "物流業務委託費",
            "取引先郵便番号": "192-0052",
            "取引先都道府県": "東京都",
            "取引先住所1": "八王子市",
            "取引先住所2": "本郷町10-6 LC BLDG.",
            "取引先敬称": "",
            "備考": "振込手数料は御社負担でお願い致します。",
            "振込先": "楽天銀行 第二営業支店（252）普通預金 7484378 カ）パピ－",
            "自社担当者氏名": "",
        },
        # 保管料種別マスタ：種別名 -> {単価, 出力品名}
        "保管料マスタ": [
            {"種別名": "ライオンズ：パレット", "単価": 1000, "出力品名": "保管料：パレット"},
            {"種別名": "イーグルス：パレット", "単価": 1000, "出力品名": "保管料：パレット"},
            {"種別名": "スワローズ：パレット", "単価": 1000, "出力品名": "保管料：パレット"},
            {"種別名": "トイプー：ラック棚板(90)", "単価": 300, "出力品名": "保管料：中量棚"},
        ],
        # 単価マスタ（出荷作業料・資材費は配送種別別。実例の単価表より）
        "単価マスタ": [
            {"費目": "出荷作業", "種別": "nekop", "単価": 52, "出力品名": "出荷作業料"},
            {"費目": "出荷作業", "種別": "60", "単価": 84, "出力品名": "出荷作業料"},
            {"費目": "出荷作業", "種別": "80", "単価": 84, "出力品名": "出荷作業料"},
            {"費目": "出荷作業", "種別": "100", "単価": 84, "出力品名": "出荷作業料"},
            {"費目": "出荷作業", "種別": "120", "単価": 105, "出力品名": "出荷作業料"},
            {"費目": "出荷作業", "種別": "140", "単価": 210, "出力品名": "出荷作業料"},
            {"費目": "出荷作業", "種別": "160", "単価": 210, "出力品名": "出荷作業料"},
            {"費目": "資材", "種別": "nekop", "単価": 25.46, "出力品名": "資材費"},
            {"費目": "資材", "種別": "60", "単価": 51.46, "出力品名": "資材費"},
            {"費目": "資材", "種別": "80", "単価": 71.51, "出力品名": "資材費"},
            {"費目": "資材", "種別": "100", "単価": 135.64, "出力品名": "資材費"},
            {"費目": "資材", "種別": "120", "単価": 166.98, "出力品名": "資材費"},
            {"費目": "資材", "種別": "140", "単価": 218.60, "出力品名": "資材費"},
            {"費目": "資材", "種別": "160", "単価": 266.26, "出力品名": "資材費"},
            {"費目": "受注作業", "種別": "", "単価": 0, "出力品名": "受注作業料"},
            {"費目": "送料", "種別": "", "単価": 0, "出力品名": "送料",
             "マージン率": 0, "加算額": 0},
        ],
    },
    "未来": {
        "略号": "MR",
        "header": {
            "取引先名称": "",
            "件名": "物流業務委託費",
            "取引先郵便番号": "",
            "取引先都道府県": "",
            "取引先住所1": "",
            "取引先住所2": "",
            "取引先敬称": "",
            "備考": "振込手数料は御社負担でお願い致します。",
            "振込先": "楽天銀行 第二営業支店（252）普通預金 7484378 カ）パピ－",
            "自社担当者氏名": "",
        },
        "保管料マスタ": [
            {"種別名": "パレット", "単価": 1000, "出力品名": "保管料：パレット"},
        ],
    },
}

CLIENTS_FILE = "clients.json"


def _ensure_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def load_clients():
    """クライアント設定を読み込む。無ければ初期値を作成して返す。"""
    _ensure_dir()
    path = os.path.join(DATA_DIR, CLIENTS_FILE)
    if not os.path.exists(path):
        save_clients(DEFAULT_CLIENTS)
        return json.loads(json.dumps(DEFAULT_CLIENTS))  # ディープコピー
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_clients(clients):
    """クライアント設定をローカルJSONへ保存する。"""
    _ensure_dir()
    path = os.path.join(DATA_DIR, CLIENTS_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(clients, f, ensure_ascii=False, indent=2)
    return path


# --- Google Drive バックアップ（任意） ---

def _get_drive_service():
    """既存ページと同じリフレッシュトークン方式でDriveサービスを得る。"""
    import streamlit as st
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        token=None,
        refresh_token=st.secrets["GOOGLE_REFRESH_TOKEN"],
        client_id=st.secrets["GOOGLE_CLIENT_ID"],
        client_secret=st.secrets["GOOGLE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
    )
    return build("drive", "v3", credentials=creds)


def backup_to_drive(file_bytes, filename, folder_id, mimetype="text/csv"):
    """
    指定bytesをDriveの folder_id 配下へアップロードする。
    folder_id は請求書専用フォルダを想定（他機能のフォルダとは分ける）。
    成功時はファイルIDを返す。
    """
    import io
    from googleapiclient.http import MediaIoBaseUpload

    service = _get_drive_service()
    metadata = {"name": filename, "parents": [folder_id]}
    media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mimetype, resumable=False)
    created = service.files().create(body=metadata, media_body=media, fields="id").execute()
    return created.get("id")
