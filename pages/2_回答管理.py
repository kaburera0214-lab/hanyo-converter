import streamlit as st
from notion_client import Client
from datetime import datetime
from streamlit_autorefresh import st_autorefresh

st.set_page_config(page_title="回答管理", layout="wide")
st.title("✅ 回答・管理（パピー用）")

st_autorefresh(interval=60000, key="auto_refresh")

NOTION_API_KEY = "".join(c for c in st.secrets["NOTION_API_KEY"] if c.isprintable() and ord(c) < 128)
PAGE_ID = "37384fb235d780b88a46eb8d619a19ad"
ANTHROPIC_API_KEY = st.secrets.get("ANTHROPIC_API_KEY", "")

def get_database_id():
    client = Client(auth=NOTION_API_KEY)
    children = client.blocks.children.list(block_id=PAGE_ID)
    for block in children["results"]:
        if block["type"] == "child_database":
            return block["id"]
    return PAGE_ID

DATABASE_ID = get_database_id()

def ensure_properties():
    try:
        client = Client(auth=NOTION_API_KEY)
        db = client.databases.retrieve(database_id=DATABASE_ID)
        updates = {}
        for prop in ["画像URL", "編集履歴", "追加質問", "会話ログ"]:
            if prop not in db["properties"]:
                updates[prop] = {"rich_text": {}}
        if updates:
            client.databases.update(database_id=DATABASE_ID, properties=updates)
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
            "番号": p.get("ID", {}).get("unique_id", {}).get("number"),
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
            "編集履歴": get_text(p.get("編集履歴", {})),
            "追加質問": get_text(p.get("追加質問", {})),
            "会話ログ": get_text(p.get("会話ログ", {})),
        })
    return questions

def get_current_status(page_id):
    try:
        client = Client(auth=NOTION_API_KEY)
        page = client.pages.retrieve(page_id=page_id)
        return page["properties"]["ステータス"]["select"]["name"]
    except Exception:
        return None

def append_edit_history(existing_history, editor, new_content=None):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    new_entry = f"[{timestamp}] {editor}"
    lines = existing_history.strip().split("\n") if existing_history.strip() else []
    lines.append(new_entry)
    lines = lines[-30:]
    result = "\n".join(lines)
    while len(result) > 1900 and len(lines) > 1:
        lines = lines[1:]
        result = "\n".join(lines)
    return result

def generate_draft(question, questions):
    if not ANTHROPIC_API_KEY:
        return "（Anthropic APIキーが未設定のためドラフト生成できません）"
    knowledge = [q for q in questions if q["ステータス"] == "回答済"]
    knowledge_text = "\n\n".join([
        f"【事例】\n質問: {q['質問本文']}\n回答: {q['回答本文']}\n判断理由: {', '.join(q['判断理由カテゴリ'])} / {q['判断理由詳細']}"
        for q in knowledge
    ]) or "（まだ蓄積データがありません）"
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        message = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1024,
            messages=[{"role": "user", "content": f"""あなたはパピー社のナレッジアシスタントです。過去の判断事例をもとに回答ドラフトを作成してください。

【過去の判断事例】
{knowledge_text}

質問: {question['質問本文']}

上記の質問に対する回答ドラフトを作成してください。"""}]
        )
        return message.content[0].text
    except Exception as e:
        return f"（AIドラフト生成に失敗しました。APIキーを確認してください。エラー: {type(e).__name__}）"

# EDITOR_1_NAME / EDITOR_1_PASSWORD 〜 EDITOR_10_NAME / EDITOR_10_PASSWORD で定義
PASSWORDS = {}
for i in range(1, 11):
    name = st.secrets.get(f"EDITOR_{i}_NAME", "")
    pw = st.secrets.get(f"EDITOR_{i}_PASSWORD", "")
    if name and pw:
        PASSWORDS[pw] = name

if st.button("🔄 最新の質問を読み込む"):
    st.rerun()

questions = get_questions()

# ── 検索フォーム ──────────────────────────────────────────────────────
all_tags = sorted({t for q in questions for t in q["タグ"]})

with st.form("search_form"):
    st.markdown("**🔍 絞り込み検索**")
    col1, col2 = st.columns(2)
    with col1:
        f_keyword = st.text_input("キーワード（質問内容・回答本文）", placeholder="例：送料　キャンセル")
        f_number = st.text_input("質問番号", placeholder="例：42")
    with col2:
        f_tags = st.multiselect("タグ", all_tags)
        f_date = st.date_input("期間", value=[], help="開始日・終了日の2つを選択（1つだけでもOK）")
    search_submitted = st.form_submit_button("🔍 検索する", type="primary")
    clear_submitted = st.form_submit_button("クリア")

