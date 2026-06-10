# -*- coding: utf-8 -*-
"""
買掛・請求書処理システムのNotion永続化。

既存の請求書発行(lib/invoice/notion_store)と同じ親ページ
(Secrets: INVOICE_NOTION_PARENT_PAGE_ID)配下に、「支払_」接頭の専用DBを
冪等生成する。既存DB(請求_*)とはタイトルで分離するため干渉しない。
"""
import json

# スキーマ(列)を変更したらこの版数を上げる。app_initがセッションキャッシュを
# 無視して ensure_databases(不足列の自動追加) を再実行する。
SCHEMA_VERSION = "2026-06-09c"

DB_SCHEMAS = {
    "支払_取引先マスタ": {
        "会社名": {"title": {}},
        "別名": {"rich_text": {}},
        "NE仕入先cd": {"rich_text": {}},
        "科目": {"rich_text": {}},
        "支払方法": {"rich_text": {}},
        "支払日": {"rich_text": {}},
        "銀行": {"rich_text": {}},
        "支店": {"rich_text": {}},
        "銀行番号": {"rich_text": {}},
        "支店番号": {"rich_text": {}},
        "支払元銀行": {"rich_text": {}},
        "預金種目": {"select": {"options": [
            {"name": "普通", "color": "blue"},
            {"name": "当座", "color": "green"},
        ]}},
        "口座番号": {"rich_text": {}},
        "受取人口座名": {"rich_text": {}},
        "顧客番号": {"rich_text": {}},
        "固定額": {"number": {}},
        "除外フラグ": {"checkbox": {}},
        "備考": {"rich_text": {}},
    },
    "支払_請求書": {
        "レコード名": {"title": {}},
        "会社名": {"rich_text": {}},
        "当月請求額": {"number": {}},
        "今回請求額": {"number": {}},
        "前月繰越額": {"number": {}},
        "消費税額": {"number": {}},
        "税内訳": {"rich_text": {}},
        "軽減税率": {"checkbox": {}},
        "請求日": {"rich_text": {}},
        "支払期日": {"rich_text": {}},
        "カテゴリ": {"select": {"options": [
            {"name": "WEB発行", "color": "blue"},
            {"name": "郵送", "color": "green"},
            {"name": "前払い", "color": "orange"},
        ]}},
        "抽出_銀行": {"rich_text": {}},
        "抽出_支店": {"rich_text": {}},
        "抽出_預金種目": {"rich_text": {}},
        "抽出_口座番号": {"rich_text": {}},
        "抽出_口座名義": {"rich_text": {}},
        "口座相違フラグ": {"checkbox": {}},
        "ステータス": {"select": {"options": [
            {"name": "読取済", "color": "gray"},
            {"name": "確認済", "color": "blue"},
            {"name": "突合OK", "color": "green"},
            {"name": "確定", "color": "purple"},
        ]}},
        "突合状態": {"select": {"options": [
            {"name": "未突合", "color": "gray"},
            {"name": "一致", "color": "green"},
            {"name": "金額不一致", "color": "red"},
            {"name": "発注なし", "color": "orange"},
            {"name": "マスタ未登録", "color": "yellow"},
        ]}},
        "NE合算額": {"number": {}},
        "NE送料": {"number": {}},
        "差額": {"number": {}},
        "NE発注番号": {"rich_text": {}},
        "対象月": {"rich_text": {}},
        "ファイルリンク": {"rich_text": {}},
        "抽出メモ": {"rich_text": {}},
        "登録日時": {"rich_text": {}},
    },
    "支払_振込履歴": {
        "レコード名": {"title": {}},
        "実行日": {"rich_text": {}},
        "対象月": {"rich_text": {}},
        "件数": {"number": {}},
        "合計金額": {"number": {}},
        "明細JSON": {"rich_text": {}},
        "生成日時": {"rich_text": {}},
    },
}


# ---- 低レベルヘルパ(invoice/notion_storeと同方針) ----
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


def _read_select(prop):
    sel = prop.get("select") if prop else None
    return sel["name"] if sel else ""


def _read_check(prop):
    return bool(prop.get("checkbox")) if prop else False


def ensure_databases():
    """親ページ配下に支払_*のDBを冪等生成し {タイトル: id} を返す。"""
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
# 取引先マスタ
# ============================================================
MASTER_FIELDS = ["会社名", "別名", "NE仕入先cd", "科目", "支払方法", "支払日", "銀行",
                 "支店", "銀行番号", "支店番号", "預金種目", "口座番号", "受取人口座名",
                 "顧客番号", "固定額", "除外フラグ", "支払元銀行", "備考"]


