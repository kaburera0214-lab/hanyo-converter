# -*- coding: utf-8 -*-
"""
請求書発行機能のNotion永続化。

親ページ（Secrets: INVOICE_NOTION_PARENT_PAGE_ID）配下に、請求書専用の
データベースを冪等に自動生成し、マスタ／履歴の読み書きを行う。
既存の質問機能のNotion DBとは別物（タイトルで区別）なので干渉しない。

データ層の考え方:
  - マスタ層: 最新値を保持（クライアント情報・単価）
  - 履歴層: 発行ごと・月次ごとに不変レコードを蓄積（発行履歴・保管内訳）
"""
import json

# データベース定義（タイトル -> Notionプロパティschema）
# title型プロパティは各DBに必ず1つ必要。
DB_SCHEMAS = {
    "請求_クライアントマスタ": {
        "クライアント名": {"title": {}},
        "略号": {"rich_text": {}},
        "取引先名称": {"rich_text": {}},
        "郵便番号": {"rich_text": {}},
        "都道府県": {"rich_text": {}},
        "住所1": {"rich_text": {}},
        "住所2": {"rich_text": {}},
        "件名": {"rich_text": {}},
        "備考": {"rich_text": {}},
        "振込先": {"rich_text": {}},
        "自社担当者": {"rich_text": {}},
        # 送料の請求方式（クライアント別）
        "送料方式": {"select": {"options": [
            {"name": "送料表", "color": "blue"},
            {"name": "実費マージン", "color": "green"},
        ]}},
        "送料マージン率": {"number": {}},
        "送料加算額": {"number": {}},
    },
    "請求_単価マスタ": {
        "項目名": {"title": {}},
        "クライアント": {"rich_text": {}},
        "費目": {"select": {"options": [
            {"name": "保管", "color": "blue"},
            {"name": "送料", "color": "green"},
            {"name": "出荷作業", "color": "orange"},
            {"name": "資材", "color": "yellow"},
            {"name": "受注作業", "color": "purple"},
            {"name": "その他", "color": "gray"},
        ]}},
        "種別": {"rich_text": {}},
        "単価": {"number": {}},
        "出力品名": {"rich_text": {}},
        "マージン率": {"number": {}},
        "加算額": {"number": {}},
        "備考": {"rich_text": {}},
    },
    "請求_保管内訳履歴": {
        "レコード名": {"title": {}},
        "クライアント": {"rich_text": {}},
        "対象年月": {"rich_text": {}},
        "種別": {"rich_text": {}},
        "15日数量": {"number": {}},
        "末日数量": {"number": {}},
        "平均数量": {"number": {}},
        "単価": {"number": {}},
        "金額": {"number": {}},
        "出力品名": {"rich_text": {}},
    },
    "請求_イレギュラー作業": {
        "レコード名": {"title": {}},
        "クライアント": {"rich_text": {}},
        "日付": {"rich_text": {}},
        "時間数": {"number": {}},
        "人数": {"number": {}},
        "合計時間": {"number": {}},
        "作業項目": {"rich_text": {}},
        "作業詳細": {"rich_text": {}},
        "備考": {"rich_text": {}},
    },
    "請求_保管カウント": {
        "レコード名": {"title": {}},
        "クライアント": {"rich_text": {}},
        "対象年月": {"rich_text": {}},
        "期": {"select": {"options": [
            {"name": "第1期", "color": "blue"},
            {"name": "第2期", "color": "green"},
        ]}},
        "カウント日": {"rich_text": {}},
        "種別": {"rich_text": {}},
        "エリア": {"rich_text": {}},
        "ロケーション": {"rich_text": {}},
        "数量": {"number": {}},
        "備考": {"rich_text": {}},
    },
    "請求_発行履歴": {
        "請求書番号": {"title": {}},
        "クライアント": {"rich_text": {}},
        "対象年月": {"rich_text": {}},
        "区分": {"select": {"options": [
            {"name": "請求", "color": "blue"},
            {"name": "見積", "color": "orange"},
        ]}},
        "請求日": {"rich_text": {}},
        "支払期限": {"rich_text": {}},
        "小計": {"number": {}},
        "消費税": {"number": {}},
        "合計金額": {"number": {}},
        "品目JSON": {"rich_text": {}},
        "発行日時": {"rich_text": {}},
    },
}


