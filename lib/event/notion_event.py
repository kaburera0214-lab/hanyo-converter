# -*- coding: utf-8 -*-
"""
イベントLP作成システムのNotion永続化。

既存の請求書発行/買掛と同じ親ページ(Secrets: INVOICE_NOTION_PARENT_PAGE_ID)配下に、
「イベント_」接頭の専用DBを冪等生成する。既存DB(請求_*/支払_*)とはタイトルで分離。

セクション構成や商品スナップショットはJSON文字列で保存する。Notionのrich_textは
1要素2000字までのため、複数要素に分割して保存し、読み出し時に結合する
(_rt_long / _read_rt_full)。支払系の _rt は2000字で切り捨てる仕様なので流用しない。
"""
import datetime
import json

# スキーマ(列)を変更したらこの版数を上げる。app_initがセッションキャッシュを
# 無視して ensure_databases(不足列の自動追加) を再実行する。
SCHEMA_VERSION = "2026-07-09a"

STATUS_OPTIONS = ["下書き", "生成済", "公開中", "終了"]

DB_SCHEMAS = {
    "イベント_イベント": {
        "イベント名": {"title": {}},
        "期間開始": {"rich_text": {}},
        "期間終了": {"rich_text": {}},
        "ステータス": {"select": {"options": [
            {"name": "下書き", "color": "gray"},
            {"name": "生成済", "color": "blue"},
            {"name": "公開中", "color": "green"},
            {"name": "終了", "color": "default"},
        ]}},
        "キャッチコピー": {"rich_text": {}},
        "テーマカラー": {"rich_text": {}},
        "GOLDパス": {"rich_text": {}},
        "公開URL": {"rich_text": {}},
        "セクションJSON": {"rich_text": {}},
        "商品スナップショットJSON": {"rich_text": {}},
        "生成日時": {"rich_text": {}},
        "最終アップ日時": {"rich_text": {}},
    },
}


# ---- 低レベルヘルパ(payable/notion_payableと同方針) ----
def _get_key():
    import streamlit as st
    return "".join(c for c in st.secrets["NOTION_API_KEY"]
                   if c.isprintable() and ord(c) < 128)


def _get_parent_page_id():
    import streamlit as st
    return st.secrets.get("INVOICE_NOTION_PARENT_PAGE_ID", "")


def _client():
    from notion_client import Client
    return Client(auth=_get_key())


def _title(value):
    return [{"type": "text", "text": {"content": ("" if value is None else str(value))[:2000]}}]


def _rt(value):
    s = "" if value is None else str(value)
    return [{"type": "text", "text": {"content": s[:2000]}}]


def _rt_long(value, max_chunks=95):
    """2000字を超える文字列(JSON等)を複数のtext要素に分割して保存する。"""
    s = "" if value is None else str(value)
    if not s:
        return [{"type": "text", "text": {"content": ""}}]
    chunks = [s[i:i + 2000] for i in range(0, len(s), 2000)]
    if len(chunks) > max_chunks:
        raise ValueError(f"保存データが大きすぎます({len(s)}文字)。商品数やセクション数を減らしてください。")
    return [{"type": "text", "text": {"content": c}} for c in chunks]


def _read_rt(prop):
    items = prop.get("rich_text", []) if prop else []
    return items[0]["plain_text"] if items else ""


def _read_rt_full(prop):
    """分割保存されたrich_textを結合して返す。"""
    items = prop.get("rich_text", []) if prop else []
    return "".join(it.get("plain_text", "") for it in items)


def _read_title(prop):
    items = prop.get("title", []) if prop else []
    return items[0]["plain_text"] if items else ""


def _read_select(prop):
    sel = prop.get("select") if prop else None
    return sel["name"] if sel else ""


