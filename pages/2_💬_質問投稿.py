import streamlit as st
from notion_client import Client
from datetime import datetime, timezone, timedelta
from PIL import Image
import io
import re

JST = timezone(timedelta(hours=9))

def now_jst():
    return datetime.now(JST)
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

NOTION_API_KEY = "".join(c for c in st.secrets["NOTION_API_KEY"] if c.isprintable() and ord(c) < 128)
PAGE_ID = "37384fb235d780b88a46eb8d619a19ad"
GDRIVE_FOLDER_ID = "1z7yCYxDGO3lVVKrBmG8mL1apH6Pfl4Xu"
ANTHROPIC_API_KEY = st.secrets.get("ANTHROPIC_API_KEY", "")
MAX_FILE_SIZE = 4.5 * 1024 * 1024

def get_database_id():
    client = Client(auth=NOTION_API_KEY)
    children = client.blocks.children.list(block_id=PAGE_ID)
    for block in children["results"]:
        if block["type"] == "child_database":
            return block["id"]
    return PAGE_ID

DATABASE_ID = get_database_id()

def ensure_status_option():
    try:
        client = Client(auth=NOTION_API_KEY)
        db = client.databases.retrieve(database_id=DATABASE_ID)
        options = db["properties"].get("ステータス", {}).get("select", {}).get("options", [])
        names = [o["name"] for o in options]
        new_options = list(options)
        if "編集中" not in names:
            new_options.append({"name": "編集中", "color": "yellow"})
        if "再質問" not in names:
            new_options.append({"name": "再質問", "color": "orange"})
        if new_options != options:
            client.databases.update(
                database_id=DATABASE_ID,
                properties={"ステータス": {"select": {"options": new_options}}}
            )
        # 追加質問・会話ログプロパティを追加
        updates = {}
        if "追加質問" not in db["properties"]:
            updates["追加質問"] = {"rich_text": {}}
        if "会話ログ" not in db["properties"]:
            updates["会話ログ"] = {"rich_text": {}}
        if updates:
            client.databases.update(database_id=DATABASE_ID, properties=updates)
    except Exception:
        pass

ensure_status_option()

def get_tags():
    client = Client(auth=NOTION_API_KEY)
    db = client.databases.retrieve(database_id=DATABASE_ID)
    options = db["properties"].get("タグ", {}).get("multi_select", {}).get("options", [])
    return [o["name"] for o in options]

TAGS = get_tags()

def get_text(prop):
    items = prop.get("rich_text", [])
    return items[0]["plain_text"] if items else ""

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
    service.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"}
    ).execute()
    return f"https://drive.google.com/file/d/{file_id}/view"

def check_tag_consistency(title, content, tags):
    if not ANTHROPIC_API_KEY:
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        message = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=100,
            messages=[{"role": "user", "content": f"""質問タイトル：{title}
質問内容：{content}
選択されたタグ：{', '.join(tags)}

質問内容とタグが整合しているか判断してください。
整合している場合は「OK」とだけ答えてください。
整合していない場合は「NG: （理由を30文字以内で）」の形式で答えてください。"""}]
        )
        result = message.content[0].text.strip()
        if result.startswith("NG"):
            return f"タグと質問内容が合っていない可能性があります。{result[3:].strip()}"
        return None
    except Exception:
        return None

def validate_question(title, content, tags, has_images):
    errors = []

    # タイトル粒度チェック（大カテゴリ＋中カテゴリ）
    if len(title.strip()) < 10:
        errors.append("質問タイトルが短すぎます。「大カテゴリ＋中カテゴリ」の粒度で記入してください（例：「CS・保険証券の発行タイミングについて」）")

    # 複数質問の「■」チェック
    question_matches = re.findall(r'[^\n。]*[？?]', content)
    if len(question_matches) >= 2 and "■" not in content:
        errors.append("複数の質問がある場合は、各質問の先頭に「■」をつけて質問を明確に分けてください")

    # 画像依存チェック
    if has_images and len(content.strip()) < 50:
        errors.append("画像に頼りすぎず、質問内容だけで何を聞いているか分かるよう記載してください（目安50文字以上）")

    # タグ整合性チェック（AI）
    tag_error = check_tag_consistency(title, content, tags)
    if tag_error:
        errors.append(tag_error)

    return errors

