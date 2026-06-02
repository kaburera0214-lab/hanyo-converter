import streamlit as st
from notion_client import Client
from datetime import datetime
import uuid
from PIL import Image
import io
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

NOTION_API_KEY = "".join(c for c in st.secrets["NOTION_API_KEY"] if c.isprintable() and ord(c) < 128)
PAGE_ID = "37384fb235d780b88a46eb8d619a19ad"  # ページID（固定）
GDRIVE_FOLDER_ID = "1z7yCYxDGO3lVVKrBmG8mL1apH6Pfl4Xu"

TAGS = ["デザイン", "納期", "仕様変更", "費用", "その他"]
MAX_FILE_SIZE = 4.5 * 1024 * 1024  # 4.5MB

def get_database_id():
    client = Client(auth=NOTION_API_KEY)
    children = client.blocks.children.list(block_id=PAGE_ID)
    for block in children["results"]:
        if block["type"] == "child_database":
            return block["id"]
    return PAGE_ID

DATABASE_ID = get_database_id()

def compress_image(file_bytes):
    img = Image.open(io.BytesIO(file_bytes))
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    quality = 85
    while quality >= 30:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        if buf.tell() <= MAX_FILE_SIZE:
            return buf.getvalue()
        quality -= 10
    w, h = img.size
    while True:
        w, h = int(w * 0.8), int(h * 0.8)
        img_resized = img.resize((w, h), Image.LANCZOS)
        buf = io.BytesIO()
        img_resized.save(buf, format="JPEG", quality=70, optimize=True)
        if buf.tell() <= MAX_FILE_SIZE:
            return buf.getvalue()

def get_drive_service():
    creds = Credentials(
        token=None,
        refresh_token=st.secrets["GOOGLE_REFRESH_TOKEN"],
        client_id=st.secrets["GOOGLE_CLIENT_ID"],
        client_secret=st.secrets["GOOGLE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token"
    )
    return build("drive", "v3", credentials=creds)

def upload_to_drive(file_bytes, filename):
    service = get_drive_service()
    file_metadata = {"name": filename, "parents": [GDRIVE_FOLDER_ID]}
    media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype="image/jpeg")
    file = service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id"
    ).execute()
    file_id = file.get("id")
    # 誰でも閲覧可能に
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
        with st.spinner("送信中..."):
            # 画像をGoogle Driveにアップロード
            画像URLs = []
            if 画像ファイル:
                today = datetime.now().strftime("%Y%m%d")
                for f in 画像ファイル:
                    compressed = compress_image(f.read())
                    unique_id = str(uuid.uuid4())[:8]
                    filename = f"{today}_{unique_id}.jpg"
                    url = upload_to_drive(compressed, filename)
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

        msg = "質問を送信しました！"
        if 画像URLs:
            msg += f"（画像 {len(画像URLs)}枚アップロード済）"
        st.success(msg)
