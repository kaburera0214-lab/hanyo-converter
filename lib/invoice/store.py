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
        # 保管料種別マスタ：種別名 -> {単価, 出力品名}（エリアは別項目で管理）
        "保管料マスタ": [
            {"種別名": "保管料：パレット", "単価": 1000, "出力品名": "保管料：パレット"},
            {"種別名": "保管料：中量棚", "単価": 300, "出力品名": "保管料：中量棚"},
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
            {"費目": "その他", "種別": "[汎用]作業料", "単価": 2000,
             "出力品名": "[汎用]作業料"},
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

# 配送種別（出荷作業料・資材費の軸）。ネコポス＝nekop、宅配便はサイズ。
DELIVERY_TYPES = ["nekop", "60", "80", "100", "120", "140", "160"]

# 保管カウントのエリア（プルダウン）。ロケーションは自由記入。
STORAGE_AREAS = ["ライオンズ", "スワローズ", "イーグルス", "ポメ", "シュナ", "トイプー", "その他"]

# 送料表の地域（列）。ヤマト運輸の地域区分に準拠。
SHIPPING_AREAS = [
    "北海道", "北東北", "南東北", "関東", "信越", "北陸", "東海",
    "関西", "中国", "四国", "北九州", "南九州", "沖縄",
]

# 都道府県 -> 地域（送料表の地域を引くための変換マスタ初期値）
DEFAULT_AREA_MAP = {
    "北海道": "北海道",
    "青森県": "北東北", "岩手県": "北東北", "秋田県": "北東北",
    "宮城県": "南東北", "山形県": "南東北", "福島県": "南東北",
    "茨城県": "関東", "栃木県": "関東", "群馬県": "関東", "埼玉県": "関東",
    "千葉県": "関東", "東京都": "関東", "神奈川県": "関東", "山梨県": "関東",
    "新潟県": "信越", "長野県": "信越",
    "富山県": "北陸", "石川県": "北陸", "福井県": "北陸",
    "岐阜県": "東海", "静岡県": "東海", "愛知県": "東海", "三重県": "東海",
    "滋賀県": "関西", "京都府": "関西", "大阪府": "関西", "兵庫県": "関西",
    "奈良県": "関西", "和歌山県": "関西",
    "鳥取県": "中国", "島根県": "中国", "岡山県": "中国", "広島県": "中国", "山口県": "中国",
    "徳島県": "四国", "香川県": "四国", "愛媛県": "四国", "高知県": "四国",
    "福岡県": "北九州", "佐賀県": "北九州", "長崎県": "北九州",
    "大分県": "北九州", "熊本県": "北九州",
    "宮崎県": "南九州", "鹿児島県": "南九州",
    "沖縄県": "沖縄",
}

# 送料表の初期値（Team-EC：ヤマト運輸 宅配便60〜160＋ネコポス）。
# 各行は {配送業者, 配送区分, サイズ, 地域名:運賃...} 形式。
def _shipping_row(carrier, kubun, size, values):
    row = {"配送業者": carrier, "配送区分": kubun, "サイズ": size}
    row.update(dict(zip(SHIPPING_AREAS, values)))
    return row

DEFAULT_SHIPPING_TABLE = [
    _shipping_row("ヤマト運輸", "宅配便", "60",
                  [930, 820, 710, 500, 500, 710, 710, 680, 770, 770, 930, 930, 1460]),
    _shipping_row("ヤマト運輸", "宅配便", "80",
                  [1100, 880, 770, 540, 550, 720, 770, 880, 950, 950, 1100, 1100, 1820]),
    _shipping_row("ヤマト運輸", "宅配便", "100",
                  [1240, 1060, 950, 800, 800, 910, 850, 1020, 1130, 1130, 1280, 1280, 2170]),
    _shipping_row("ヤマト運輸", "宅配便", "120",
                  [1380, 1170, 1100, 880, 940, 1080, 990, 1100, 1310, 1310, 1430, 1430, 2500]),
    _shipping_row("ヤマト運輸", "宅配便", "140",
                  [1540, 1310, 1210, 880, 1100, 1210, 1100, 1270, 1380, 1430, 1540, 1540, 2860]),
    _shipping_row("ヤマト運輸", "宅配便", "160",
                  [1600, 1430, 1380, 1160, 1210, 1320, 1270, 1380, 1430, 1490, 1600, 1600, 3190]),
    _shipping_row("ネコポス", "メール便", "3cm以内",
                  [220, 220, 220, 220, 220, 220, 220, 220, 220, 220, 220, 220, 220]),
]


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

# Drive HTTP1リクエストあたりのタイムアウト秒。httplib2は既定でタイムアウト無し＝
# Drive呼び出しが無限ハングし得る（Yahoo/NEトークンのDrive読み書きが固まりStreamlitが
# 打切られる原因）。健全な呼び出しは数秒で終わるため、上限を設けてハングを例外化する。
DRIVE_HTTP_TIMEOUT = 30


def _get_drive_service():
    """既存ページと同じリフレッシュトークン方式でDriveサービスを得る。
    HTTPトランスポートにタイムアウトを設定し、Drive呼び出しの無限ハングを防ぐ。"""
    import streamlit as st
    import google_auth_httplib2
    import httplib2
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        token=None,
        refresh_token=st.secrets["GOOGLE_REFRESH_TOKEN"],
        client_id=st.secrets["GOOGLE_CLIENT_ID"],
        client_secret=st.secrets["GOOGLE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
    )
    http = google_auth_httplib2.AuthorizedHttp(
        creds, http=httplib2.Http(timeout=DRIVE_HTTP_TIMEOUT))
    # static_discovery=True（既定）でディスカバリはバンドル済みを使い追加の通信をしない。
    return build("drive", "v3", http=http)


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
