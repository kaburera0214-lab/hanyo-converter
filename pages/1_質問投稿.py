import streamlit as st
from notion_client import Client
from datetime import datetime

NOTION_API_KEY = st.secrets["NOTION_API_KEY"].replace("​", "").replace("\n", "").strip()
DATABASE_ID = st.secrets["NOTION_DATABASE_ID"]

TAGS = ["デザイン", "納期", "仕様変更", "費用", "その他"]

st.set_page_config(page_title="質問を送る", layout="centered")
st.title("📝 質問を送る（インハナさん用）")

with st.form("question_form"):
    タイトル = st.text_input("質問タイトル *", placeholder="例：ヘッダーの色変更について")
    質問本文 = st.text_area("質問内容 *", height=150, placeholder="詳しい内容を記入してください")
    タグ = st.multiselect("タグ（任意）", TAGS)
    submitted = st.form_submit_button("質問を送信する")

if submitted:
    if not タイトル or not 質問本文:
        st.error("タイトルと質問内容は必須です")
    else:
        client = Client(auth=NOTION_API_KEY)
        client.pages.create(**{
            "parent": {"database_id": DATABASE_ID},
            "properties": {
                "質問タイトル": {"title": [{"text": {"content": タイトル}}]},
                "質問本文": {"rich_text": [{"text": {"content": 質問本文}}]},
                "ステータス": {"select": {"name": "未回答"}},
                "質問者": {"select": {"name": "インハナ"}},
                "質問日時": {"date": {"start": datetime.now().isoformat()}},
                "タグ": {"multi_select": [{"name": t} for t in タグ]},
            }
        })
        st.success("質問を送信しました。回答をお待ちください。")