# 送料表DB・地域マスタDBは可変列（地域）を含むため、storeの定義から動的生成
from . import store as _store  # storeはnotionを参照しないので循環しない

DB_SCHEMAS["請求_地域マスタ"] = {
    "都道府県": {"title": {}},
    "エリア": {"rich_text": {}},
}

_shipping_props = {
    "行キー": {"title": {}},
    "クライアント": {"rich_text": {}},
    "配送業者": {"rich_text": {}},
    "配送区分": {"rich_text": {}},
    "サイズ": {"rich_text": {}},
}
for _area in _store.SHIPPING_AREAS:
    _shipping_props[_area] = {"number": {}}
DB_SCHEMAS["請求_送料表"] = _shipping_props


def _get_key():
    import streamlit as st
    # 既存ページと同じく非ASCII混入を除去
    return "".join(c for c in st.secrets["NOTION_API_KEY"] if c.isprintable() and ord(c) < 128)


def _get_parent_page_id():
    import streamlit as st
    return st.secrets.get("INVOICE_NOTION_PARENT_PAGE_ID", "")


def _client():
    from notion_client import Client
    return Client(auth=_get_key())


def _rt(value):
    """rich_text用のペイロードを作る。"""
    return [{"type": "text", "text": {"content": str(value) if value is not None else ""}}]


def _title(value):
    return [{"type": "text", "text": {"content": str(value) if value is not None else ""}}]


def _read_rt(prop):
    items = prop.get("rich_text", []) if prop else []
    return items[0]["plain_text"] if items else ""


def _read_title(prop):
    items = prop.get("title", []) if prop else []
    return items[0]["plain_text"] if items else ""


def _read_num(prop):
    return prop.get("number") if prop else None


def _read_select(prop):
    sel = prop.get("select") if prop else None
    return sel["name"] if sel else ""


