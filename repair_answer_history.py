# -*- coding: utf-8 -*-
"""
追加回答で消えた「元の回答」を、会話ログから回答本文へ戻す（1回限りの修復）。

背景:
  2026-08-13以前の回答管理は、追加回答を送るときに 回答本文 を上書きしていた。
  そのため、ラリーした質問では最初の回答が 回答本文 から消え、会話ログにだけ残っている。
  AIドラフトは「質問本文＋回答本文」のペアを事例として読むので、
  このままだと噛み合わないQ&Aを学習し続ける。

やること:
  会話ログの 【A】/【追加A】 を順につないで 回答本文 を組み直す。
  会話ログが無い質問、既に整合している質問は触らない。

使い方:
  python repair_answer_history.py             # ドライラン（何も書き込まない）
  python repair_answer_history.py --live      # 実際にNotionを更新する

  NOTION_API_KEY は実行時に聞かれる（画面には出ない）。
  .streamlit/secrets.toml に入っていればそちらを使う。
"""
import getpass
import sys
import tomllib
from pathlib import Path

from notion_client import Client

sys.path.insert(0, str(Path(__file__).parent))

from lib.qa.notion_text import get_text, to_rich_text  # noqa: E402
from lib.qa.thread_ui import parse_conversation  # noqa: E402

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


def rebuild_answer(log):
    """会話ログから回答本文を組み直す。組めなければ (None, 理由)。

    戻り値: (組み直した回答本文, 注記)
    """
    turns, preamble = parse_conversation(log)
    answers = [t for t in turns if t["kind"] in ("A", "追加A")]
    if not answers:
        return None, "回答のターンが無い"
    parts = [answers[0]["body"].strip()]
    for t in answers[1:]:
        stamp = t["timestamp"] or "日時不明"
        parts.append(f"【追加回答｜{stamp}】\n{t['body'].strip()}")
    note = ""
    if "古い会話を省略" in (preamble or "") or turns[0]["kind"] != "Q":
        note = "※会話ログの先頭が欠けているため、最初の回答も一部しか戻せません"
    return "\n\n".join(p for p in parts if p), note


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

    targets, danger, skipped, warned = [], [], 0, 0
    for page in rows:
        p = page["properties"]
        log = get_text(p.get("会話ログ", {}))
        if not log.strip():
            continue
        current = get_text(p.get("回答本文", {}))
        rebuilt, note = rebuild_answer(log)
        if not rebuilt:
            skipped += 1
            continue
        num = (p.get("ID", {}).get("unique_id") or {}).get("number")
        title = "".join(t["plain_text"] for t in p.get("質問タイトル", {}).get("title", []))

        # 既に元の回答が含まれているなら触らない
        first_answer = rebuilt.split("\n\n【追加回答｜")[0].strip()
        if first_answer and first_answer[:80] in current:
            skipped += 1
            continue

        # 安全弁：いま回答本文に入っている文章が、組み直した文章に丸ごと含まれていること。
        # 会話ログの先頭が欠けている質問では、組み直すと逆に情報が減ることがあるため、
        # 「増えるときだけ書き換える」に限定する。減る側は手作業に回す。
        if current.strip() and current.strip() not in rebuilt:
            danger.append((num, title, len(current), len(rebuilt), note))
            continue

        targets.append((page["id"], num, title, current, rebuilt, note))
        if note:
            warned += 1

    print(f"修復対象 {len(targets)}件 / 変更不要 {skipped}件 / 手作業に回す {len(danger)}件")
    print(f"（修復対象のうち、先頭欠けのため部分復元になるもの {warned}件）\n")
    if danger:
        print("■ 自動では触らないもの（組み直すと今ある文章が減るため）")
        for num, title, cur_len, reb_len, note in danger:
            print(f"   #{num} {title[:44]}  現在{cur_len}字 → 組み直し{reb_len}字")
            print(f"       {note or 'Notionのページ履歴から会話ログを復元するのが確実です'}")
        print()
    for _pid, num, title, current, rebuilt, note in targets:
        print("=" * 74)
        print(f"#{num} {title}")
        if note:
            print(f"  {note}")
        print(f"  現在 ({len(current):>5}字): {current[:70].replace(chr(10), ' ')}")
        print(f"  修復 ({len(rebuilt):>5}字): {rebuilt[:70].replace(chr(10), ' ')}")

    if not targets:
        print("修復するものはありません。")
        return 0
    if not live:
        print(f"\nドライランです。実際に更新するには --live を付けてください（{len(targets)}件）")
        return 0

    ok = 0
    for pid, num, _t, _c, rebuilt, _n in targets:
        try:
            n.pages.update(page_id=pid, properties={"回答本文": {"rich_text": to_rich_text(rebuilt)}})
            ok += 1
        except Exception as e:  # noqa: BLE001 - 1件失敗しても続ける
            print(f"  NG #{num}: {type(e).__name__} {e}")
    print(f"\n更新しました: {ok}/{len(targets)}件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