def load_master(db_ids):
    """取引先マスタを辞書リストで返す(payable_master_seed.csvと同じ列)。"""
    rows = []
    for row in _query_all(db_ids["支払_取引先マスタ"]):
        p = row["properties"]
        rows.append({
            "id": row["id"],
            "会社名": _read_title(p.get("会社名")),
            "別名": _read_rt(p.get("別名")),
            "NE仕入先cd": _read_rt(p.get("NE仕入先cd")),
            "科目": _read_rt(p.get("科目")),
            "支払方法": _read_rt(p.get("支払方法")),
            "支払日": _read_rt(p.get("支払日")),
            "銀行": _read_rt(p.get("銀行")),
            "支店": _read_rt(p.get("支店")),
            "銀行番号": _read_rt(p.get("銀行番号")),
            "支店番号": _read_rt(p.get("支店番号")),
            "支払元銀行": _read_rt(p.get("支払元銀行")),
            "預金種目": _read_select(p.get("預金種目")),
            "口座番号": _read_rt(p.get("口座番号")),
            "受取人口座名": _read_rt(p.get("受取人口座名")),
            "顧客番号": _read_rt(p.get("顧客番号")),
            "固定額": _read_num(p.get("固定額")) or "",
            "除外フラグ": "✓" if _read_check(p.get("除外フラグ")) else "",
            "備考": _read_rt(p.get("備考")),
        })
    rows.sort(key=lambda r: r["会社名"])
    return rows


def _master_props(r):
    def num(v):
        try:
            return float(str(v).replace(",", "")) if str(v).strip() != "" else None
        except (ValueError, TypeError):
            return None
    return {
        "会社名": {"title": _title(r.get("会社名", ""))},
        "別名": {"rich_text": _rt(r.get("別名", ""))},
        "NE仕入先cd": {"rich_text": _rt(r.get("NE仕入先cd", ""))},
        "科目": {"rich_text": _rt(r.get("科目", ""))},
        "支払方法": {"rich_text": _rt(r.get("支払方法", ""))},
        "支払日": {"rich_text": _rt(r.get("支払日", ""))},
        "銀行": {"rich_text": _rt(r.get("銀行", ""))},
        "支店": {"rich_text": _rt(r.get("支店", ""))},
        "銀行番号": {"rich_text": _rt(r.get("銀行番号", ""))},
        "支店番号": {"rich_text": _rt(r.get("支店番号", ""))},
        "支払元銀行": {"rich_text": _rt(r.get("支払元銀行", ""))},
        "預金種目": ({"select": {"name": r["預金種目"]}}
                  if str(r.get("預金種目", "")).strip() in ("普通", "当座") else {"select": None}),
        "口座番号": {"rich_text": _rt(r.get("口座番号", ""))},
        "受取人口座名": {"rich_text": _rt(r.get("受取人口座名", ""))},
        "顧客番号": {"rich_text": _rt(r.get("顧客番号", ""))},
        "固定額": {"number": num(r.get("固定額", ""))},
        "除外フラグ": {"checkbox": str(r.get("除外フラグ", "")).strip() in ("✓", "1", "True", "true", "○")},
        "備考": {"rich_text": _rt(r.get("備考", ""))},
    }


def seed_master_if_empty(db_ids, seed_rows):
    """マスタが空のときだけ seed_rows(辞書リスト) を投入する。"""
    if _query_all(db_ids["支払_取引先マスタ"]):
        return 0
    return _seed_create(db_ids, seed_rows)


def seed_master_missing(db_ids, seed_rows):
    """
    既に存在する会社名はスキップし、未登録の会社名だけを追加する(冪等)。
    二重seedによる重複を防ぐため、空判定ではなく会社名集合で判定する。
    """
    existing = set()
    for row in _query_all(db_ids["支払_取引先マスタ"]):
        nm = _read_title(row["properties"].get("会社名")).strip()
        if nm:
            existing.add(nm)
    rows = [r for r in seed_rows if str(r.get("会社名", "")).strip() not in existing]
    return _seed_create(db_ids, rows)


def _seed_create(db_ids, seed_rows):
    client = _client()
    db = db_ids["支払_取引先マスタ"]
    n = 0
    for r in seed_rows:
        if not str(r.get("会社名", "")).strip():
            continue
        client.pages.create(parent={"database_id": db}, properties=_master_props(r))
        n += 1
    return n