def ensure_databases():
    """
    親ページ配下に必要なDBを冪等に作成し、{タイトル: database_id} を返す。
    既存があればそのIDを使い回す（タイトル一致で判定）。
    """
    parent = _get_parent_page_id()
    if not parent:
        raise RuntimeError(
            "Secrets に INVOICE_NOTION_PARENT_PAGE_ID が設定されていません。")
    client = _client()

    # 親ページ直下の child_database を列挙してタイトル→IDを得る
    existing = {}
    cursor = None
    while True:
        kwargs = {"block_id": parent, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = client.blocks.children.list(**kwargs)
        for block in resp["results"]:
            if block["type"] == "child_database":
                title = block["child_database"].get("title", "")
                existing[title] = block["id"]
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
    """既存DBに不足しているプロパティ（title型を除く）を追加する。"""
    db = client.databases.retrieve(database_id=db_id)
    existing_props = db.get("properties", {})
    missing = {}
    for name, spec in schema.items():
        if name in existing_props:
            continue
        if "title" in spec:  # title型は既存があるはずなので追加しない
            continue
        missing[name] = spec
    if missing:
        client.databases.update(database_id=db_id, properties=missing)


def _query_all(db_id):
    """DBの全ページを取得。"""
    client = _client()
    rows = []
    cursor = None
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
# クライアント＋単価マスタ → アプリ内のclients辞書へ変換
# ============================================================
def load_clients(db_ids):
    """
    Notionの クライアントマスタ＋単価マスタ(費目=保管) を読み、
    store.py と同じ構造の clients 辞書を返す。データが無ければ空辞書。
    """
    clients = {}
    for row in _query_all(db_ids["請求_クライアントマスタ"]):
        p = row["properties"]
        name = _read_title(p.get("クライアント名"))
        if not name:
            continue
        clients[name] = {
            "略号": _read_rt(p.get("略号")),
            "header": {
                "取引先名称": _read_rt(p.get("取引先名称")),
                "件名": _read_rt(p.get("件名")),
                "取引先郵便番号": _read_rt(p.get("郵便番号")),
                "取引先都道府県": _read_rt(p.get("都道府県")),
                "取引先住所1": _read_rt(p.get("住所1")),
                "取引先住所2": _read_rt(p.get("住所2")),
                "取引先敬称": "",
                "備考": _read_rt(p.get("備考")),
                "振込先": _read_rt(p.get("振込先")),
                "自社担当者氏名": _read_rt(p.get("自社担当者")),
            },
            "保管料マスタ": [],
        }

    # 単価マスタの 費目=保管 を各クライアントの保管料マスタへ
    for row in _query_all(db_ids["請求_単価マスタ"]):
        p = row["properties"]
        if _read_select(p.get("費目")) != "保管":
            continue
        cname = _read_rt(p.get("クライアント"))
        if cname not in clients:
            continue
        clients[cname]["保管料マスタ"].append({
            "種別名": _read_rt(p.get("種別")),
            "単価": _read_num(p.get("単価")) or 0,
            "出力品名": _read_rt(p.get("出力品名")) or "保管料",
        })
    return clients


def seed_clients_if_empty(db_ids, default_clients):
    """
    クライアントマスタが空のとき、初期値（store.DEFAULT_CLIENTS）を投入する。
    既にデータがあれば何もしない。
    """
    rows = _query_all(db_ids["請求_クライアントマスタ"])
    if rows:
        return False
    client = _client()
    cdb = db_ids["請求_クライアントマスタ"]
    pdb = db_ids["請求_単価マスタ"]
    for name, c in default_clients.items():
        h = c["header"]
        client.pages.create(parent={"database_id": cdb}, properties={
            "クライアント名": {"title": _title(name)},
            "略号": {"rich_text": _rt(c.get("略号", ""))},
            "取引先名称": {"rich_text": _rt(h.get("取引先名称", ""))},
            "郵便番号": {"rich_text": _rt(h.get("取引先郵便番号", ""))},
            "都道府県": {"rich_text": _rt(h.get("取引先都道府県", ""))},
            "住所1": {"rich_text": _rt(h.get("取引先住所1", ""))},
            "住所2": {"rich_text": _rt(h.get("取引先住所2", ""))},
            "件名": {"rich_text": _rt(h.get("件名", ""))},
            "備考": {"rich_text": _rt(h.get("備考", ""))},
            "振込先": {"rich_text": _rt(h.get("振込先", ""))},
            "自社担当者": {"rich_text": _rt(h.get("自社担当者氏名", ""))},
        })
        for m in c.get("保管料マスタ", []):
            client.pages.create(parent={"database_id": pdb}, properties={
                "項目名": {"title": _title(f"{name}|保管|{m['種別名']}")},
                "クライアント": {"rich_text": _rt(name)},
                "費目": {"select": {"name": "保管"}},
                "種別": {"rich_text": _rt(m["種別名"])},
                "単価": {"number": m["単価"]},
                "出力品名": {"rich_text": _rt(m["出力品名"])},
            })
        # 出荷作業・資材・受注作業・送料などの単価マスタ
        for m in c.get("単価マスタ", []):
            props = {
                "項目名": {"title": _title(f"{name}|{m['費目']}|{m.get('種別', '')}")},
                "クライアント": {"rich_text": _rt(name)},
                "費目": {"select": {"name": m["費目"]}},
                "種別": {"rich_text": _rt(m.get("種別", ""))},
                "単価": {"number": float(m.get("単価", 0))},
                "出力品名": {"rich_text": _rt(m.get("出力品名", ""))},
            }
            if m.get("マージン率") is not None:
                props["マージン率"] = {"number": float(m["マージン率"])}
            if m.get("加算額") is not None:
                props["加算額"] = {"number": float(m["加算額"])}
            client.pages.create(parent={"database_id": pdb}, properties=props)
    return True


# ============================================================
# 履歴の保存
# ============================================================
def save_issue_history(db_ids, *, invoice_no, client_name, target_ym, kind,
                       issue_date, due_date, subtotal, tax, total, items):
    """発行履歴に1レコード（スナップショット）を追加する。"""
    import datetime
    client = _client()
    client.pages.create(parent={"database_id": db_ids["請求_発行履歴"]}, properties={
        "請求書番号": {"title": _title(invoice_no)},
        "クライアント": {"rich_text": _rt(client_name)},
        "対象年月": {"rich_text": _rt(target_ym)},
        "区分": {"select": {"name": kind}},
        "請求日": {"rich_text": _rt(issue_date)},
        "支払期限": {"rich_text": _rt(due_date)},
        "小計": {"number": int(subtotal)},
        "消費税": {"number": int(tax)},
        "合計金額": {"number": int(total)},
        "品目JSON": {"rich_text": _rt(json.dumps(items, ensure_ascii=False))},
        "発行日時": {"rich_text": _rt(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))},
    })