def ensure_databases():
    """親ページ配下にイベント_*のDBを冪等生成し {タイトル: id} を返す。"""
    parent = _get_parent_page_id()
    if not parent:
        raise RuntimeError("Secrets に INVOICE_NOTION_PARENT_PAGE_ID が設定されていません。")
    client = _client()
    existing = {}
    cursor = None
    while True:
        kwargs = {"block_id": parent, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = client.blocks.children.list(**kwargs)
        for block in resp["results"]:
            if block["type"] == "child_database":
                existing[block["child_database"].get("title", "")] = block["id"]
        if resp.get("has_more"):
            cursor = resp.get("next_cursor")
        else:
            break
    result = {}
    for title, props in DB_SCHEMAS.items():
        if title in existing:
            result[title] = existing[title]
            _sync_db_properties(client, existing[title], props)
        else:
            created = client.databases.create(
                parent={"type": "page_id", "page_id": parent},
                title=[{"type": "text", "text": {"content": title}}],
                properties=props,
            )
            result[title] = created["id"]
    return result


def _sync_db_properties(client, db_id, schema):
    db = client.databases.retrieve(database_id=db_id)
    existing_props = db.get("properties", {})
    missing = {name: spec for name, spec in schema.items()
               if name not in existing_props and "title" not in spec}
    if missing:
        client.databases.update(database_id=db_id, properties=missing)


def _query_all(db_id):
    client = _client()
    rows, cursor = [], None
    while True:
        kwargs = {"database_id": db_id, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = client.databases.query(**kwargs)
        rows.extend(resp["results"])
        if resp.get("has_more"):
            cursor = resp.get("next_cursor")
        else:
            break
    return rows


# ============================================================
# イベント
# ============================================================
def _parse_json(text, default):
    try:
        return json.loads(text) if text else default
    except Exception:  # noqa: BLE001
        return default


def load_events(db_ids):
    """イベント一覧を辞書リストで返す(セクション/スナップショットはパース済み)。"""
    rows = []
    for row in _query_all(db_ids["イベント_イベント"]):
        p = row["properties"]
        rows.append({
            "id": row["id"],
            "イベント名": _read_title(p.get("イベント名")),
            "期間開始": _read_rt(p.get("期間開始")),
            "期間終了": _read_rt(p.get("期間終了")),
            "ステータス": _read_select(p.get("ステータス")) or "下書き",
            "キャッチコピー": _read_rt(p.get("キャッチコピー")),
            "テーマカラー": _read_rt(p.get("テーマカラー")),
            "GOLDパス": _read_rt(p.get("GOLDパス")),
            "公開URL": _read_rt(p.get("公開URL")),
            "セクション": _parse_json(_read_rt_full(p.get("セクションJSON")), []),
            "商品スナップショット": _parse_json(_read_rt_full(p.get("商品スナップショットJSON")), {}),
            "生成日時": _read_rt(p.get("生成日時")),
            "最終アップ日時": _read_rt(p.get("最終アップ日時")),
        })
    rows.sort(key=lambda r: r.get("生成日時") or "", reverse=True)
    return rows


def upsert_event(db_ids, ev):
    """
    イベント1件を保存。ev["id"]があれば更新、無ければ新規作成しidを返す。
    ev: イベント名/期間開始/期間終了/ステータス/キャッチコピー/テーマカラー/
        GOLDパス/公開URL/セクション(list)/商品スナップショット(dict)/最終アップ日時
    """
    client = _client()
    props = {
        "イベント名": {"title": _title(ev.get("イベント名", ""))},
        "期間開始": {"rich_text": _rt(ev.get("期間開始", ""))},
        "期間終了": {"rich_text": _rt(ev.get("期間終了", ""))},
        "キャッチコピー": {"rich_text": _rt(ev.get("キャッチコピー", ""))},
        "テーマカラー": {"rich_text": _rt(ev.get("テーマカラー", ""))},
        "GOLDパス": {"rich_text": _rt(ev.get("GOLDパス", ""))},
        "公開URL": {"rich_text": _rt(ev.get("公開URL", ""))},
        "セクションJSON": {"rich_text": _rt_long(
            json.dumps(ev.get("セクション", []), ensure_ascii=False))},
        "商品スナップショットJSON": {"rich_text": _rt_long(
            json.dumps(ev.get("商品スナップショット", {}), ensure_ascii=False))},
        "生成日時": {"rich_text": _rt(
            ev.get("生成日時") or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))},
        "最終アップ日時": {"rich_text": _rt(ev.get("最終アップ日時", ""))},
    }
    status = ev.get("ステータス", "下書き")
    props["ステータス"] = {"select": {"name": status if status in STATUS_OPTIONS else "下書き"}}
    page_id = str(ev.get("id") or "").strip()
    if page_id:
        client.pages.update(page_id=page_id, properties=props)
        return page_id
    created = client.pages.create(
        parent={"database_id": db_ids["イベント_イベント"]}, properties=props)
    return created["id"]


def delete_event(db_ids, page_id):
    _client().pages.update(page_id=page_id, archived=True)