if clear_submitted:
    st.rerun()

# フィルタ適用（検索ボタン押下時のみ）
filtered = questions
is_filtered = False
if search_submitted:
    is_filtered = True
    if f_keyword:
        kw = f_keyword.lower()
        filtered = [q for q in filtered
                    if kw in q["タイトル"].lower()
                    or kw in q["質問本文"].lower()
                    or kw in q["回答本文"].lower()
                    or kw in q["判断理由詳細"].lower()]
    if f_number:
        try:
            num = int(f_number)
            filtered = [q for q in filtered if q["番号"] == num]
        except ValueError:
            pass
    if f_tags:
        filtered = [q for q in filtered if any(t in q["タグ"] for t in f_tags)]
    if isinstance(f_date, (list, tuple)) and len(f_date) >= 1:
        from datetime import date
        date_from = f_date[0] if len(f_date) >= 1 else None
        date_to = f_date[1] if len(f_date) >= 2 else None
        def in_range(q):
            d_str = q["質問日時"][:10] if q["質問日時"] else ""
            if not d_str:
                return False
            try:
                d = date.fromisoformat(d_str)
                if date_from and d < date_from:
                    return False
                if date_to and d > date_to:
                    return False
                return True
            except Exception:
                return False
        filtered = [q for q in filtered if in_range(q)]

display_questions = filtered if is_filtered else questions

if is_filtered:
    st.caption(f"検索結果：{len(filtered)}件 / 全{len(questions)}件")

if not display_questions:
    st.info("該当する質問がありません" if is_filtered else "質問がまだありません")
