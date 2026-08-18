# -*- coding: utf-8 -*-
"""
編集履歴の「記録者」を実態に合わせる（過去分の一括修正）。

背景:
  以前は回答管理ページからの操作をすべて「パピー」で記録していたため、
  実際はインハナさんの操作である「追加質問」「完了」がパピー名で残っている。
  記録者はアクション種別から決まる（lib/qa/history.ACTOR_BY_ACTION）ので、
  そこに合わせて直す。

  もともと回答管理ページのチェックボックスから実行できたが、一度直せば
  基本的に不要なので、画面から外してこのスクリプトに移した（2026-08-18）。

使い方:
  python repair_history_actor.py          # ドライラン（何も書き込まない）
  python repair_history_actor.py --live   # 実際にNotionを更新する

  何度実行しても結果は同じ（対象が無くなれば「修正不要」と出るだけ）。
"""
import getpass
import sys
import tomllib
from pathlib import Path

from notion_client import Client

sys.path.insert(0, str(Path(__file__).parent))

from lib.qa.history import retag_history  # noqa: E402
from lib.qa.notion_text import get_text, to_rich_text  # noqa: E402

PAGE_ID = "37384fb235d780b88a46eb8d619a19ad"
SECRETS = Path(__file__).parent / ".streamlit" / "secrets.toml"


def load_key():
    if SECRETS.exists():
        try:
            with open(SECRETS, "rb") as f:
                key = tomllib.load(f).get("NOTION_API_KEY", "")
            key = "".join(c for c in key if c.isprintable() and ord(c) < 128)
            if key:
                print("secrets.toml のキーを使います")
                return key
        except Exception:
            pass
    key = getpass.getpass("NOTION_API_KEY を貼り付けてEnter: ").strip()
    return "".join(c for c in key if c.isprintable() and ord(c) < 128)


def make_query(client, db_id):
    """notion-client 2.x（databases.query）と新しい版（data_sources.query）の両対応。"""
    if hasattr(client, "data_sources"):
        sources = (client.databases.retrieve(database_id=db_id) or {}).get("data_sources") or []
        if sources:
            ds = sources[0]["id"]
            return lambda **kw: client.data_sources.query(data_source_id=ds, **kw)
    return lambda **kw: client.databases.query(database_id=db_id, **kw)


def main():
    live = "--live" in sys.argv
    key = load_key()
    if not key:
        print("キーが空です。中止します。")
        return 1

    n = Client(auth=key)
    db_id = None
    for b in n.blocks.children.list(block_id=PAGE_ID)["results"]:
        if b["type"] == "child_database":
            db_id = b["id"]
    if not db_id:
        print("データベースが見つかりません。")
        return 1

    query = make_query(n, db_id)
    rows, cursor = [], None
    while True:
        kw = {"page_size": 100}
        if cursor:
            kw["start_cursor"] = cursor
        res = query(**kw)
        rows += res["results"]
        if not res.get("has_more"):
            break
        cursor = res["next_cursor"]
    print(f"取得 {len(rows)}件\n")

    targets = []
    for page in rows:
        p = page["properties"]
        history = get_text(p.get("編集履歴", {}))
        fixed, changed = retag_history(history)
        if not changed:
            continue
        num = (p.get("ID", {}).get("unique_id") or {}).get("number")
        title = "".join(t["plain_text"] for t in p.get("質問タイトル", {}).get("title", []))
        targets.append((page["id"], num, title, history, fixed, changed))

    if not targets:
        print("修正が必要な履歴はありません。")
        return 0

    total = sum(t[5] for t in targets)
    print(f"修正対象 {len(targets)}件（計{total}行）\n")
    for _pid, num, title, before, after, changed in targets:
        print("=" * 74)
        print(f"#{num} {title}（{changed}行）")
        for b, a in zip(before.split("\n"), after.split("\n")):
            if b != a:
                print(f"  - {b}")
                print(f"  + {a}")

    if not live:
        print(f"\nドライランです。実際に更新するには --live を付けてください（{len(targets)}件）")
        return 0

    ok = 0
    for pid, num, _t, _b, after, _c in targets:
        try:
            n.pages.update(page_id=pid, properties={"編集履歴": {"rich_text": to_rich_text(after)}})
            ok += 1
        except Exception as e:  # noqa: BLE001 - 1件失敗しても続ける
            print(f"  NG #{num}: {type(e).__name__} {e}")
    print(f"\n更新しました: {ok}/{len(targets)}件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
