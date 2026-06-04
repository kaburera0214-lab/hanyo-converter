# -*- coding: utf-8 -*-
"""
スプレッドシートから Notion へ質問データをインポートするスクリプト

使い方:
  python import_questions.py

事前に .streamlit/secrets.toml に以下が設定されていること:
  NOTION_API_KEY = "..."
  ANTHROPIC_API_KEY = "..."
"""

import pandas as pd
import time
import json
import re
import tomllib
from pathlib import Path
from datetime import datetime
from notion_client import Client
import anthropic

# ── 設定 ────────────────────────────────────────────────────────────
EXCEL_PATH = r"C:/Users/info/Downloads/【インハナオーナー】質問表 (1) - コピー.xlsx"
SECRETS_PATH = Path(__file__).parent / ".streamlit" / "secrets.toml"
PAGE_ID = "37384fb235d780b88a46eb8d619a19ad"
BATCH_SIZE = 20        # タイトル生成の1バッチ件数
NOTION_INTERVAL = 0.4  # Notion API間隔（秒）

# ── APIキー読み込み（引数 > secrets.toml の順で優先）────────────────
import sys
args = sys.argv[1:]
notion_key_arg = next((args[i+1] for i, a in enumerate(args) if a == "--notion-key"), None)
anthropic_key_arg = next((args[i+1] for i, a in enumerate(args) if a == "--anthropic-key"), None)

if notion_key_arg and anthropic_key_arg:
    NOTION_API_KEY = notion_key_arg.strip()
    ANTHROPIC_API_KEY = anthropic_key_arg.strip()
else:
    with open(SECRETS_PATH, "rb") as f:
        secrets = tomllib.load(f)
    NOTION_API_KEY = "".join(c for c in secrets.get("NOTION_API_KEY", "") if c.isprintable() and ord(c) < 128)
    ANTHROPIC_API_KEY = secrets.get("ANTHROPIC_API_KEY", "")
    if not NOTION_API_KEY or not ANTHROPIC_API_KEY:
        print("使い方: python import_questions.py --notion-key <KEY> --anthropic-key <KEY>")
        sys.exit(1)

notion = Client(auth=NOTION_API_KEY)
ai = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

def get_database_id():
    children = notion.blocks.children.list(block_id=PAGE_ID)
    for block in children["results"]:
        if block["type"] == "child_database":
            return block["id"]
    raise Exception("データベースが見つかりません")

DATABASE_ID = get_database_id()
print(f"DATABASE_ID: {DATABASE_ID}")

# ── タグオプションを Notion DB に追加 ──────────────────────────────
NEW_TAGS = ["CS", "受注", "発注", "請求書", "在庫更新", "シェア", "設定"]

def ensure_tags():
    db = notion.databases.retrieve(database_id=DATABASE_ID)
    existing = [o["name"] for o in db["properties"].get("タグ", {}).get("multi_select", {}).get("options", [])]
    to_add = [t for t in NEW_TAGS if t not in existing]
    if to_add:
        current = db["properties"]["タグ"]["multi_select"]["options"]
        colors = ["blue", "green", "orange", "pink", "purple", "red", "yellow"]
        new_options = current + [{"name": t, "color": colors[i % len(colors)]} for i, t in enumerate(to_add)]
        notion.databases.update(database_id=DATABASE_ID, properties={
            "タグ": {"multi_select": {"options": new_options}}
        })
        print(f"タグ追加: {to_add}")
    else:
        print("タグは全て登録済みです")

# ── rich_text ヘルパー（2000文字制限対応）──────────────────────────
def to_rich_text(text: str) -> list:
    if not text or text == "nan":
        return []
    chunks = [text[i:i+2000] for i in range(0, len(text), 2000)]
    return [{"text": {"content": c}} for c in chunks]