# マージ対象のデータ列(id/表示用の除外フラグ表記を除く実データ)
_MERGE_FIELDS = ["別名", "NE仕入先cd", "科目", "支払方法", "支払日", "銀行", "支店",
                 "銀行番号", "支店番号", "預金種目", "口座番号", "受取人口座名",
                 "顧客番号", "固定額", "除外フラグ", "支払元銀行", "備考"]


def dedupe_master(db_ids):
    """
    会社名が重複したレコードを整理する。
      - 全項目が同一 → 1件に統合(他はアーカイブ)
      - 差分はあるが、各項目で非空値が1種類だけ(項目かぶりなし) → 結合して1件に
      - いずれかの項目で非空値が2種類以上(どちらが正か不明) → そのグループは残す
    戻り値: {"統合":n, "結合":n, "競合保留":n, "削除":n, "詳細":[...]}
    """
    client = _client()
    rows = load_master(db_ids)
    groups = {}
    for r in rows:
        key = r["会社名"].strip()
        if key:
            groups.setdefault(key, []).append(r)

    report = {"統合": 0, "結合": 0, "競合保留": 0, "削除": 0, "詳細": []}
    for name, recs in groups.items():
        if len(recs) < 2:
            continue
        # 各項目の非空値の集合
        conflict = False
        merged = {"会社名": name}
        for f in _MERGE_FIELDS:
            vals = []
            for r in recs:
                v = str(r.get(f, "")).strip()
                if v and v not in vals:
                    vals.append(v)
            if len(vals) >= 2:
                conflict = True
                break
            merged[f] = vals[0] if vals else ""
        if conflict:
            report["競合保留"] += 1
            report["詳細"].append(f"競合保留: {name}（{len(recs)}件、項目に複数値あり）")
            continue
        # 全同一か(結合不要か)の判定
        identical = all(
            all(str(r.get(f, "")).strip() == merged[f] for f in _MERGE_FIELDS)
            for r in recs)
        keep = recs[0]
        merged["id"] = keep["id"]
        upsert_master_row(db_ids, merged)
        for r in recs[1:]:
            client.pages.update(page_id=r["id"], archived=True)
            report["削除"] += 1
        if identical:
            report["統合"] += 1
            report["詳細"].append(f"統合: {name}（{len(recs)}件→1件）")
        else:
            report["結合"] += 1
            report["詳細"].append(f"結合: {name}（{len(recs)}件→1件、項目を結合）")
    return report


def enrich_bank_names(db_ids):
    """
    既存マスタの 銀行番号/支店番号 から 銀行名/支店名 を補完する。
    既存の『銀行』列が金融機関名と一致しない値(弊社の支払元=楽天等)なら、
    空の『支払元銀行』へ退避してから 銀行=受取人銀行名 に置き換える。
    戻り値: {"更新":n, "詳細":[...]}
    """
    from . import bank_master as BM
    client = _client()
    rows = load_master(db_ids)
    updated, detail = 0, []
    for r in rows:
        bank_no = (r.get("銀行番号") or "").strip()
        branch_no = (r.get("支店番号") or "").strip()
        new_bank = BM.bank_name(bank_no) if bank_no else ""
        new_branch = BM.branch_name(bank_no, branch_no) if (bank_no and branch_no) else ""
        cur_bank = (r.get("銀行") or "").strip()
        cur_branch = (r.get("支店") or "").strip()
        cur_moto = (r.get("支払元銀行") or "").strip()
        props = {}
        # 元の『銀行』が受取人銀行名でない(=弊社の支払元:楽天等)なら支払元へ退避。
        # 番号が無く受取人名を解決できない社でも、楽天等を支払元へ移して銀行は空に。
        if cur_bank and cur_bank != new_bank and not cur_moto:
            props["支払元銀行"] = {"rich_text": _rt(cur_bank)}
        if cur_bank != new_bank:
            props["銀行"] = {"rich_text": _rt(new_bank)}
        if new_branch and cur_branch != new_branch:
            props["支店"] = {"rich_text": _rt(new_branch)}
        if props:
            client.pages.update(page_id=r["id"], properties=props)
            updated += 1
            moved = cur_bank if "支払元銀行" in props else cur_moto
            detail.append(f"{r['会社名']}: 銀行『{new_bank or '(空)'}』 支店『{new_branch}』 支払元『{moved}』")
    return {"更新": updated, "詳細": detail}