def search_similar_questions(title, content):
    """過去の類似質問をNotionで検索し、上位件数を返す"""
    import anthropic as ac, json, re
    client = Client(auth=NOTION_API_KEY)

    # Notion検索API（タイトル+本文の先頭100文字をキーワードに）
    search_text = f"{title} {content[:100]}"
    res = client.search(query=search_text, filter={"property": "object", "value": "page"}, page_size=20)

    candidates = []
    for page in res.get("results", []):
        p = page.get("properties", {})
        if not p:
            continue
        status = p.get("ステータス", {}).get("select", {})
        if not status or status.get("name") != "回答済":
            continue
        title_prop = p.get("質問タイトル", {}).get("title", [])
        q_title = title_prop[0]["plain_text"] if title_prop else ""
        q_body_items = p.get("質問本文", {}).get("rich_text", [])
        q_body = q_body_items[0]["plain_text"] if q_body_items else ""
        a_items = p.get("回答本文", {}).get("rich_text", [])
        a_body = a_items[0]["plain_text"] if a_items else ""
        tags = [s["name"] for s in p.get("タグ", {}).get("multi_select", [])]
        if q_title or q_body:
            candidates.append({
                "id": page["id"],
                "タイトル": q_title,
                "質問本文": q_body,
                "回答本文": a_body,
                "タグ": tags,
            })

    if not candidates:
        return []

    # Claudeで類似度を判定（上位3件を選別）
    try:
        summary = "\n".join([
            f"[{i}] タイトル:{c['タイトル']} | 質問:{c['質問本文'][:100]}"
            for i, c in enumerate(candidates)
        ])
        ai = ac.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = ai.messages.create(
            model="claude-haiku-4-5",
            max_tokens=200,
            messages=[{"role": "user", "content": f"""新規質問：「{title}」{content[:150]}

以下の過去質問のうち、新規質問と類似または参考になるものの番号を最大3つ選んでください。
類似するものがなければ空配列を返してください。
JSON配列のみで返答してください（例：[0,2]）：

{summary}"""}]
        )
        raw = msg.content[0].text.strip()
        raw = re.sub(r"```.*?```", "", raw, flags=re.DOTALL).strip()
        indices = json.loads(raw)
        return [candidates[i] for i in indices if i < len(candidates)]
    except Exception:
        return candidates[:3]

def rewrite_question(title, content):
    """Claude APIで質問をリライトし、タイトルと本文を返す"""
    import anthropic, json, re
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=2048,
        messages=[{"role": "user", "content": f"""あなたは日本語ビジネス文章の校正アシスタントです。
以下の質問を、日本語として自然で丁寧なビジネス文章にリライトしてください。

ルール：
- 意味・情報は一切変えない
- 文法・表現・敬語を整える
- 複数の質問がある場合は各質問の先頭に「■」をつける
- タイトルは「大カテゴリ・中カテゴリ」の形式で整える

質問タイトル：{title}
質問内容：{content}

必ず以下のJSON形式のみで返してください。前後に説明文や```は不要です：
{{"title": "リライト後のタイトル", "content": "リライト後の内容"}}"""}]
    )
    raw = message.content[0].text.strip()
    # コードブロックを除去
    raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("```").strip()
    result = json.loads(raw)
    return result.get("title", title), result.get("content", content)

def get_editable_questions():
    client = Client(auth=NOTION_API_KEY)
    res = client.databases.query(**{
        "database_id": DATABASE_ID,
        "filter": {"or": [
            {"property": "ステータス", "select": {"equals": "未回答"}},
            {"property": "ステータス", "select": {"equals": "編集中"}},
        ]},
        "sorts": [{"property": "質問日時", "direction": "descending"}]
    })
    questions = []
    for page in res["results"]:
        p = page["properties"]
        questions.append({
            "id": page["id"],
            "タイトル": p["質問タイトル"]["title"][0]["plain_text"] if p.get("質問タイトル", {}).get("title") else "",
            "質問本文": get_text(p.get("質問本文", {})),
            "タグ": [s["name"] for s in p.get("タグ", {}).get("multi_select", [])],
            "ステータス": p["ステータス"]["select"]["name"] if p["ステータス"]["select"] else "未回答",
            "質問日時": p["質問日時"]["date"]["start"] if p.get("質問日時", {}).get("date") else "",
            "編集履歴": get_text(p.get("編集履歴", {})),
        })
    return questions