def save_storage_history(db_ids, *, client_name, target_ym, storage_rows):
    """
    保管内訳履歴に、対象年月の明細を追加する。
    storage_rows: [{"種別名","平均数量","単価","金額","出力品名","15日数量","末日数量"}]
    同一クライアント×対象年月の既存レコードは削除（アーカイブ）してから入れ直す。
    """
    client = _client()
    db = db_ids["請求_保管内訳履歴"]
    # 既存（同一クライアント×対象年月）をアーカイブ
    for row in _query_all(db):
        p = row["properties"]
        if (_read_rt(p.get("クライアント")) == client_name
                and _read_rt(p.get("対象年月")) == target_ym):
            client.pages.update(page_id=row["id"], archived=True)
    # 追加
    for r in storage_rows:
        client.pages.create(parent={"database_id": db}, properties={
            "レコード名": {"title": _title(f"{client_name} {target_ym} {r['種別名']}")},
            "クライアント": {"rich_text": _rt(client_name)},
            "対象年月": {"rich_text": _rt(target_ym)},
            "種別": {"rich_text": _rt(r["種別名"])},
            "15日数量": {"number": float(r.get("15日数量", 0))},
            "末日数量": {"number": float(r.get("末日数量", 0))},
            "平均数量": {"number": float(r.get("平均数量", 0))},
            "単価": {"number": float(r.get("単価", 0))},
            "金額": {"number": int(r.get("金額", 0))},
            "出力品名": {"rich_text": _rt(r.get("出力品名", ""))},
        })


# ============================================================
# 送料方式（クライアント別）の読込・保存
# ============================================================
def load_client_shipping_method(db_ids, client_name):
    """クライアントの送料方式・マージン率・加算額を返す。"""
    for row in _query_all(db_ids["請求_クライアントマスタ"]):
        p = row["properties"]
        if _read_title(p.get("クライアント名")) == client_name:
            return {
                "page_id": row["id"],
                "送料方式": _read_select(p.get("送料方式")) or "実費マージン",
                "送料マージン率": _read_num(p.get("送料マージン率")) or 0,
                "送料加算額": _read_num(p.get("送料加算額")) or 0,
            }
    return {"page_id": None, "送料方式": "実費マージン", "送料マージン率": 0, "送料加算額": 0}


def save_client_shipping_method(db_ids, client_name, method, margin, addon):
    """クライアントの送料方式を保存（既存ページを更新）。"""
    info = load_client_shipping_method(db_ids, client_name)
    if not info["page_id"]:
        raise RuntimeError(f"クライアント '{client_name}' がマスタに見つかりません。")
    _client().pages.update(page_id=info["page_id"], properties={
        "送料方式": {"select": {"name": method}},
        "送料マージン率": {"number": float(margin or 0)},
        "送料加算額": {"number": float(addon or 0)},
    })


# ============================================================
# 地域マスタ（都道府県 -> エリア）の読込・保存・シード
# ============================================================
def seed_area_map_if_empty(db_ids, default_map):
    rows = _query_all(db_ids["請求_地域マスタ"])
    if rows:
        return False
    client = _client()
    db = db_ids["請求_地域マスタ"]
    for pref, area in default_map.items():
        client.pages.create(parent={"database_id": db}, properties={
            "都道府県": {"title": _title(pref)},
            "エリア": {"rich_text": _rt(area)},
        })
    return True


def load_area_map(db_ids):
    """{都道府県: エリア} を返す。"""
    result = {}
    for row in _query_all(db_ids["請求_地域マスタ"]):
        p = row["properties"]
        pref = _read_title(p.get("都道府県"))
        if pref:
            result[pref] = _read_rt(p.get("エリア"))
    return result