def add_alias_by_company(db_ids, company, alias):
    """
    会社名(company)のマスタ行に別名(alias)を追記する(学習用)。
    既に会社名・別名に含まれていれば何もしない。追記したらTrue。
    """
    import re
    from .matching import normalize_name
    alias = str(alias).strip()
    if not alias:
        return False
    target = normalize_name(company)
    for row in _query_all(db_ids["支払_取引先マスタ"]):
        p = row["properties"]
        name = _read_title(p.get("会社名"))
        if normalize_name(name) != target:
            continue
        cur_alias = _read_rt(p.get("別名"))
        existing = {normalize_name(name)}
        for a in re.split(r"[;,、/／]", cur_alias or ""):
            if a.strip():
                existing.add(normalize_name(a))
        if normalize_name(alias) in existing:
            return False
        new_alias = (cur_alias + ";" + alias) if cur_alias.strip() else alias
        _client().pages.update(page_id=row["id"], properties={
            "別名": {"rich_text": _rt(new_alias)}})
        return True
    return False


def upsert_master_row(db_ids, r):
    """マスタ1行を保存。idがあれば更新、無ければ新規。"""
    client = _client()
    rid = str(r.get("id") or "").strip()
    props = _master_props(r)
    if rid and rid.lower() != "nan":
        client.pages.update(page_id=rid, properties=props)
    else:
        client.pages.create(parent={"database_id": db_ids["支払_取引先マスタ"]}, properties=props)


# ============================================================
# 請求書レコード
# ============================================================
def save_invoice(db_ids, data):
    """
    抽出結果+突合結果を 支払_請求書 に1件作成する。
    data: 会社名/当月請求額/今回請求額/前月繰越額/消費税額/請求日/支払期日/
          カテゴリ/抽出_*/口座相違フラグ/ステータス/突合状態/NE合算額/差額/
          対象月/ファイルリンク/抽出メモ
    """
    import datetime
    client = _client()
    company = data.get("会社名", "")
    ym = data.get("対象月", "")
    props = {
        "レコード名": {"title": _title(f"{ym} {company}".strip())},
        "会社名": {"rich_text": _rt(company)},
        "当月請求額": {"number": _to_num(data.get("当月請求額"))},
        "今回請求額": {"number": _to_num(data.get("今回請求額"))},
        "前月繰越額": {"number": _to_num(data.get("前月繰越額"))},
        "消費税額": {"number": _to_num(data.get("消費税額"))},
        "税内訳": {"rich_text": _rt(data.get("税内訳", ""))},
        "軽減税率": {"checkbox": bool(data.get("軽減税率"))},
        "請求日": {"rich_text": _rt(data.get("請求日", ""))},
        "支払期日": {"rich_text": _rt(data.get("支払期日", ""))},
        "抽出_銀行": {"rich_text": _rt(data.get("抽出_銀行", ""))},
        "抽出_支店": {"rich_text": _rt(data.get("抽出_支店", ""))},
        "抽出_預金種目": {"rich_text": _rt(data.get("抽出_預金種目", ""))},
        "抽出_口座番号": {"rich_text": _rt(data.get("抽出_口座番号", ""))},
        "抽出_口座名義": {"rich_text": _rt(data.get("抽出_口座名義", ""))},
        "口座相違フラグ": {"checkbox": bool(data.get("口座相違フラグ"))},
        "NE合算額": {"number": _to_num(data.get("NE合算額"))},
        "差額": {"number": _to_num(data.get("差額"))},
        "対象月": {"rich_text": _rt(ym)},
        "ファイルリンク": {"rich_text": _rt(data.get("ファイルリンク", ""))},
        "抽出メモ": {"rich_text": _rt(data.get("抽出メモ", ""))},
        "登録日時": {"rich_text": _rt(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))},
    }
    cat = str(data.get("カテゴリ", "")).strip()
    if cat in ("WEB発行", "郵送", "前払い"):
        props["カテゴリ"] = {"select": {"name": cat}}
    props["ステータス"] = {"select": {"name": data.get("ステータス", "読取済")}}
    props["突合状態"] = {"select": {"name": data.get("突合状態", "未突合")}}
    client.pages.create(parent={"database_id": db_ids["支払_請求書"]}, properties=props)


