import streamlit as st
from notion_client import Client
from datetime import datetime
import uuid
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from google.oauth2 import service_account
import json
import io

NOTION_API_KEY = "".join(c for c in st.secrets["NOTION_API_KEY"] if c.isprintable() and ord(c) < 128)
DATABASE_ID = st.secrets["NOTION_DATABASE_ID"]
GDRIVE_FOLDER_ID = "1z7yCYxDGO3lVVKrBmG8mL1apH6Pfl4Xu"

TAGS = ["デザイン", "納期", "仕様変更", "費用", "その他"]

def upload_to_drive(file_bytes, filename, mimetype):
    creds_dict = json.loads(st.secrets["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    service = build("drive", "v3", credentials=creds)
    file_metadata = {
        "name": filename,
        "parents": [GDRIVE_FOLDER_ID]
    }
    media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mimetype)
    file = service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id",
        supportsAllDrives=True
    ).execute()
    file_id = file.get("id")
    # 閲覧リンクを公開
    service.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"}
    ).execute()
    return f"https://drive.google.com/file/d/{file_id}/view"

st.set_page_config(page_title="質問を送る", layout="centered")
st.title("📝 質問を送る（インハナさん用）")

with st.form("question_form"):
    タイトル = st.text_input("質問タイトル *", placeholder="例：ヘッダーの色変更について")
    質問本文 = st.text_area("質問内容 *", height=150, placeholder="詳しい内容を記入してください")
    画像ファイル = st.file_uploader("画像（複数可）", type=["png", "jpg", "jpeg", "gif", "webp"], accept_multiple_files=True)
    タグ = st.multiselect("タグ（任意）", TAGS)
    submitted = st.form_submit_button("質問を送信する")

if submitted:
    if not タイトル or not 質問本文:
        st.error("タイトルと質問内容は必須です")
    else:
        # 画像をGoogle Driveにアップロード
        画像URLs = []
        if 画像ファイル:
            with st.spinner("画像をアップロード中..."):
                today = datetime.now().strftime("%Y%m%d")
                for f in 画像ファイル:
                    unique_id = str(uuid.uuid4())[:8]
                    ext = f.name.split(".")[-1]
                    filename = f"{today}_{unique_id}.{ext}"
                    url = upload_to_drive(f.read(), filename, f.type)
                    画像URLs.append(url)

        # Notionに保存
        client = Client(auth=NOTION_API_KEY)
        props = {
            "質問タイトル": {"title": [{"text": {"content": タイトル}}]},
            "質問本文": {"rich_text": [{"text": {"content": 質問本文}}]},
            "ステータス": {"select": {"name": "未回答"}},
            "質問者": {"select": {"name": "インハナ"}},
            "質問日時": {"date": {"start": datetime.now().isoformat()}},
            "タグ": {"multi_select": [{"name": t} for t in タグ]},
        }
        if 画像URLs:
            props["画像URL"] = {"rich_text": [{"text": {"content": "\n".join(画像URLs)}}]}

        client.pages.create(**{
            "parent": {"database_id": DATABASE_ID},
            "properties": props
        })
        st.success(f"質問を送信しました！{'（画像 ' + str(len(画像URLs)) + '枚アップロード済）' if 画像URLs else ''}")
