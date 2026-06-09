# -*- coding: utf-8 -*-
"""2026-06-09以外の回答済質問を完了ステータスに一括更新"""
import sys, time, requests

NOTION_API_KEY = sys.argv[sys.argv.index("--notion-key") + 1]
DATABASE_ID = "37384fb2-35d7-802a-8452-f5a25a492ddd"
HEADERS = {"Authorization": f"Bearer {NOTION_API_KEY}", "Notion-Version": "2022-06-28", "Content-Type": "application/json"}
EXCLUDE_DATE = "2026-06-09"
DRY_RUN = "--apply" not in sys.argv

# 完了オプションを追加
db = requests.get(f"https://api.notion.com/v1/databases/{DATABASE_ID}", headers=HEADERS).json()
opts = db["properties"]["ステータス"]["select"]["options"]
if "完了" not in [o["name"] for o in opts]:
    requests.patch(f"https://api.notion.com/v1/databases/{DATABASE_ID}", headers=HEADERS,
                   json={"properties": {"ステータス": {"select": {"options": opts + [{"name": "完了", "color": "green"}]}}}})
    print("完了オプションを追加しました")

# 全ページ取得
pages = []
cursor = None
while True:
    body = {"page_size": 100}
    if cursor:
        body["start_cursor"] = cursor
    data = requests.post(f"https://api.notion.com/v1/databases/{DATABASE_ID}/query", headers=HEADERS, json=body).json()
    pages.extend(data["results"])
    if not data.get("has_more"):
        break
    cursor = data["next_cursor"]

print(f"全{len(pages)}件を確認中...")

targets = []
for p in pages:
    props = p["properties"]
    status_sel = props.get("ステータス", {}).get("select")
    status = status_sel["name"] if status_sel else ""
    date_obj = props.get("質問日時", {}).get("date")
    date_str = date_obj["start"][:10] if date_obj else ""
    if status == "回答済" and date_str != EXCLUDE_DATE:
        targets.append(p["id"])

print(f"対象（回答済かつ{EXCLUDE_DATE}以外）: {len(targets)}件")

if DRY_RUN:
    print("【DRY RUN】--apply を付けると実行します")
else:
    for i, pid in enumerate(targets):
        requests.patch(f"https://api.notion.com/v1/pages/{pid}", headers=HEADERS,
                       json={"properties": {"ステータス": {"select": {"name": "完了"}}}})
        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(targets)}件更新")
        time.sleep(0.35)
    print(f"完了: {len(targets)}件を完了ステータスに更新しました")
