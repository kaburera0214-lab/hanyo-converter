# -*- coding: utf-8 -*-
import streamlit as st
from notion_client import Client
from datetime import datetime, timedelta
import anthropic

st.set_page_config(page_title="パターン分析", layout="wide")
st.title("📊 パターン分析・改善提案")

NOTION_API_KEY = "".join(c for c in st.secrets["NOTION_API_KEY"] if c.isprintable() and ord(c) < 128)
ANTHROPIC_API_KEY = st.secrets.get("ANTHROPIC_API_KEY", "")
PAGE_ID = "37384fb235d780b88a46eb8d619a19ad"

def get_database_id():
    client = Client(auth=NOTION_API_KEY)
    children = client.blocks.children.list(block_id=PAGE_ID)
    for block in children["results"]:
        if block["type"] == "child_database":
            return block["id"]
    return PAGE_ID

DATABASE_ID = get_database_id()

def get_text(prop):
    items = prop.get("rich_text", [])
    return items[0]["plain_text"] if items else ""

def fetch_answered_questions(days: int = None):
    """回答済み質問を取得（days=Noneで全件）"""
    client = Client(auth=NOTION_API_KEY)
    filters = [{"property": "ステータス", "select": {"equals": "回答済"}}]
    if days:
        since = (datetime.now() - timedelta(days=days)).isoformat()
        filters.append({"property": "質問日時", "date": {"on_or_after": since}})

    query_filter = {"and": filters} if len(filters) > 1 else filters[0]

    questions = []
    cursor = None
    while True:
        kwargs = {
            "database_id": DATABASE_ID,
            "filter": query_filter,
            "sorts": [{"property": "質問日時", "direction": "descending"}],
            "page_size": 100,
        }
        if cursor:
            kwargs["start_cursor"] = cursor
        res = client.databases.query(**kwargs)
        for page in res["results"]:
            p = page["properties"]
            questions.append({
                "タイトル": p["質問タイトル"]["title"][0]["plain_text"] if p.get("質問タイトル", {}).get("title") else "",
                "質問本文": get_text(p["質問本文"]),
                "回答本文": get_text(p["回答本文"]),
                "タグ": [s["name"] for s in p["タグ"]["multi_select"]],
                "判断理由カテゴリ": [s["name"] for s in p["判断理由カテゴリ"]["multi_select"]],
                "判断理由詳細": get_text(p["判断理由詳細"]),
                "質問日時": p["質問日時"]["date"]["start"] if p.get("質問日時", {}).get("date") else "",
            })
        if not res.get("has_more"):
            break
        cursor = res["next_cursor"]
    return questions

def generate_report(questions: list, period_label: str) -> str:
    """Claudeでパターン分析レポートを生成"""
    if not ANTHROPIC_API_KEY:
        return "（Anthropic APIキーが未設定です）"

    # サマリーデータを作成（トークン節約のため本文は先頭200文字）
    summary_lines = []
    for i, q in enumerate(questions):
        tags = "・".join(q["タグ"]) if q["タグ"] else "未分類"
        reasons = "・".join(q["判断理由カテゴリ"]) if q["判断理由カテゴリ"] else ""
        line = f"[{i+1}] タグ:{tags} | タイトル:{q['タイトル']} | 判断理由:{reasons}"
        if q["判断理由詳細"]:
            line += f" | 詳細:{q['判断理由詳細'][:80]}"
        summary_lines.append(line)

    summary = "\n".join(summary_lines)

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""あなたはパピー社の業務改善アドバイザーです。
以下は{period_label}の社内Q&A {len(questions)}件のサマリーです。

{summary}

上記を分析して、以下の構成で日本語のレポートを作成してください：

## 1. 全体サマリー
件数・タグ別内訳・期間の特徴を2〜3文で

## 2. 頻出テーマ TOP3
最も多く質問されたテーマを3つ挙げ、それぞれ件数と具体例を記載

## 3. 判断理由の傾向
どんな基準で判断されることが多いか、傾向と背景を分析

## 4. 根本原因の仮説
なぜこれらの質問が発生しているか、プロセス・ルール・情報共有の観点から考察

## 5. 構造改善の提案（優先度順）
具体的なアクションを3〜5個、実行難易度（低/中/高）とセットで提示

## 6. 次の分析までに確認すべき点
改善効果を測るための指標や確認事項"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text

# ── UI ──────────────────────────────────────────────────────────────
st.markdown("蓄積されたQ&Aデータをまとめて分析し、業務改善の提案を生成します。")

col1, col2 = st.columns([2, 1])
with col1:
    period = st.selectbox(
        "分析対象期間",
        ["直近30日", "直近90日", "直近180日", "全期間"],
        index=3
    )
with col2:
    st.markdown("<br>", unsafe_allow_html=True)
    run_btn = st.button("🔍 分析レポートを生成する", type="primary", use_container_width=True)

period_map = {"直近30日": 30, "直近90日": 90, "直近180日": 180, "全期間": None}
days = period_map[period]

# データ件数を先に表示
with st.spinner("データ取得中..."):
    questions = fetch_answered_questions(days)

col_a, col_b, col_c, col_d = st.columns(4)
tag_counts = {}
for q in questions:
    for t in q["タグ"]:
        tag_counts[t] = tag_counts.get(t, 0) + 1

col_a.metric("対象件数", f"{len(questions)}件")
col_b.metric("最多タグ", max(tag_counts, key=tag_counts.get) if tag_counts else "-",
             str(max(tag_counts.values())) + "件" if tag_counts else "")
reason_counts = {}
for q in questions:
    for r in q["判断理由カテゴリ"]:
        reason_counts[r] = reason_counts.get(r, 0) + 1
col_c.metric("最多判断理由", max(reason_counts, key=reason_counts.get) if reason_counts else "-")
col_d.metric("分析期間", period)

# タグ内訳
if tag_counts:
    st.markdown("**タグ別内訳：**　" + "　".join([f"{k} ({v}件)" for k, v in sorted(tag_counts.items(), key=lambda x: -x[1])]))

st.divider()

if run_btn:
    if len(questions) == 0:
        st.warning("対象期間に回答済みの質問がありません。")
    elif len(questions) < 5:
        st.warning(f"データが{len(questions)}件と少ないため、分析精度が低い可能性があります。")

    with st.spinner(f"Claudeが{len(questions)}件を分析中...（30〜60秒かかります）"):
        report = generate_report(questions, period)

    st.markdown(report)

    # ダウンロードボタン
    now = datetime.now().strftime("%Y%m%d_%H%M")
    st.download_button(
        "📥 レポートをテキストでダウンロード",
        data=report.encode("utf-8"),
        file_name=f"改善レポート_{period}_{now}.txt",
        mime="text/plain"
    )