# ============================================================
# 送料表（サイズ×地域マトリクス）の読込・保存・シード
# ============================================================
def seed_shipping_table_if_empty(db_ids, client_name, default_table, areas):
    rows = [r for r in _query_all(db_ids["請求_送料表"])
            if _read_rt(r["properties"].get("クライアント")) == client_name]
    if rows:
        return False
    client = _client()
    db = db_ids["請求_送料表"]
    for row in default_table:
        props = {
            "行キー": {"title": _title(
                f"{client_name}|{row['配送業者']}|{row['配送区分']}|{row['サイズ']}")},
            "クライアント": {"rich_text": _rt(client_name)},
            "配送業者": {"rich_text": _rt(row["配送業者"])},
            "配送区分": {"rich_text": _rt(row["配送区分"])},
            "サイズ": {"rich_text": _rt(row["サイズ"])},
        }
        for area in areas:
            props[area] = {"number": float(row.get(area, 0) or 0)}
        client.pages.create(parent={"database_id": db}, properties=props)
    return True


def load_shipping_table(db_ids, client_name, areas):
    """送料表を [{配送業者,配送区分,サイズ, 各エリア:運賃}] で返す。"""
    rows = []
    for row in _query_all(db_ids["請求_送料表"]):
        p = row["properties"]
        if _read_rt(p.get("クライアント")) != client_name:
            continue
        rec = {
            "配送業者": _read_rt(p.get("配送業者")),
            "配送区分": _read_rt(p.get("配送区分")),
            "サイズ": _read_rt(p.get("サイズ")),
        }
        for area in areas:
            rec[area] = _read_num(p.get(area)) or 0
        rows.append(rec)
    return rows


def replace_shipping_table(db_ids, client_name, rows, areas):
    """送料表をクライアント単位で置き換える（既存アーカイブ→新規作成）。"""
    client = _client()
    db = db_ids["請求_送料表"]
    for row in _query_all(db):
        p = row["properties"]
        if _read_rt(p.get("クライアント")) == client_name:
            client.pages.update(page_id=row["id"], archived=True)
    saved = 0
    for r in rows:
        size = str(r.get("サイズ", "")).strip()
        if not size:
            continue
        carrier = str(r.get("配送業者", "")).strip()
        kubun = str(r.get("配送区分", "")).strip()
        props = {
            "行キー": {"title": _title(f"{client_name}|{carrier}|{kubun}|{size}")},
            "クライアント": {"rich_text": _rt(client_name)},
            "配送業者": {"rich_text": _rt(carrier)},
            "配送区分": {"rich_text": _rt(kubun)},
            "サイズ": {"rich_text": _rt(size)},
        }
        for area in areas:
            val = r.get(area)
            props[area] = {"number": float(val) if val not in (None, "") else 0}
        client.pages.create(parent={"database_id": db}, properties=props)
        saved += 1
    return saved


# ============================================================
# 単価マスタの読込・保存（クライアント別）
# ============================================================
def load_price_master(db_ids, client_name):
    """
    指定クライアントの単価マスタ全行を返す。
    [{"費目","種別","単価","出力品名","マージン率","加算額","備考"}]
    """
    rows = []
    for row in _query_all(db_ids["請求_単価マスタ"]):
        p = row["properties"]
        if _read_rt(p.get("クライアント")) != client_name:
            continue
        rows.append({
            "費目": _read_select(p.get("費目")),
            "種別": _read_rt(p.get("種別")),
            "単価": _read_num(p.get("単価")) or 0,
            "出力品名": _read_rt(p.get("出力品名")),
            "マージン率": _read_num(p.get("マージン率")),
            "加算額": _read_num(p.get("加算額")),
            "備考": _read_rt(p.get("備考")),
        })
    return rows