def load_invoices(db_ids, target_ym=None, status=None):
    rows = []
    for row in _query_all(db_ids["支払_請求書"]):
        p = row["properties"]
        ym = _read_rt(p.get("対象月"))
        if target_ym and ym != target_ym:
            continue
        st_ = _read_select(p.get("ステータス"))
        if status and st_ != status:
            continue
        rows.append({
            "id": row["id"],
            "会社名": _read_rt(p.get("会社名")),
            "当月請求額": _read_num(p.get("当月請求額")) or 0,
            "今回請求額": _read_num(p.get("今回請求額")) or 0,
            "前月繰越額": _read_num(p.get("前月繰越額")) or 0,
            "税内訳": _read_rt(p.get("税内訳")),
            "軽減税率": _read_check(p.get("軽減税率")),
            "請求日": _read_rt(p.get("請求日")),
            "支払期日": _read_rt(p.get("支払期日")),
            "カテゴリ": _read_select(p.get("カテゴリ")),
            "抽出_銀行": _read_rt(p.get("抽出_銀行")),
            "抽出_支店": _read_rt(p.get("抽出_支店")),
            "抽出_預金種目": _read_rt(p.get("抽出_預金種目")),
            "抽出_口座番号": _read_rt(p.get("抽出_口座番号")),
            "抽出_口座名義": _read_rt(p.get("抽出_口座名義")),
            "口座相違フラグ": _read_check(p.get("口座相違フラグ")),
            "ステータス": st_,
            "突合状態": _read_select(p.get("突合状態")),
            "NE合算額": _read_num(p.get("NE合算額")),
            "NE送料": _read_num(p.get("NE送料")),
            "差額": _read_num(p.get("差額")),
            "NE発注番号": _read_rt(p.get("NE発注番号")),
            "対象月": ym,
            "ファイルリンク": _read_rt(p.get("ファイルリンク")),
            "抽出メモ": _read_rt(p.get("抽出メモ")),
        })
    rows.sort(key=lambda r: (r["突合状態"], r["会社名"]))
    return rows


def update_invoice_fields(db_ids, page_id, **fields):
    """請求書レコードの一部プロパティを更新する。"""
    client = _client()
    props = {}
    for k, v in fields.items():
        if k in ("ステータス", "突合状態", "カテゴリ"):
            props[k] = {"select": {"name": v}} if v else {"select": None}
        elif k in ("当月請求額", "今回請求額", "NE合算額", "NE送料", "差額", "前月繰越額"):
            props[k] = {"number": _to_num(v)}
        elif k == "口座相違フラグ":
            props[k] = {"checkbox": bool(v)}
        else:
            props[k] = {"rich_text": _rt(v)}
    if props:
        client.pages.update(page_id=page_id, properties=props)


def delete_invoice(db_ids, page_id):
    _client().pages.update(page_id=page_id, archived=True)


# ============================================================
# 振込履歴
# ============================================================
def save_transfer_history(db_ids, *, 実行日, 対象月, records):
    """生成した振込CSVの内容スナップショットを保存する。"""
    import datetime
    client = _client()
    total = sum(int(r.get("金額", 0)) for r in records)
    detail = [{"会社名": r.get("会社名", ""), "金額": int(r.get("金額", 0))} for r in records]
    client.pages.create(parent={"database_id": db_ids["支払_振込履歴"]}, properties={
        "レコード名": {"title": _title(f"{対象月} 振込 {len(records)}件")},
        "実行日": {"rich_text": _rt(実行日)},
        "対象月": {"rich_text": _rt(対象月)},
        "件数": {"number": len(records)},
        "合計金額": {"number": total},
        "明細JSON": {"rich_text": _rt(json.dumps(detail, ensure_ascii=False))},
        "生成日時": {"rich_text": _rt(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))},
    })


def load_transfer_history(db_ids):
    rows = []
    for row in _query_all(db_ids["支払_振込履歴"]):
        p = row["properties"]
        try:
            detail = json.loads(_read_rt(p.get("明細JSON")) or "[]")
        except Exception:  # noqa: BLE001
            detail = []
        rows.append({
            "id": row["id"],
            "実行日": _read_rt(p.get("実行日")),
            "対象月": _read_rt(p.get("対象月")),
            "件数": _read_num(p.get("件数")) or 0,
            "合計金額": _read_num(p.get("合計金額")) or 0,
            "明細": detail,
            "生成日時": _read_rt(p.get("生成日時")),
        })
    rows.sort(key=lambda r: r["生成日時"], reverse=True)
    return rows


def _to_num(v):
    if v is None or str(v).strip() == "":
        return None
    try:
        return float(str(v).replace(",", ""))
    except (ValueError, TypeError):
        return None
