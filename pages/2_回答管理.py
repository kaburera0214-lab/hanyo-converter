import streamlit as st
from notion_client import Client
from datetime import datetime

st.set_page_config(page_title="回答管理", layout="wide")
st.title("✅ 回答・管理（パピー用）")

NOTION_API_KEY = "".join(c for c in st.secrets["NOTION_API_KEY"] if c.isprintable() and ord(c) < 128)
PAGE_ID = "37384fb235d780b88a46eb8d619a19ad"  # ページID（固定）
ANTHROPIC_API_KEY = st.secrets.get("ANTHROPIC_API_KEY", "")

def get_database_id():
    client = Client(auth=NOTION_API_KEY)
    children = client.blocks.children.list(block_id=PAGE_ID)
    for block in children["results"]:
        if block["type"] == "child_database":
            return block["id"]
    return PAGE_ID

DATABASE_ID = get_database_id()

# 画像URLプロパティが未存在なら自動追加
def ensure_properties():
    try:
        client = Client(auth=NOTION_API_KEY)
        db = client.databases.retrieve(database_id=DATABASE_ID)
        if "画像URL" not in db["properties"]:
            client.databases.update(
                database_id=DATABASE_ID,
                properties={"画像URL": {"rich_text": {}}}
            )
    except Exception:
        pass

ensure_properties()

REASON_CATEGORIES = ["スピード優先", "品質優先", "コスト優先", "顧客対応", "社内ルール", "その他"]

def get_text(prop):
    items = prop.get("rich_text", [])
    return items[0]["plain_text"] if items else ""

def get_questions():
    client = Client(auth=NOTION_API_KEY)
    res = client.databases.query(**{
        "database_id": DATABASE_ID,
        "sorts": [{"property": "質問日時", "direction": "descending"}]
    })
    questions = []
    for page in res["results"]:
        p = page["properties"]
        questions.append({
            "id": page["id"],
            "タイトル": p["質問タイトル"]["title"][0]["plain_text"] if p.get("質問タイトル", {}).get("title") else "",
            "質問本文": get_text(p["質問本文"]),
            "ステータス": p["ステータス"]["select"]["name"] if p["ステータス"]["select"] else "未回答",
            "回答本文": get_text(p["回答本文"]),
            "AI生成ドラフト": get_text(p["AI生成ドラフト"]),
            "判断理由カテゴリ": [s["name"] for s in p["判断理由カテゴリ"]["multi_select"]],
            "判断理由詳細": get_text(p["判断理由詳細"]),
            "タグ": [s["name"] for s in p["タグ"]["multi_select"]],
            "質問日時": p["質問日時"]["date"]["start"] if p["質問日時"]["date"] else "",
            "画像URL": get_text(p.get("画像URL", {})),
        })
    return questions

def generate_draft(question, questions):
    if not ANTHROPIC_API_KEY:
        return "（Claude APIキーが未設定のためドラフト生成できません）"
    import anthropic
    knowledge = [q for q in questions if q["ステータス"] == "回答済"]
    knowledge_text = "\n\n".join([
        f"【事例】\n質問: {q['質問本文']}\n回答: {q['回答本文']}\n判断理由: {', '.join(q['判断理由カテゴリ'])} / {q['判断理由詳細']}"
        for q in knowledge
    ]) or "（まだ蓄積データがありません）"
    ac = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = ac.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=f"あなたはパピー社のナレッジアシスタントです。過去の判断事例をもとに回答ドラフトを作成してください。\n\n【過去の判断事例】\n{knowledge_text}",
        messages=[{"role": "user", "content": f"質問: {question['質問本文']}"}]
    )
    return message.content[0].text

if st.button("🔄 最新の質問を読み込む"):
    st.rerun()

questions = get_questions()

if not questions:
    st.info("質問がまだありません")
else:
    STATUS_EMOJI = {"未回答": "🔴", "ドラフト生成済": "🔵", "回答済": "🟢"}
    for q in questions:
        emoji = STATUS_EMOJI.get(q["ステータス"], "⚪")
        label = f"{emoji} {q['タイトル']}　（{q['ステータス']}）　{q['質問日時'][:10] if q['質問日時'] else ''}"
        with st.expander(label):
            st.markdown(f"**質問内容：** {q['質問本文']}")
            if q["画像URL"]:
                st.markdown(f"**画像：** [Google Driveで開く]({q['画像URL']})")
            if q["タグ"]:
                st.markdown(f"**タグ：** {', '.join(q['タグ'])}")
            st.divider()

            if q["ステータス"] == "未回答":
                if st.button("AIドラフトを生成する", key=f"draft_{q['id']}"):
                    with st.spinner("AIが回答を考えています..."):
                        draft = generate_draft(q, questions)
                        c = Client(auth=NOTION_API_KEY)
                        c.pages.update(
                            page_id=q["id"],
                            properties={
                                "AI生成ドラフト": {"rich_text": [{"text": {"content": draft}}]},
                                "ステータス": {"select": {"name": "ドラフト生成済"}},
                            }
                        )
                        st.success("ドラフトを生成しました。")
                        st.rerun()

            if q["ステータス"] in ("ドラフト生成済", "回答済"):
                回答本文 = st.text_area(
                    "回答内容（編集可）",
                    value=q["AI生成ドラフト"] if not q["回答本文"] else q["回答本文"],
                    height=150,
                    key=f"answer_{q['id']}",
                    disabled=q["ステータス"] == "回答済"
                )

                if q["ステータス"] == "ドラフト生成済":
                    st.warning("⚠️ 判断理由を入力しないと送信できません")
                    選択カテゴリ = st.multiselect("判断理由カテゴリ", REASON_CATEGORIES, key=f"cat_{q['id']}")
                    理由詳細 = st.text_area("判断理由の詳細", height=80, key=f"reason_{q['id']}")

                    if st.button("✓ インハナさんに送信する", key=f"approve_{q['id']}", type="primary"):
                        if not 選択カテゴリ and not 理由詳細.strip():
                            st.error("判断理由を入力してください")
                        elif not 回答本文.strip():
                            st.error("回答内容を入力してください")
                        else:
                            c = Client(auth=NOTION_API_KEY)
                            c.pages.update(
                                page_id=q["id"],
                                properties={
                                    "回答本文": {"rich_text": [{"text": {"content": 回答本文}}]},
                                    "判断理由カテゴリ": {"multi_select": [{"name": c2} for c2 in 選択カテゴリ]},
                                    "判断理由詳細": {"rich_text": [{"text": {"content": 理由詳細}}]},
                                    "ステータス": {"select": {"name": "回答済"}},
                                    "回答日時": {"date": {"start": datetime.now().isoformat()}},
                                    "AI学習済": {"checkbox": True},
                                }
                            )
                            st.success("送信しました！")
                            st.rerun()

                elif q["ステータス"] == "回答済":
                    st.success("回答済み")
                    if q["判断理由カテゴリ"]:
                        st.markdown(f"**判断理由：** {', '.join(q['判断理由カテゴリ'])}")
                    if q["判断理由詳細"]:
                        st.markdown(f"**詳細：** {q['判断理由詳細']}")