def replace_price_master(db_ids, client_name, rows):
    """
    指定クライアントの単価マスタを丸ごと置き換える（既存をアーカイブ→新規作成）。
    rows: load_price_master と同じ形式のリスト。費目・種別が空の行は無視。
    """
    client = _client()
    db = db_ids["請求_単価マスタ"]
    # 既存をアーカイブ
    for row in _query_all(db):
        p = row["properties"]
        if _read_rt(p.get("クライアント")) == client_name:
            client.pages.update(page_id=row["id"], archived=True)
    # 新規作成
    saved = 0
    for r in rows:
        himoku = str(r.get("費目", "")).strip()
        shubetsu = str(r.get("種別", "")).strip()
        if not himoku and not shubetsu:
            continue
        props = {
            "項目名": {"title": _title(f"{client_name}|{himoku}|{shubetsu}")},
            "クライアント": {"rich_text": _rt(client_name)},
            "種別": {"rich_text": _rt(shubetsu)},
            "単価": {"number": float(r.get("単価") or 0)},
            "出力品名": {"rich_text": _rt(r.get("出力品名", ""))},
            "備考": {"rich_text": _rt(r.get("備考", ""))},
        }
        if himoku:
            props["費目"] = {"select": {"name": himoku}}
        margin = r.get("マージン率")
        if margin is not None and str(margin) != "":
            props["マージン率"] = {"number": float(margin)}
        addon = r.get("加算額")
        if addon is not None and str(addon) != "":
            props["加算額"] = {"number": float(addon)}
        client.pages.create(parent={"database_id": db}, properties=props)
        saved += 1
    return saved


def replace_price_rows(db_ids, client_name, himoku_set, rows):
    """
    指定クライアントの、特定費目（himoku_set）の単価行だけを置き換える。
    他費目（保管・送料など）の行は残す。
    rows: [{"費目","種別","単価","出力品名"}]
    """
    client = _client()
    db = db_ids["請求_単価マスタ"]
    for row in _query_all(db):
        p = row["properties"]
        if (_read_rt(p.get("クライアント")) == client_name
                and _read_select(p.get("費目")) in himoku_set):
            client.pages.update(page_id=row["id"], archived=True)
    saved = 0
    for r in rows:
        himoku = str(r.get("費目", "")).strip()
        shubetsu = str(r.get("種別", "")).strip()
        if himoku not in himoku_set or not shubetsu:
            continue
        client.pages.create(parent={"database_id": db}, properties={
            "項目名": {"title": _title(f"{client_name}|{himoku}|{shubetsu}")},
            "クライアント": {"rich_text": _rt(client_name)},
            "費目": {"select": {"name": himoku}},
            "種別": {"rich_text": _rt(shubetsu)},
            "単価": {"number": float(r.get("単価") or 0)},
            "出力品名": {"rich_text": _rt(r.get("出力品名", ""))},
        })
        saved += 1
    return saved


# ============================================================
# イレギュラー作業（[汎用]作業料の元データ）
# ============================================================
def load_irregular_work(db_ids, client_name, target_ym=None):
    """
    指定クライアント（任意で対象年月）のイレギュラー作業を返す。
    [{日付,時間数,人数,合計時間,作業項目,作業詳細,備考}]
    """
    rows = []
    for row in _query_all(db_ids["請求_イレギュラー作業"]):
        p = row["properties"]
        if _read_rt(p.get("クライアント")) != client_name:
            continue
        # 月は実日付から判定（対象年月という保存項目は廃止）
        ym = _ym_from_date(_read_rt(p.get("日付")), "")
        if target_ym and ym != target_ym:
            continue
        rows.append({
            "id": row["id"],
            "対象年月": ym,  # 日付から計算した請求対象月（保存はしない）
            "日付": _read_rt(p.get("日付")),
            "時間数": _read_num(p.get("時間数")) or 0,
            "人数": _read_num(p.get("人数")) or 0,
            "合計時間": _read_num(p.get("合計時間")) or 0,
            "作業項目": _read_rt(p.get("作業項目")),
            "作業詳細": _read_rt(p.get("作業詳細")),
            "備考": _read_rt(p.get("備考")),
        })
    # 日付順に並べる
    rows.sort(key=lambda r: r["日付"])
    return rows


def _ym_from_date(date_str, fallback):
    """ '2026/03/12'・'2026-3-12' 等から '2026-03' を作る。失敗時はfallback。"""
    import re
    m = re.match(r"\s*(\d{4})\D+(\d{1,2})", str(date_str))
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
    return fallback