def get_answered_questions():
    """追加質問できる回答済み質問を取得"""
    client = Client(auth=NOTION_API_KEY)
    res = client.databases.query(**{
        "database_id": DATABASE_ID,
        "filter": {"or": [
            {"property": "ステータス", "select": {"equals": "回答済"}},
            {"property": "ステータス", "select": {"equals": "再質問"}},
        ]},
        "sorts": [{"property": "質問日時", "direction": "descending"}]
    })
    questions = []
    for page in res["results"]:
        p = page["properties"]
        questions.append({
            "id": page["id"],
            "タイトル": p["質問タイトル"]["title"][0]["plain_text"] if p.get("質問タイトル", {}).get("title") else "",
            "質問本文": get_text(p.get("質問本文", {})),
            "回答本文": get_text(p.get("回答本文", {})),
            "追加質問": get_text(p.get("追加質問", {})),
            "会話ログ": get_text(p.get("会話ログ", {})),
            "タグ": [s["name"] for s in p.get("タグ", {}).get("multi_select", [])],
            "ステータス": p["ステータス"]["select"]["name"] if p["ステータス"]["select"] else "回答済",
            "質問日時": p["質問日時"]["date"]["start"] if p.get("質問日時", {}).get("date") else "",
        })
    return questions

# ── 新規投稿フォーム ─────────────────────────────────────────────────
st.set_page_config(page_title="質問を送る", layout="centered")

from lib.qa.history import append_history
from lib.qa.thread_ui import inject_qa_styles, render_text_block, text_to_html

st.title("📝 質問を送る（インハナさん用）")
inject_qa_styles()

st.info("""**記入ルール**
- タイトルは「大カテゴリ＋中カテゴリ」の粒度で（例：CS・保険証券の発行タイミングについて）
- 複数の質問がある場合は各質問の先頭に「■」をつけること
- 画像だけで説明を省かず、文章だけで内容が伝わるように記載すること""")

def submit_question(タイトル, 質問本文, タグ, 画像ファイル):
    """Notionへ質問を保存し、画像をDriveにアップロードする"""
    client = Client(auth=NOTION_API_KEY)
    history = append_history("", "質問投稿")
    props = {
        "質問タイトル": {"title": [{"text": {"content": タイトル}}]},
        "質問本文": {"rich_text": [{"text": {"content": 質問本文}}]},
        "ステータス": {"select": {"name": "未回答"}},
        "質問者": {"select": {"name": "インハナ"}},
        "質問日時": {"date": {"start": now_jst().isoformat()}},
        "タグ": {"multi_select": [{"name": t} for t in タグ]},
        "編集履歴": {"rich_text": [{"text": {"content": history}}]},
    }
    page = client.pages.create(**{"parent": {"database_id": DATABASE_ID}, "properties": props})
    page_id = page["id"]
    unique_num = client.pages.retrieve(page_id=page_id)["properties"].get("ID", {}).get("unique_id", {}).get("number", 0)

    画像URLs = []
    if 画像ファイル:
        today = datetime.now().strftime("%Y%m%d")
        for i, f in enumerate(画像ファイル, start=1):
            compressed = compress_image(f.read())
            url = upload_to_drive(compressed, f"{today}_{unique_num:04d}_{i:02d}.jpg")
            画像URLs.append(url)
        client.pages.update(
            page_id=page_id,
            properties={"画像URL": {"rich_text": [{"text": {"content": "\n".join(画像URLs)}}]}}
        )
    return len(画像URLs)

# ── ステップ管理 ──────────────────────────────────────────────────────
step = st.session_state.get("post_step", "input")  # input / similar / preview