# ── タイトルをバッチ生成 ────────────────────────────────────────────
def generate_titles_batch(rows: list[dict]) -> list[str]:
    """20件ずつ Claude に投げてタイトルを返す"""
    items = "\n".join([f'{i}: {str(r["質問内容"])[:300]}' for i, r in enumerate(rows)])
    prompt = f"""以下の質問内容それぞれに対して、「大カテゴリ・中カテゴリ」形式の簡潔なタイトルを日本語で生成してください。
タイトルは20文字以内で、内容が一目でわかるようにしてください。

質問一覧:
{items}

必ず以下のJSON配列のみで返してください（説明文や```は不要）:
[{{"index": 0, "title": "タイトル"}}, {{"index": 1, "title": "タイトル"}}, ...]"""

    try:
        msg = ai.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = msg.content[0].text.strip()
        raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
        results = json.loads(raw)
        titles = [""] * len(rows)
        for r in results:
            titles[r["index"]] = r["title"]
        return titles
    except Exception as e:
        print(f"  タイトル生成失敗: {e} → フォールバック使用")
        return [f"{r['カテゴリ']}・{str(r['日付'])[:10]}" for r in rows]

# ── Notion にページ作成 ─────────────────────────────────────────────
def create_page(row: dict, title: str):
    質問日時 = None
    if pd.notna(row["日付"]):
        try:
            質問日時 = pd.to_datetime(row["日付"]).isoformat()
        except Exception:
            pass

    回答本文 = str(row["対処"]) if pd.notna(row["対処"]) else ""
    判断理由 = str(row["再発防止"]) if pd.notna(row["再発防止"]) else ""
    画像URL = str(row["備考"]) if pd.notna(row["備考"]) else ""

    props = {
        "質問タイトル": {"title": [{"text": {"content": title[:100]}}]},
        "質問本文": {"rich_text": to_rich_text(str(row["質問内容"]))},
        "ステータス": {"select": {"name": "回答済"}},
        "質問者": {"select": {"name": "インハナ"}},
        "タグ": {"multi_select": [{"name": str(row["カテゴリ"])}]},
        "AI学習済": {"checkbox": True},
    }
    if 質問日時:
        props["質問日時"] = {"date": {"start": 質問日時}}
    if 回答本文 and 回答本文 != "nan":
        props["回答本文"] = {"rich_text": to_rich_text(回答本文)}
    if 判断理由 and 判断理由 != "nan":
        props["判断理由詳細"] = {"rich_text": to_rich_text(判断理由)}
    if 画像URL and 画像URL != "nan":
        props["画像URL"] = {"rich_text": to_rich_text(画像URL)}

    notion.pages.create(parent={"database_id": DATABASE_ID}, properties=props)

# ── メイン処理 ──────────────────────────────────────────────────────
def main():
    test_mode = "--test" in sys.argv

    # --skip N --limit M オプション
    skip = 0
    limit = None
    if "--skip" in sys.argv:
        skip = int(sys.argv[sys.argv.index("--skip") + 1])
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    # データ読み込み
    df = pd.read_excel(EXCEL_PATH)
    df = df[df["質問内容"].notna() & (df["ステータス"] == "完了")].copy()
    if test_mode:
        df = df.head(5)
        print(f"[テストモード] 最初の5件のみ実行")
    elif skip or limit:
        df = df.iloc[skip:skip + limit if limit else None].reset_index(drop=True)
        print(f"[範囲指定] {skip+1}〜{skip+len(df)}件目")
    print(f"インポート対象: {len(df)}件")

    # タグはページ作成時にNotionが自動追加するためskip

    rows = df.to_dict("records")

    # タイトル生成（バッチ処理）
    print(f"\nタイトル生成中（{BATCH_SIZE}件ずつ）...")
    all_titles = []
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i+BATCH_SIZE]
        titles = generate_titles_batch(batch)
        all_titles.extend(titles)
        print(f"  {min(i+BATCH_SIZE, len(rows))}/{len(rows)} 件完了")
        time.sleep(1)

    # Notion インポート
    print(f"\nNotion へインポート中...")
    success, failed = 0, 0
    for i, (row, title) in enumerate(zip(rows, all_titles)):
        try:
            if not title:
                title = f"{row['カテゴリ']}・{str(row['日付'])[:10]}"
            create_page(row, title)
            success += 1
            if (i + 1) % 20 == 0:
                print(f"  {i+1}/{len(rows)} 件完了")
            time.sleep(NOTION_INTERVAL)
        except Exception as e:
            print(f"  NG [{i+1}] {title[:30]}: {e}")
            failed += 1
            time.sleep(1)

    print(f"\n完了: 成功 {success}件 / 失敗 {failed}件")

if __name__ == "__main__":
    main()
