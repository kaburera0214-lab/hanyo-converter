# -*- coding: utf-8 -*-
"""
Google Drive上の共有マスタ（③NE商品マスタ等）の保存・読込（請求書発行機能専用）。

③商品マスタはNE共通・10万件規模で毎回アップは非効率なため、Driveに1ファイルとして
保存し再利用する。Streamlit Cloudはローカルが揮発するため永続化先としてDriveを使う。
Driveの認証は既存ページと同じリフレッシュトークン方式（store経由）。
"""
import io
from . import store

# Drive上の③商品マスタの固定ファイル名（フォルダはINVOICE_GDRIVE_FOLDER_ID）
PRODUCT_MASTER_NAME = "ne_product_master.csv"


def _service():
    return store._get_drive_service()


def find_file(name, folder_id):
    """フォルダ内の指定名ファイルを探し、{id,name,modifiedTime}を返す（無ければNone）。"""
    service = _service()
    q = (f"name = '{name}' and '{folder_id}' in parents and trashed = false")
    res = service.files().list(
        q=q, fields="files(id, name, modifiedTime)", pageSize=1).execute()
    files = res.get("files", [])
    return files[0] if files else None


def download_bytes(file_id):
    """ファイル内容をbytesで取得する。"""
    from googleapiclient.http import MediaIoBaseDownload
    service = _service()
    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def get_or_create_folder(name, parent_id):
    """parent_id配下に name のフォルダを取得（無ければ作成）し、IDを返す。"""
    service = _service()
    q = (f"name = '{name}' and '{parent_id}' in parents and "
         "mimeType = 'application/vnd.google-apps.folder' and trashed = false")
    res = service.files().list(q=q, fields="files(id)", pageSize=1).execute()
    files = res.get("files", [])
    if files:
        return files[0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id]}
    created = service.files().create(body=meta, fields="id").execute()
    return created["id"]


def upload_bytes(file_bytes, name, folder_id, mimetype="application/octet-stream"):
    """指定フォルダへbytesを新規アップロード（同名でも別ファイルとして作成）。"""
    import io
    from googleapiclient.http import MediaIoBaseUpload
    service = _service()
    media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mimetype, resumable=False)
    created = service.files().create(
        body={"name": name, "parents": [folder_id]},
        media_body=media, fields="id").execute()
    return created["id"]


def _next_version(folder_id, prefix, today):
    """{prefix}_{today}_NNN.csv の次の版数を返す（汎用マスタ変換と同じ規則）。"""
    service = _service()
    q = (f"'{folder_id}' in parents and name contains '{prefix}_{today}_' "
         "and trashed = false")
    res = service.files().list(q=q, fields="files(name)", pageSize=100).execute()
    vers = []
    for f in res.get("files", []):
        try:
            vers.append(int(f["name"].replace(".csv", "").split("_")[-1]))
        except (ValueError, IndexError):
            pass
    return max(vers) + 1 if vers else 1


def upload_versioned(file_bytes, prefix, folder_id, mimetype="text/csv"):
    """{prefix}_{YYYYMMDD}_{NNN}.csv の版数付き名でフォルダに保存し、ファイル名を返す。"""
    import datetime
    today = datetime.datetime.now().strftime("%Y%m%d")
    v = _next_version(folder_id, prefix, today)
    name = f"{prefix}_{today}_{v:03d}.csv"
    upload_bytes(file_bytes, name, folder_id, mimetype)
    return name


def find_latest(folder_id, prefix):
    """フォルダ内の {prefix}_* で最新（名前降順=日付/版数が最大）のファイルを返す。"""
    service = _service()
    q = (f"'{folder_id}' in parents and name contains '{prefix}_' and trashed = false")
    res = service.files().list(
        q=q, fields="files(id, name, modifiedTime)", pageSize=1000).execute()
    files = [f for f in res.get("files", []) if f["name"].startswith(prefix + "_")]
    if not files:
        return None
    files.sort(key=lambda f: f["name"], reverse=True)
    return files[0]


def upload_or_replace(file_bytes, name, folder_id, mimetype="text/csv"):
    """
    同名ファイルがあれば中身を更新、無ければ新規作成する。ファイルIDを返す。
    """
    from googleapiclient.http import MediaIoBaseUpload
    service = _service()
    media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mimetype, resumable=False)
    existing = find_file(name, folder_id)
    if existing:
        service.files().update(fileId=existing["id"], media_body=media).execute()
        return existing["id"]
    metadata = {"name": name, "parents": [folder_id]}
    created = service.files().create(
        body=metadata, media_body=media, fields="id").execute()
    return created["id"]