# ── ステップ1：入力フォーム ──────────────────────────────────────────
if step == "input":
    with st.form("question_form"):
        タイトル = st.text_input("質問タイトル *", placeholder="例：CS・保険証券の発行タイミングについて")
        質問本文 = st.text_area("質問内容 *", height=150, placeholder="■ 〇〇について確認したいのですが...\n■ また、〇〇の場合はどうなりますか？")
        画像ファイル = st.file_uploader("画像（複数可）", type=["png", "jpg", "jpeg", "gif", "webp"], accept_multiple_files=True)
        タグ = st.multiselect("タグ（必須）*", TAGS)
        submitted = st.form_submit_button("類似質問を確認する →")

    if submitted:
        if not タイトル or not 質問本文:
            st.error("タイトルと質問内容は必須です")
        elif not タグ:
            st.error("タグを選択してください")
        else:
            with st.spinner("入力内容を確認中..."):
                errors = validate_question(タイトル, 質問本文, タグ, bool(画像ファイル))

            if errors:
                for err in errors:
                    st.error(err)
            else:
                with st.spinner("過去の類似質問を検索中..."):
                    similar = search_similar_questions(タイトル, 質問本文)

                st.session_state["post_step"] = "similar"
                st.session_state["orig_title"] = タイトル
                st.session_state["orig_content"] = 質問本文
                st.session_state["post_tags"] = タグ
                st.session_state["post_images"] = 画像ファイル
                st.session_state["similar_results"] = similar
                st.rerun()

# ── ステップ1.5：類似質問プレビュー ──────────────────────────────────
elif step == "similar":
    similar = st.session_state.get("similar_results", [])
    タイトル = st.session_state.get("orig_title", "")
    質問本文 = st.session_state.get("orig_content", "")

    if similar:
        st.subheader(f"🔍 {len(similar)}件の類似質問が見つかりました")
        st.caption("以下の回答で解決する場合は投稿不要です。解決しない場合は「新規質問として投稿する」へ進んでください。")

        for i, q in enumerate(similar):
            with st.expander(f"**{q['タイトル'] or '（タイトルなし）'}**　タグ：{'・'.join(q['タグ'])}", expanded=False):
                body = q["質問本文"][:400] + ("…" if len(q["質問本文"]) > 400 else "")
                render_text_block(body, label="質問内容")
                if q["回答本文"]:
                    answer = q["回答本文"][:400] + ("…" if len(q["回答本文"]) > 400 else "")
                    render_text_block(answer, label="回答")
                else:
                    st.caption("（回答なし）")
    else:
        st.subheader("✅ 類似質問は見つかりませんでした")
        st.caption("新規質問として投稿してください。")

    st.divider()
    col1, col2 = st.columns(2)
    with col1:
        if st.button("← 入力に戻る"):
            st.session_state["post_step"] = "input"
            st.rerun()
    with col2:
        btn_label = "この内容では解決しない → 新規質問として投稿する" if similar else "新規質問として投稿する →"
        if st.button(btn_label, type="primary"):
            with st.spinner("AIがリライトしています..."):
                rewritten_title, rewritten_content = rewrite_question(タイトル, 質問本文)
            st.session_state["post_step"] = "preview"
            st.session_state["rewrite_title"] = rewritten_title
            st.session_state["rewrite_content"] = rewritten_content
            st.rerun()

# ── ステップ2：リライトプレビュー＆確認 ────────────────────────────
elif step == "preview":
    st.subheader("📋 リライトプレビュー")
    st.caption("左：入力原文　右：AIリライト（編集可）")

    # 余白を最小化するCSS
    st.markdown("""<style>
    [data-testid="column"] { padding-left: 0.25rem !important; padding-right: 0.25rem !important; }
    </style>""", unsafe_allow_html=True)

    # 内容の行数に応じて高さを動的に計算
    orig_content = st.session_state["orig_content"]
    rewrite_content = st.session_state["rewrite_content"]
    content_height = max(300, max(orig_content.count("\n"), rewrite_content.count("\n")) * 22 + 120)

    col_orig, col_rewrite = st.columns(2)
    with col_orig:
        st.markdown("**原文**")
        st.text_input("タイトル（原文）", value=st.session_state["orig_title"], key="orig_t", label_visibility="visible")
        st.markdown(
            f'<div class="qa-panel" style="min-height:{content_height}px;">{text_to_html(orig_content)}</div>',
            unsafe_allow_html=True,
        )

    with col_rewrite:
        st.markdown("**AIリライト（必要なら編集してください）**")
        final_title = st.text_input("タイトル", value=st.session_state["rewrite_title"], key="final_t")
        final_content = st.text_area("内容", value=rewrite_content, height=content_height, key="final_c")

    st.divider()
    col1, col2 = st.columns(2)
    with col1:
        if st.button("← 入力に戻る"):
            st.session_state["post_step"] = "input"
            st.rerun()
    with col2:
        if st.button("✅ この内容で送信する", type="primary"):
            with st.spinner("送信中..."):
                img_count = submit_question(
                    final_title, final_content,
                    st.session_state["post_tags"],
                    st.session_state.get("post_images")
                )
            msg = "質問を送信しました！"
            if img_count:
                msg += f"（画像 {img_count}枚アップロード済）"
            st.success(msg)
            # セッションをリセット
            for k in ["post_step", "orig_title", "orig_content", "rewrite_title", "rewrite_content", "post_tags", "post_images"]:
                st.session_state.pop(k, None)
            st.rerun()