def _irregular_props(client_name, r, fallback_ym):
    """イレギュラー作業1行のNotionプロパティを組み立てる。"""
    date = str(r.get("日付", "")).strip()
    item = str(r.get("作業項目", "")).strip()
    hours = float(r.get("時間数") or 0)
    people = float(r.get("人数") or 0)
    return {
        "レコード名": {"title": _title(f"{client_name} {date} {item}")},
        "クライアント": {"rich_text": _rt(client_name)},
        "日付": {"rich_text": _rt(date)},
        "時間数": {"number": hours},
        "人数": {"number": people},
        "合計時間": {"number": hours * people},
        "作業項目": {"rich_text": _rt(item)},
        "作業詳細": {"rich_text": _rt(r.get("作業詳細", ""))},
        "備考": {"rich_text": _rt(r.get("備考", ""))},
    }


def add_irregular_work(db_ids, client_name, row):
    """
    イレギュラー作業を1件だけ追加（新規作成のみ）。
    過去レコードには一切触れないため、現場の日々入力でも誤操作で
    過去分を壊す心配がない。
    """
    _client().pages.create(
        parent={"database_id": db_ids["請求_イレギュラー作業"]},
        properties=_irregular_props(client_name, row, ""))


def save_irregular_work(db_ids, client_name, edited_rows, loaded_ids, fallback_ym):
    """
    差分保存。
      - id付き行 → 更新
      - id無し行 → 新規作成
      - loaded_ids のうち今回の行に無いid → アーカイブ（削除）
    表示中に読み込んだ範囲(loaded_ids)だけを対象にするため、範囲外の月は触らない。
    返り値: dict(created, updated, deleted)
    """
    client = _client()
    db = db_ids["請求_イレギュラー作業"]
    kept_ids = set()
    created = updated = deleted = 0

    for r in edited_rows:
        date = str(r.get("日付", "")).strip()
        item = str(r.get("作業項目", "")).strip()
        hours = float(r.get("時間数") or 0)
        if not date and not item and hours == 0:
            continue
        props = _irregular_props(client_name, r, fallback_ym)
        rid = r.get("id")
        rid = str(rid).strip() if rid is not None else ""
        if rid and rid.lower() != "nan":
            client.pages.update(page_id=rid, properties=props, archived=False)
            kept_ids.add(rid)
            updated += 1
        else:
            client.pages.create(parent={"database_id": db}, properties=props)
            created += 1

    for rid in set(loaded_ids) - kept_ids:
        try:
            client.pages.update(page_id=rid, archived=True)
            deleted += 1
        except Exception:  # noqa: BLE001
            pass

    return {"created": created, "updated": updated, "deleted": deleted}


# ============================================================
# 保管カウント（明細行ベース：種別×ロケーション×数量／第1期・第2期）
# ============================================================
def _period_from_date(date_str):
    """カウント日の日付から期を推定（20日以前=第1期、以降=第2期）。"""
    import re
    m = re.search(r"\D(\d{1,2})\s*$", "/" + str(date_str).strip())
    if m:
        return "第1期" if int(m.group(1)) <= 20 else "第2期"
    return "第1期"


def _storage_props(client_name, r, ym):
    date = str(r.get("カウント日", "")).strip()
    period = str(r.get("期", "")).strip() or _period_from_date(date)
    shubetsu = str(r.get("種別", "")).strip()
    area = str(r.get("エリア", "")).strip()
    loc = str(r.get("ロケーション", "")).strip()
    return {
        "レコード名": {"title": _title(f"{client_name} {ym} {period} {area} {shubetsu} {loc}")},
        "クライアント": {"rich_text": _rt(client_name)},
        "対象年月": {"rich_text": _rt(ym)},
        "期": {"select": {"name": period}},
        "カウント日": {"rich_text": _rt(date)},
        "種別": {"rich_text": _rt(shubetsu)},
        "エリア": {"rich_text": _rt(area)},
        "ロケーション": {"rich_text": _rt(loc)},
        "数量": {"number": float(r.get("数量") or 0)},
        "備考": {"rich_text": _rt(r.get("備考", ""))},
    }


