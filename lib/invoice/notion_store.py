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
        else:
            created = client.databases.create(
                parent={"type": "page_id", "page_id": parent},
                title=[{"type": "text", "text": {"content": title}}],
                properties=props,
            )
            result[title] = created["id"]
    return result


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