# ── 投稿済み質問の編集 ────────────────────────────────────────────────
st.divider()
st.subheader("✏️ 投稿済み質問を編集する")

edit_questions = get_editable_questions()
if not edit_questions:
    st.info("編集できる質問（未回答）はありません")
else:
    editing_id = st.session_state.get("editing_id")

    for q in edit_questions:
        is_editing = (editing_id == q["id"])
        status_label = "🟡 編集中" if q["ステータス"] == "編集中" else ""
        label = f"{q['タイトル']}　{q['質問日時'][:10] if q['質問日時'] else ''}　{status_label}"

        with st.expander(label, expanded=is_editing):
            if not is_editing:
                render_text_block(q["質問本文"], label="質問内容")
                st.caption(f"タグ：{'・'.join(q['タグ'])}")
                if st.button("✏️ 編集する", key=f"start_edit_{q['id']}"):
                    st.session_state["editing_id"] = q["id"]
                    try:
                        Client(auth=NOTION_API_KEY).pages.update(
                            page_id=q["id"],
                            properties={"ステータス": {"select": {"name": "編集中"}}}
                        )
                    except Exception as e:
                        st.error(f"ステータス更新失敗: {e}")
                        st.stop()
                    st.rerun()
            else:
                with st.form(f"edit_form_{q['id']}"):
                    new_title = st.text_input("質問タイトル", value=q["タイトル"])
                    new_content = st.text_area("質問内容", value=q["質問本文"], height=150)
                    new_tags = st.multiselect("タグ", TAGS, default=q["タグ"])
                    col1, col2 = st.columns(2)
                    with col1:
                        save = st.form_submit_button("保存する", type="primary")
                    with col2:
                        cancel = st.form_submit_button("キャンセル")

                if cancel:
                    st.session_state.pop("editing_id", None)
                    Client(auth=NOTION_API_KEY).pages.update(
                        page_id=q["id"],
                        properties={"ステータス": {"select": {"name": "未回答"}}}
                    )
                    st.rerun()

                if save:
                    with st.spinner("入力内容を確認中..."):
                        errors = validate_question(new_title, new_content, new_tags, False)

                    if errors:
                        for err in errors:
                            st.error(err)
                        # バリデーションエラーでも保存操作なのでロックは維持。解除は上のボタンで。
                    else:
                        try:
                            new_hist = append_history(q.get("編集履歴", ""), "質問編集")
                            Client(auth=NOTION_API_KEY).pages.update(
                                page_id=q["id"],
                                properties={
                                    "質問タイトル": {"title": [{"text": {"content": new_title}}]},
                                    "質問本文": {"rich_text": [{"text": {"content": new_content}}]},
                                    "タグ": {"multi_select": [{"name": t} for t in new_tags]},
                                    "ステータス": {"select": {"name": "未回答"}},
                                    "編集履歴": {"rich_text": [{"text": {"content": new_hist}}]},
                                }
                            )
                            st.session_state.pop("editing_id", None)
                            st.success("更新しました。")
                            st.rerun()
                        except Exception as e:
                            st.error(f"保存に失敗しました: {e}")

