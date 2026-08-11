# -*- coding: utf-8 -*-
"""
質問・回答管理DBの「編集履歴」の記録者を、実態（lib/qa/history.ACTOR_BY_ACTION）に
合わせて過去分まで書き換える一回限りのスクリプト。

もともと回答管理ページは追加質問・完了も「パピー」で記録していたため、
実際にはインハナさんの操作なのに「パピー：追加質問」「パピー：完了」と
残っている行がある。それを直す。

実行（hanyo-converter直下）:
    python tools/fix_qa_history_actor.py            # 変更内容の確認だけ（書き込まない）
    python tools/fix_qa_history_actor.py --apply    # Notionへ書き込む

NOTION_API_KEY は .streamlit/secrets.toml から読む。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from notion_client import Client  # noqa: E402

from lib.qa.history import retag_history  # noqa: E402

PAGE_ID = "37384fb235d780b88a46eb8d619a19ad"
SECRETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".streamlit", "secrets.toml")


def load_api_key():
    try:
        import tomllib
        with open(SECRETS, "rb") as f:
            raw = tomllib.load(f).get("NOTION_API_KEY", "")
    except FileNotFoundError:
        raw = os.environ.get("NOTION_API_KEY", "")
    key = "".join(c for c in raw if c.isprintable() and ord(c) < 128)
    if not key:
        sys.exit("NOTION_API_KEY が見つかりません（.streamlit/secrets.toml か環境変数）")
    return key


def get_database_id(client):
    for block in client.blocks.children.list(block_id=PAGE_ID)["results"]:
        if block["type"] == "child_database":
            return block["id"]
    return PAGE_ID


def get_text(prop):
    items = (prop or {}).get("rich_text", [])
    return items[0]["plain_text"] if items else ""


def main():
    apply = "--apply" in sys.argv
    client = Client(auth=load_api_key())
    database_id = get_database_id(client)

    pages, cursor = [], None
    while True:
        kwargs = {"database_id": database_id, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        res = client.databases.query(**kwargs)
        pages.extend(res["results"])
        if not res.get("has_more"):
            break
        cursor = res["next_cursor"]

    print(f"対象ページ数: {len(pages)}　モード: {'APPLY（書き込む）' if apply else 'DRY-RUN（確認のみ）'}\n")

    targets = []
    for page in pages:
        props = page["properties"]
        before = get_text(props.get("編集履歴"))
        after, changed = retag_history(before)
        if not changed:
            continue
        num = props.get("ID", {}).get("unique_id", {}).get("number")
        title_prop = props.get("質問タイトル", {}).get("title", [])
        title = title_prop[0]["plain_text"] if title_prop else "（タイトルなし）"
        targets.append((page["id"], num, title, before, after, changed))

    if not targets:
        print("書き換える行はありませんでした。")
        return

    total_lines = 0
    for _, num, title, before, after, changed in targets:
        total_lines += changed
        print(f"── #{num} {title[:40]}（{changed}行）")
        for b, a in zip(before.split("\n"), after.split("\n")):
            if b != a:
                print(f"   - {b}\n   + {a}")
        print()

    print(f"合計: {len(targets)}ページ / {total_lines}行")

    if not apply:
        print("\n書き込みは行っていません。--apply を付けると反映します。")
        return

    for page_id, num, _, _, after, _ in targets:
        client.pages.update(
            page_id=page_id,
            properties={"編集履歴": {"rich_text": [{"text": {"content": after}}]}},
        )
        print(f"更新: #{num}")
    print(f"\n完了: {len(targets)}ページを更新しました。")


if __name__ == "__main__":
    main()