else:
    STATUS_EMOJI = {"未回答": "🔴", "ドラフト生成済": "🔵", "回答済": "🟢", "編集中": "🟡", "再質問": "🟠"}
    for q in display_questions:
        is_editing = q["ステータス"] == "編集中"
        emoji = STATUS_EMOJI.get(q["ステータス"], "⚪")
        label = f"{emoji} {q['タイトル']}　（{q['ステータス']}）　{q['質問日時'][:10] if q['質問日時'] else ''}"
        with st.expander(label):
            st.markdown(f"**質問内容：** {q['質問本文']}")
            if q["画像URL"]:
                st.markdown("**画像：**")
                urls = q["画像URL"].split("\n")
                cols = st.columns(min(len(urls), 3))
                for i, url in enumerate(urls):
                    if url.strip():
                        if "drive.google.com/file/d/" in url:
                            file_id = url.split("/file/d/")[1].split("/")[0]
                            img_url = f"https://drive.google.com/thumbnail?id={file_id}&sz=w400"
                        else:
                            img_url = url
                        with cols[i % 3]:
                            st.image(img_url, use_container_width=True)
                            st.caption(f"[拡大表示]({url})")
            if q["タグ"]:
                st.markdown(f"**タグ：** {', '.join(q['タグ'])}")
            st.divider()

            if is_editing:
                st.warning("現在インハナさんが編集中です。編集完了後に対応してください。")

            elif q["ステータス"] == "再質問":
                # 会話ログ表示
                if q["会話ログ"]:
                    st.markdown("**会話の流れ：**")
                    conv_height = max(200, q["会話ログ"].count("\n") * 22 + 100)
                    log_html = q["会話ログ"].replace("\n", "<br>")
                    st.markdown(f'<div style="background:#fff;border:1px solid #dee2e6;border-radius:6px;padding:12px;line-height:1.7;font-size:0.9em;">{log_html}</div>', unsafe_allow_html=True)
                st.error(f"**追加質問：** {q['追加質問']}")
                st.divider()

                # 追加質問への回答（高さは固定150px）
                add_answer = st.text_area("追加質問への回答", value="", height=150, key=f"add_ans_{q['id']}")
                選択カテゴリ = st.multiselect("判断理由カテゴリ", REASON_CATEGORIES, key=f"add_cat_{q['id']}")
                理由詳細 = st.text_area("判断理由の詳細", height=80, key=f"add_reason_{q['id']}")

                if st.button("✓ 追加回答を送信する", key=f"add_approve_{q['id']}", type="primary"):
                    if not add_answer.strip():
                        st.error("回答内容を入力してください")
                    else:
                        # 会話ログに追記
                        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
                        new_log = q["会話ログ"] + f"\n\n【追加A｜{timestamp}】{add_answer.strip()}"
                        if len(new_log) > 1900:
                            new_log = "（古い会話を省略）\n" + new_log[-1800:]
                        c = Client(auth=NOTION_API_KEY)
                        c.pages.update(
                            page_id=q["id"],
                            properties={
                                "回答本文": {"rich_text": [{"text": {"content": add_answer}}]},
                                "追加質問": {"rich_text": [{"text": {"content": ""}}]},
                                "会話ログ": {"rich_text": [{"text": {"content": new_log}}]},
                                "判断理由カテゴリ": {"multi_select": [{"name": c2} for c2 in 選択カテゴリ]},
                                "判断理由詳細": {"rich_text": [{"text": {"content": 理由詳細}}]},
                                "ステータス": {"select": {"name": "回答済"}},
                                "回答日時": {"date": {"start": datetime.now().isoformat()}},
                                "AI学習済": {"checkbox": True},
                            }
                        )
                        st.success("追加回答を送信しました！")
                        st.rerun()

            elif q["ステータス"] == "未回答":
                if st.button("AIドラフトを生成する", key=f"draft_{q['id']}"):
                    current = get_current_status(q["id"])
                    if current == "編集中":
                        st.warning("現在インハナさんが編集中です。編集完了後に対応してください。")
                        st.rerun()
                    else:
                        with st.spinner("AIが回答を考えています..."):
                            draft = generate_draft(q, questions)
                            if draft.startswith("（AIドラフト生成に失敗") or draft.startswith("（Anthropic") or draft.startswith("（Gemini"):
                                st.error(draft)
                            else:
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

            if q["ステータス"] == "ドラフト生成済":
                answer_value = q["AI生成ドラフト"] if not q["回答本文"] else q["回答本文"]
                answer_height = max(150, answer_value.count("\n") * 22 + 100)
                回答本文 = st.text_area("回答内容（編集可）", value=answer_value, height=answer_height, key=f"answer_{q['id']}")
                st.warning("⚠️ 判断理由を入力しないと送信できません")
                選択カテゴリ = st.multiselect("判断理由カテゴリ", REASON_CATEGORIES, key=f"cat_{q['id']}")
                理由詳細 = st.text_area("判断理由の詳細", height=80, key=f"reason_{q['id']}")

                if st.button("✅ 回答を送信する", key=f"approve_{q['id']}", type="primary"):
                    current = get_current_status(q["id"])
                    if current == "編集中":
                        st.warning("現在インハナさんが編集中です。編集完了後に対応してください。")
                        st.rerun()
                    elif not 選択カテゴリ and not 理由詳細.strip():
                        st.error("判断理由を入力してください")
                    elif not 回答本文.strip():
                        st.error("回答内容を入力してください")
                    else:
                        new_history = append_edit_history(q["編集履歴"], "パピー")
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
                                "編集履歴": {"rich_text": [{"text": {"content": new_history}}]},
                            }
                        )
                        st.success("送信しました！")
                        st.rerun()

            elif q["ステータス"] == "回答済":
                answer_text = q["回答本文"]
                answer_height = max(150, answer_text.count("\n") * 22 + 100)

                # 回答修正モード判定
                edit_key = f"editing_answer_{q['id']}"
                is_answer_editing = st.session_state.get(edit_key, False)

                if not is_answer_editing:
                    # 会話ログがあれば全会話を表示、なければ通常の回答表示
                    if q["会話ログ"]:
                        st.markdown("**会話の流れ：**")
                        log_html = q["会話ログ"].replace("\n", "<br>")
                        st.markdown(f'<div style="background:#fff;border:1px solid #dee2e6;border-radius:6px;padding:12px;line-height:1.7;font-size:0.9em;">{log_html}</div>', unsafe_allow_html=True)
                    else:
                        st.markdown("**回答内容：**")
                        st.text_area("", value=answer_text, height=answer_height, key=f"view_{q['id']}", label_visibility="collapsed")
                    st.success("回答済み")
                    if q["判断理由カテゴリ"]:
                        st.markdown(f"**判断理由：** {', '.join(q['判断理由カテゴリ'])}")
                    if q["判断理由詳細"]:
                        st.markdown(f"**詳細：** {q['判断理由詳細']}")
                    col_btn1, col_btn2 = st.columns(2)
                    with col_btn1:
                        if st.button("✏️ 回答を修正する", key=f"start_edit_ans_{q['id']}"):
                            st.session_state[edit_key] = "auth"
                            st.rerun()
                    with col_btn2:
                        if st.button("💬 追加質問する", key=f"start_followup_{q['id']}"):
                            st.session_state[edit_key] = "followup"
                            st.rerun()
                    if q["編集履歴"]:
                        with st.expander("📋 編集履歴"):
                            st.text(q["編集履歴"])

                elif is_answer_editing == "followup":
                    # 回答管理からの追加質問入力
                    st.markdown("**追加質問を入力してください：**")
                    follow_up = st.text_area("追加質問内容", height=120, key=f"mgmt_followup_{q['id']}")
                    col1, col2 = st.columns(2)
                    with col1:
                        if st.button("📨 送信する", key=f"send_mgmt_followup_{q['id']}", type="primary"):
                            if not follow_up.strip():
                                st.error("追加質問の内容を入力してください")
                            else:
                                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
                                existing_log = q["会話ログ"]
                                if not existing_log:
                                    existing_log = f"【Q】{q['質問本文']}\n【A】{answer_text}"
                                new_log = existing_log + f"\n\n【追加Q｜{timestamp}】{follow_up.strip()}"
                                if len(new_log) > 1900:
                                    new_log = "（古い会話を省略）\n" + new_log[-1800:]
                                Client(auth=NOTION_API_KEY).pages.update(
                                    page_id=q["id"],
                                    properties={
                                        "追加質問": {"rich_text": [{"text": {"content": follow_up.strip()}}]},
                                        "会話ログ": {"rich_text": [{"text": {"content": new_log}}]},
                                        "ステータス": {"select": {"name": "再質問"}},
                                    }
                                )
                                st.session_state[edit_key] = False
                                st.success("追加質問を送信しました！")
                                st.rerun()
                    with col2:
                        if st.button("キャンセル", key=f"cancel_followup_{q['id']}"):
                            st.session_state[edit_key] = False
                            st.rerun()

                elif is_answer_editing == "auth":
                    # パスワード認証ステップ
                    st.markdown("**回答を修正するにはパスワードを入力してください**")
                    pw = st.text_input("パスワード", type="password", key=f"pw_{q['id']}")
                    col1, col2 = st.columns(2)
                    with col1:
                        if st.button("認証して修正する", key=f"auth_ans_{q['id']}", type="primary"):
                            if pw and pw in PASSWORDS:
                                st.session_state[edit_key] = "editing"
                                st.session_state[f"editor_{q['id']}"] = PASSWORDS[pw]
                                st.rerun()
                            else:
                                st.error("パスワードが正しくありません")
                    with col2:
                        if st.button("キャンセル", key=f"cancel_auth_{q['id']}"):
                            st.session_state[edit_key] = False
                            st.rerun()

                elif is_answer_editing == "editing":
                    # 修正モード（認証済み）
                    editor_name = st.session_state.get(f"editor_{q['id']}", "不明")
                    st.markdown(f"**回答内容を修正中（{editor_name}）：**")
                    new_answer = st.text_area("回答内容", value=answer_text, height=answer_height, key=f"edit_ans_{q['id']}")
                    col1, col2 = st.columns(2)
                    with col1:
                        if st.button("💾 修正を保存する", key=f"save_ans_{q['id']}", type="primary"):
                            if not new_answer.strip():
                                st.error("回答内容を入力してください")
                            else:
                                new_history = append_edit_history(q["編集履歴"], editor_name)
                                c = Client(auth=NOTION_API_KEY)
                                c.pages.update(
                                    page_id=q["id"],
                                    properties={
                                        "回答本文": {"rich_text": [{"text": {"content": new_answer}}]},
                                        "編集履歴": {"rich_text": [{"text": {"content": new_history}}]},
                                    }
                                )
                                st.session_state[edit_key] = False
                                st.session_state.pop(f"editor_{q['id']}", None)
                                st.success("修正を保存しました。")
                                st.rerun()
                    with col2:
                        if st.button("キャンセル", key=f"cancel_ans_{q['id']}"):
                            st.session_state[edit_key] = False
                            st.session_state.pop(f"editor_{q['id']}", None)
                            st.rerun()
                    if q["編集履歴"]:
                        with st.expander("📋 編集履歴"):
                            st.text(q["編集履歴"])
