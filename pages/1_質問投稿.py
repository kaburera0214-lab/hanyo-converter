import streamlit as st
from notion_client import Client
from datetime import datetime
import uuid
from PIL import Image
import io

NOTION_API_KEY = "".join(c for c in st.secrets["NOTION_API_KEY"] if c.isprintable() and ord(c) < 128)
DATABASE_ID = st.secrets["NOTION_DATABASE_ID"]

TAGS = ["デザイン", "納期", "仕様変更", "費用", "その他"]
MAX_FILE_SIZE = 4.5 * 1024 * 1024  # 4.5MB（余裕を持って5MB未満に）

def compress_image(file_bytes, mimetype):
    """5MB未満になるまで画質を下げて圧縮する"""
    img = Image.open(io.BytesIO(file_bytes))

    # EXIFを無視してRGBに変換（PNG等対応）
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")

    quality = 85
    while quality >= 30:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        if buf.tell() <= MAX_FILE_SIZE:
            return buf.getvalue(), "image/jpeg"
        quality -= 10

    # それでも大きければリサイズ
    w, h = img.size
    while True:
        w, h = int(w * 0.8), int(h * 0.8)
        img_resized = img.resize((w, h), Image.LANCZOS)
        buf = io.BytesIO()
        img_resized.save(buf, format="JPEG", quality=70, optimize=True)
        if buf.tell() <= MAX_FILE_SIZE:
            return buf.getvalue(), "image/jpeg"

def upload_to_notion_page(page_id, file_bytes, filename):
    """NotionページにファイルをAPIでアップロード"""
    import requests
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": "2022-06-28",
    }
    # ファイルアップロードAPI
    upload_url = "https://api.notion.com/v1/file_uploads"
    res = requests.post(upload_url, headers=headers, json={"filename": filename})
    if res.status_code != 200:
        return None
    upload_data = res.json()
    upload_id = upload_data.get("id")
    upload_endpoint = upload_data.get("upload_url")

    # ファイル本体を送信
    requests.put(
        upload_endpoint,
        headers={"Authorization": f"Bearer {NOTION_API_KEY}"},
        files={"file": (filename, io.BytesIO(file_bytes), "image/jpeg")}
    )
    return upload_id

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
            # Notionにページを作成
            client = Client(auth=NOTION_API_KEY)
            props = {
                "質問タイトル": {"title": [{"text": {"content": タイトル}}]},
                "質問本文": {"rich_text": [{"text": {"content": 質問本文}}]},
                "ステータス": {"select": {"name": "未回答"}},
                "質問者": {"select": {"name": "インハナ"}},
                "質問日時": {"date": {"start": datetime.now().isoformat()}},
                "タグ": {"multi_select": [{"name": t} for t in タグ]},
            }
            page = client.pages.create(**{
                "parent": {"database_id": DATABASE_ID},
                "properties": props
            })
            page_id = page["id"]

            # 画像をページ本文にアップロード
            if 画像ファイル:
                import requests
                today = datetime.now().strftime("%Y%m%d")
                children = []
                for f in 画像ファイル:
                    file_bytes = f.read()
                    # 圧縮
                    compressed, _ = compress_image(file_bytes, f.type)
                    unique_id = str(uuid.uuid4())[:8]
                    filename = f"{today}_{unique_id}.jpg"

                    # Notionファイルアップロード
                    headers = {
                        "Authorization": f"Bearer {NOTION_API_KEY}",
                        "Notion-Version": "2022-06-28",
                        "Content-Type": "application/json"
                    }
                    res = requests.post(
                        "https://api.notion.com/v1/file_uploads",
                        headers=headers,
                        json={"filename": filename}
                    )
                    if res.status_code == 200:
                        upload_data = res.json()
                        upload_id = upload_data.get("id")
                        upload_url = upload_data.get("upload_url")

                        requests.put(
                            upload_url,
                            headers={"Authorization": f"Bearer {NOTION_API_KEY}"},
                            files={"file": (filename, io.BytesIO(compressed), "image/jpeg")}
                        )

                        children.append({
                            "object": "block",
                            "type": "image",
                            "image": {
                                "type": "file_upload",
                                "file_upload": {"id": upload_id}
                            }
                        })

                if children:
                    client.blocks.children.append(
                        block_id=page_id,
                        children=children
                    )

        st.success(f"質問を送信しました！{'（画像 ' + str(len(画像ファイル)) + '枚）' if 画像ファイル else ''}")
