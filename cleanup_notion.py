# -*- coding: utf-8 -*-
"""Notion DBの全ページを削除するスクリプト"""
import sys, time, requests

NOTION_API_KEY = sys.argv[sys.argv.index("--notion-key") + 1] if "--notion-key" in sys.argv else ""
DATABASE_ID = "37384fb2-35d7-802a-8452-f5a25a492ddd"
HEADERS = {"Authorization": f"Bearer {NOTION_API_KEY}", "Notion-Version": "2022-06-28", "Content-Type": "application/json"}

print("全ページを取得中...")
page_ids = []
cursor = None
while True:
    body = {"page_size": 100}
    if cursor:
        body["start_cursor"] = cursor
    r = requests.post(f"https://api.notion.com/v1/databases/{DATABASE_ID}/query", headers=HEADERS, json=body)
    data = r.json()
    page_ids.extend([p["id"] for p in data["results"]])
    print(f"  取得済み: {len(page_ids)}件")
    if not data.get("has_more"):
        break
    cursor = data["next_cursor"]

print(f"\n合計 {len(page_ids)}件を削除します...")
for i, pid in enumerate(page_ids):
    requests.patch(f"https://api.notion.com/v1/pages/{pid}", headers=HEADERS, json={"archived": True})
    if (i + 1) % 50 == 0:
        print(f"  {i+1}/{len(page_ids)}件削除済み")
    time.sleep(0.35)

print(f"\n完了: {len(page_ids)}件削除しました")
