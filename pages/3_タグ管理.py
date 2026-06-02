import streamlit as st
from notion_client import Client

NOTION_API_KEY = "".join(c for c in st.secrets["NOTION_API_KEY"] if c.isprintable() and ord(c) < 128)
PAGE_ID = "37384fb235d780b88a46eb8d619a19ad"

def get_database_id():
    client = Client(auth=NOTION_API_KEY)
    children = client.blocks.children.list(block_id=PAGE_ID)
    for block in children["results"]:
        if block["type"] == "child_database":
            return block["id"]
    return PAGE_ID

DATABASE_ID = get_database_id()

def get_tags():
    client = Client(auth=NOTION_API_KEY)
    db = client.databases.retrieve(database_id=DATABASE_ID)
    options = db["properties"].get("タグ", {}).get("multi_select", {}).get("options", [])
    return [o["name"] for o in options]

def add_tag(new_tag):
    client = Client(auth=NOTION_API_KEY)
    db = client.databases.retrieve(database_id=DATABASE_ID)
    current = db["properties"]["タグ"]["multi_select"]["options"]
    if any(o["name"] == new_tag for o in current):
        return False  # 重複
    current.append({"name": new_tag})
    client.databases.update(
        database_id=DATABASE_ID,
        properties={"タグ": {"multi_select": {"options": current}}}
    )
    return True

def delete_tag(tag_name):
    client = Client(auth=NOTION_API_KEY)
    db = client.databases.retrieve(database_id=DATABASE_ID)
    current = db["properties"]["タグ"]["multi_select"]["options"]
    new_options = [o for o in current if o["name"] != tag_name]
    client.databases.update(
        database_id=DATABASE_ID,
        properties={"タグ": {"multi_select": {"options": new_options}}}
    )

st.set_page_config(page_title="タグ管理", layout="centered")
st.title("🏷️ タグ管理")

tags = get_tags()

st.subheader("現在のタグ一覧")
if not tags:
    st.info("タグがまだありません")
else:
    for tag in tags:
        col1, col2 = st.columns([4, 1])
        with col1:
            st.write(f"• {tag}")
        with col2:
            if st.button("削除", key=f"del_{tag}"):
                delete_tag(tag)
                st.success(f"「{tag}」を削除しました")
                st.rerun()

st.divider()
st.subheader("タグを追加")
with st.form("add_tag_form"):
    new_tag = st.text_input("新しいタグ名", placeholder="例：デザイン")
    submitted = st.form_submit_button("追加する")

if submitted:
    if not new_tag.strip():
        st.error("タグ名を入力してください")
    else:
        if add_tag(new_tag.strip()):
            st.success(f"「{new_tag}」を追加しました")
            st.rerun()
        else:
            st.warning(f"「{new_tag}」は既に存在します")
