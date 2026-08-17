# -*- coding: utf-8 -*-
"""
Notionのrich_textプロパティの読み書き。

Notionのrich_textは「配列の1要素につき2000文字」が上限で、配列自体は
複数要素を持てる。つまりプロパティ全体では2000文字を超えて保存できる。

これまでは
  - 書き込み側が1要素しか作らず（会話ログを1900文字で切って先頭を捨てていた）
  - 読み出し側も items[0] しか見ていなかった（2000文字超の質問本文が黙って切れる）
という2点で、長い会話が失われていた。ここを直して全文を通す。

  to_rich_text(text) … 分割した配列を作る（保存用）
  get_text(prop)     … 全要素を連結して返す（読み出し用）
"""

# 1要素の上限は2000文字。サロゲートペアの数え方の違いで弾かれないよう余裕を持たせる。
CHUNK = 1900
# rich_text配列は100要素まで。上限に張り付く前に運用で気づけるよう90で止める。
MAX_CHUNKS = 90
MAX_CHARS = CHUNK * MAX_CHUNKS  # 171,000文字


def split_chunks(text):
    """文字列を CHUNK 文字ごとに分割する。空文字なら空リスト。"""
    text = text or ""
    if not text:
        return []
    return [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)]


def to_rich_text(text):
    """保存用のrich_text配列を作る。全文が入る（2000文字で切らない）。

    MAX_CHARS を超えた分は末尾を残して先頭を落とす。ここに到達するのは
    17万文字を超えたときだけなので、実運用では起こらない想定。
    """
    text = text or ""
    if len(text) > MAX_CHARS:
        text = "（このプロパティの上限を超えたため、古い部分を省略しています）\n" + text[-(MAX_CHARS - 100):]
    return [{"text": {"content": c}} for c in split_chunks(text)]


def get_text(prop):
    """rich_textプロパティを全文で読む。全要素を連結する。"""
    if not prop:
        return ""
    items = prop.get("rich_text") or []
    return "".join(item.get("plain_text", "") for item in items)


def will_truncate(text):
    """保存すると省略が発生するか（画面で警告を出したいとき用）。"""
    return len(text or "") > MAX_CHARS