def load_storage_counts(db_ids, client_name, target_ym):
    """指定クライアント×対象月の保管カウント明細を返す（id付き）。"""
    rows = []
    for row in _query_all(db_ids["請求_保管カウント"]):
        p = row["properties"]
        if _read_rt(p.get("クライアント")) != client_name:
            continue
        if _read_rt(p.get("対象年月")) != target_ym:
            continue
        rows.append({
            "id": row["id"],
            "期": _read_select(p.get("期")),
            "カウント日": _read_rt(p.get("カウント日")),
            "種別": _read_rt(p.get("種別")),
            "エリア": _read_rt(p.get("エリア")),
            "ロケーション": _read_rt(p.get("ロケーション")),
            "数量": _read_num(p.get("数量")) or 0,
            "備考": _read_rt(p.get("備考")),
        })
    rows.sort(key=lambda r: (r["カウント日"], r["種別"], r["ロケーション"]))
    return rows


def add_storage_count(db_ids, client_name, row, target_ym):
    """保管カウントを1件だけ追加（過去に触れない）。"""
    _client().pages.create(
        parent={"database_id": db_ids["請求_保管カウント"]},
        properties=_storage_props(client_name, row, target_ym))


def save_storage_counts(db_ids, client_name, edited_rows, loaded_ids, target_ym):
    """保管カウントの差分保存（id付き=更新/無し=新規/読込済で消えた=削除）。"""
    client = _client()
    db = db_ids["請求_保管カウント"]
    kept, created, updated, deleted = set(), 0, 0, 0
    for r in edited_rows:
        if not str(r.get("種別", "")).strip() and float(r.get("数量") or 0) == 0:
            continue
        props = _storage_props(client_name, r, target_ym)
        rid = str(r.get("id") or "").strip()
        if rid and rid.lower() != "nan":
            client.pages.update(page_id=rid, properties=props, archived=False)
            kept.add(rid)
            updated += 1
        else:
            client.pages.create(parent={"database_id": db}, properties=props)
            created += 1
    for rid in set(loaded_ids) - kept:
        try:
            client.pages.update(page_id=rid, archived=True)
            deleted += 1
        except Exception:  # noqa: BLE001
            pass
    return {"created": created, "updated": updated, "deleted": deleted}


def aggregate_storage(count_rows, master_price, master_out):
    """
    明細行から種別ごとの 第1期合計・第2期合計・平均・金額 を集計する。
    返り値: (preview[list], by_out[dict 出力品名->金額], warnings[list])
    """
    agg = {}
    for r in count_rows:
        name = str(r.get("種別", "")).strip()
        if not name:
            continue
        a = agg.setdefault(name, {"第1期": 0.0, "第2期": 0.0})
        q = float(r.get("数量") or 0)
        if str(r.get("期", "")) == "第2期":
            a["第2期"] += q
        else:
            a["第1期"] += q
    preview, by_out, warnings = [], {}, []
    for name, a in agg.items():
        avg = (a["第1期"] + a["第2期"]) / 2
        if name in master_price:
            price = float(master_price[name])
        else:
            price = 0.0
            warnings.append(f"種別 '{name}' の単価が保管料マスタに未登録")
        out = master_out.get(name, "保管料")
        amt = round(avg * price)
        preview.append({"種別": name, "第1期合計": a["第1期"], "第2期合計": a["第2期"],
                        "平均": avg, "単価": price, "金額": amt, "出力品名": out})
        by_out[out] = by_out.get(out, 0) + amt
    return preview, by_out, warnings


def load_storage_history(db_ids, client_name, target_ym):
    """指定クライアント×対象年月の保管内訳履歴を返す（無ければ空リスト）。"""
    rows = []
    for row in _query_all(db_ids["請求_保管内訳履歴"]):
        p = row["properties"]
        if (_read_rt(p.get("クライアント")) == client_name
                and _read_rt(p.get("対象年月")) == target_ym):
            rows.append({
                "種別名": _read_rt(p.get("種別")),
                "15日数量": _read_num(p.get("15日数量")) or 0,
                "末日数量": _read_num(p.get("末日数量")) or 0,
                "単価": _read_num(p.get("単価")) or 0,
                "出力品名": _read_rt(p.get("出力品名")),
            })
    return rows
