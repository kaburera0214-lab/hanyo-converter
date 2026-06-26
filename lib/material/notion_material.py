# -*- coding: utf-8 -*-
"""
資材備品チェック〜発注システムのNotion永続化。

既存の請求書発行(lib/invoice/notion_store)・買掛(lib/payable/notion_payable)と
同じ親ページ(Secrets: INVOICE_NOTION_PARENT_PAGE_ID)配下に、「資材_」接頭の専用DBを
冪等生成する。既存DB(請求_*/支払_*)とはタイトルで分離するため干渉しない。

仕入先の口座・メール宛先は買掛の「支払_取引先マスタ」を参照するため、資材マスタ側は
紐付けキー(NE仕入先cd)と表示用の仕入先名のみを持つ(二重管理を避ける)。
"""
import json

# スキーマ(列)を変更したらこの版数を上げる。app_initがセッションキャッシュを
# 無視して ensure_databases(不足列の自動追加) を再実行する。
SCHEMA_VERSION = "2026-06-25c"

DB_SCHEMAS = {
    "資材_資材マスタ": {
        "資材名": {"title": {}},
        "品番": {"rich_text": {}},
        "カテゴリ": {"rich_text": {}},
        "NE仕入先cd": {"rich_text": {}},
        "仕入先名": {"rich_text": {}},
        "発注方法": {"select": {"options": [
            {"name": "メール発注", "color": "blue"},
            {"name": "社内チャット依頼", "color": "green"},
            {"name": "FAX発注", "color": "orange"},
        ]}},
        "ロット候補": {"rich_text": {}},
        "単価": {"rich_text": {}},
        "発注点": {"number": {}},
        "在庫定数": {"number": {}},
        "保管ロケーション": {"rich_text": {}},
        "有効フラグ": {"checkbox": {}},
        "備考": {"rich_text": {}},
    },
    "資材_棚卸": {
        "レコード名": {"title": {}},
        "棚卸日": {"rich_text": {}},
        "要発注件数": {"number": {}},
        "明細件数": {"number": {}},
        "明細JSON": {"rich_text": {}},
        "生成日時": {"rich_text": {}},
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


def _rt(value):
    s = "" if value is None else str(value)
    return [{"type": "text", "text": {"content": s[:2000]}}]


def _title(value):
    return [{"type": "text", "text": {"content": ("" if value is None else str(value))[:2000]}}]


def _read_rt(prop):
    items = prop.get("rich_text", []) if prop else []
    return items[0]["plain_text"] if items else ""


def _read_title(prop):
    items = prop.get("title", []) if prop else []
    return items[0]["plain_text"] if items else ""


def _read_num(prop):
    return prop.get("number") if prop else None


def _read_check(prop):
    return bool(prop.get("checkbox")) if prop else False


def _read_select(prop):
    sel = prop.get("select") if prop else None
    return sel["name"] if sel else ""


def _to_num(v):
    if v is None or str(v).strip() == "":
        return None
    try:
        return float(str(v).replace(",", ""))
    except (ValueError, TypeError):
        return None


def ensure_databases():
    """親ページ配下に資材_*のDBを冪等生成し {タイトル: id} を返す。"""
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
    """不足列の追加に加え、型が変わった列(例 単価 number→rich_text)も更新する。"""
    db = client.databases.retrieve(database_id=db_id)
    existing_props = db.get("properties", {})
    changes = {}
    for name, spec in schema.items():
        if "title" in spec:
            continue
        want_type = next(iter(spec))
        if name not in existing_props:
            changes[name] = spec                       # 不足列の追加
        elif existing_props[name].get("type") != want_type:
            changes[name] = spec                       # 型変更(値は空のため安全)
    if changes:
        client.databases.update(database_id=db_id, properties=changes)


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
# 資材マスタ
# ============================================================
MASTER_FIELDS = ["資材名", "品番", "カテゴリ", "NE仕入先cd", "仕入先名", "発注方法",
                 "ロット候補", "単価", "発注点", "在庫定数", "保管ロケーション",
                 "有効フラグ", "備考"]


def load_master(db_ids):
    """資材マスタを辞書リストで返す。"""
    rows = []
    for row in _query_all(db_ids["資材_資材マスタ"]):
        p = row["properties"]
        rows.append({
            "id": row["id"],
            "資材名": _read_title(p.get("資材名")),
            "品番": _read_rt(p.get("品番")),
            "カテゴリ": _read_rt(p.get("カテゴリ")),
            "NE仕入先cd": _read_rt(p.get("NE仕入先cd")),
            "仕入先名": _read_rt(p.get("仕入先名")),
            "発注方法": _read_select(p.get("発注方法")),
            "ロット候補": _read_rt(p.get("ロット候補")),
            "単価": _read_rt(p.get("単価")),
            "発注点": _read_num(p.get("発注点")) or "",
            "在庫定数": _read_num(p.get("在庫定数")) or "",
            "保管ロケーション": _read_rt(p.get("保管ロケーション")),
            "有効フラグ": "✓" if _read_check(p.get("有効フラグ")) else "",
            "備考": _read_rt(p.get("備考")),
        })
    rows.sort(key=lambda r: (r["仕入先名"], r["資材名"]))
    return rows


def _master_props(r):
    return {
        "資材名": {"title": _title(r.get("資材名", ""))},
        "品番": {"rich_text": _rt(r.get("品番", ""))},
        "カテゴリ": {"rich_text": _rt(r.get("カテゴリ", ""))},
        "NE仕入先cd": {"rich_text": _rt(r.get("NE仕入先cd", ""))},
        "仕入先名": {"rich_text": _rt(r.get("仕入先名", ""))},
        "発注方法": ({"select": {"name": r["発注方法"]}}
                  if str(r.get("発注方法", "")).strip() in ("メール発注", "社内チャット依頼", "FAX発注")
                  else {"select": None}),
        "ロット候補": {"rich_text": _rt(r.get("ロット候補", ""))},
        "単価": {"rich_text": _rt(r.get("単価", ""))},
        "発注点": {"number": _to_num(r.get("発注点", ""))},
        "在庫定数": {"number": _to_num(r.get("在庫定数", ""))},
        "保管ロケーション": {"rich_text": _rt(r.get("保管ロケーション", ""))},
        "有効フラグ": {"checkbox": str(r.get("有効フラグ", "")).strip() in ("✓", "1", "True", "true", "○")},
        "備考": {"rich_text": _rt(r.get("備考", ""))},
    }


def upsert_master_row(db_ids, r):
    """資材マスタ1行を保存。idがあれば更新、無ければ新規。"""
    client = _client()
    rid = str(r.get("id") or "").strip()
    props = _master_props(r)
    if rid and rid.lower() not in ("nan", "none"):
        client.pages.update(page_id=rid, properties=props)
    else:
        client.pages.create(parent={"database_id": db_ids["資材_資材マスタ"]}, properties=props)


def delete_master_row(db_ids, page_id):
    _client().pages.update(page_id=page_id, archived=True)


def seed_master_missing(db_ids, seed_rows):
    """既存の資材名はスキップし、未登録の資材名だけ追加する(冪等)。"""
    existing = set()
    for row in _query_all(db_ids["資材_資材マスタ"]):
        nm = _read_title(row["properties"].get("資材名")).strip()
        if nm:
            existing.add(nm)
    rows = [r for r in seed_rows if str(r.get("資材名", "")).strip() not in existing]
    client = _client()
    db = db_ids["資材_資材マスタ"]
    n = 0
    for r in rows:
        if not str(r.get("資材名", "")).strip():
            continue
        client.pages.create(parent={"database_id": db}, properties=_master_props(r))
        n += 1
    return n


# ============================================================
# 棚卸スナップショット
# ============================================================
def save_stocktake(db_ids, *, 棚卸日, 明細):
    """
    棚卸チェックの結果スナップショットを保存する。
    明細: [{資材名, NE仕入先cd, 仕入先名, 現在庫, 発注点, 在庫定数, ロット, 要発注, 発注数量, 単価}]
    """
    import datetime
    client = _client()
    n_order = sum(1 for d in 明細 if d.get("要発注"))
    client.pages.create(parent={"database_id": db_ids["資材_棚卸"]}, properties={
        "レコード名": {"title": _title(f"{棚卸日} 棚卸 要発注{n_order}件")},
        "棚卸日": {"rich_text": _rt(棚卸日)},
        "要発注件数": {"number": n_order},
        "明細件数": {"number": len(明細)},
        "明細JSON": {"rich_text": _rt(json.dumps(明細, ensure_ascii=False))},
        "生成日時": {"rich_text": _rt(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))},
    })


def load_stocktakes(db_ids):
    rows = []
    for row in _query_all(db_ids["資材_棚卸"]):
        p = row["properties"]
        try:
            detail = json.loads(_read_rt(p.get("明細JSON")) or "[]")
        except Exception:  # noqa: BLE001
            detail = []
        rows.append({
            "id": row["id"],
            "棚卸日": _read_rt(p.get("棚卸日")),
            "要発注件数": _read_num(p.get("要発注件数")) or 0,
            "明細件数": _read_num(p.get("明細件数")) or 0,
            "明細": detail,
            "生成日時": _read_rt(p.get("生成日時")),
        })
    rows.sort(key=lambda r: r["生成日時"], reverse=True)
    return rows
