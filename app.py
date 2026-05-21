import base64
import csv
import hashlib
import io
import json
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

# ── Team-EC 固定受注者情報 ────────────────────────────────
TEAMEC = {
    "郵便番号": "192-0052",
    "住所１":   "東京都八王子市本郷町10-6　LC BLDG.",
    "住所２":   "",
    "名前":     "Team-EC株式会社",
    "電話":     "042-686-2176",
}

OUT_HEADERS = [
    "店舗伝票番号", "受注日", "受注郵便番号", "受注住所１", "受注住所２",
    "受注名", "受注名カナ", "受注電話番号", "受注メールアドレス",
    "発送郵便番号", "発送先住所１", "発送先住所２", "発送先名", "発送先カナ", "発送電話番号",
    "支払方法", "発送方法",
    "商品計", "税金", "発送料", "手数料", "ポイント", "その他費用", "合計金額",
    "ギフトフラグ", "時間帯指定", "日付指定", "作業者欄", "備考",
    "商品名", "商品コード", "商品価格", "受注数量", "商品オプション",
    "出荷済フラグ", "顧客区分", "顧客コード", "消費税率（%）",
]

MASTER_PATH   = Path(__file__).parent / "master.csv"
KOGUCHI_PATH  = Path(__file__).parent / "koguchimaster.csv"
MAPPING_GITHUB_PATH = "mapping_templates.json"

SPECIAL_LOGICS = {
    "today":           "今日の日付",
    "order_datetime":  "日時フォーマット変換",
    "jan_master_name": "JANマスタ→商品名",
    "jan_master_code": "JANマスタ→商品コード",
    "koguchi_note":    "個口数メモ（日時列指定）",
}

FIELD_GROUPS = [
    ("注文情報",   ["店舗伝票番号", "受注日"]),
    ("受注者情報", ["受注郵便番号", "受注住所１", "受注住所２", "受注名", "受注名カナ", "受注電話番号", "受注メールアドレス"]),
    ("発送先情報", ["発送郵便番号", "発送先住所１", "発送先住所２", "発送先名", "発送先カナ", "発送電話番号"]),
    ("支払・発送", ["支払方法", "発送方法"]),
    ("金額",       ["商品計", "税金", "発送料", "手数料", "ポイント", "その他費用", "合計金額"]),
    ("オプション", ["ギフトフラグ", "時間帯指定", "日付指定", "作業者欄", "備考"]),
    ("商品情報",   ["商品名", "商品コード", "商品価格", "受注数量", "商品オプション"]),
    ("管理情報",   ["出荷済フラグ", "顧客区分", "顧客コード", "消費税率（%）"]),
]

_TYPE_LABELS = ["（空欄）", "固定値", "列マッピング", "列結合", "値変換", "特殊ロジック"]
_TYPE_KEYS   = ["empty",   "fixed",  "column",      "concat", "value_map", "special"]

# NEの通常テンプレートにおける必須・準必須フィールド
REQUIRED_FIELDS = {
    "店舗伝票番号", "受注日", "受注郵便番号", "受注住所１", "受注名",
    "発送郵便番号", "発送先住所１", "発送先名", "発送電話番号",
    "支払方法", "発送方法", "商品計", "合計金額", "ギフトフラグ",
    "商品名", "商品コード", "商品価格", "受注数量", "出荷済フラグ", "顧客区分",
}
SEMI_REQUIRED_FIELDS = {"受注電話番号", "受注メールアドレス"}  # どちらか一方が必須

# 出力フィールドに対する推測ヒント（部分一致で入力列を探す）
FIELD_HINTS = {
    "店舗伝票番号":       ["注文番号", "モール注文番号", "注文ID", "伝票番号"],
    "受注日":             ["注文可能日", "モール注文日", "注文日", "受注日"],
    "受注郵便番号":       ["注文者郵便", "購入者郵便", "請求先郵便"],
    "受注住所１":         ["注文者都道府県", "注文者住所", "購入者住所"],
    "受注名":             ["注文者の名前", "注文者氏名", "注文者名", "購入者名"],
    "受注電話番号":       ["注文者電話", "購入者電話"],
    "受注メールアドレス": ["メール", "mail", "email"],
    "発送郵便番号":       ["配送先 郵便", "配送先郵便", "送付先郵便"],
    "発送先住所１":       ["配送先 都道府県", "送付先都道府県"],
    "発送先名":           ["配送先 氏名", "配送先氏名", "送付先氏名"],
    "発送先カナ":         ["配送先 カナ", "送付先カナ"],
    "発送電話番号":       ["配送先 電話", "配送先電話", "送付先電話"],
    "支払方法":           ["支払方法", "決済方法"],
    "発送方法":           ["配送方法", "発送方法"],
    "商品計":             ["商品合計税抜", "商品合計", "商品計"],
    "税金":               ["消費税"],
    "発送料":             ["送料合計税抜", "配送料", "送料"],
    "手数料":             ["決済手数料", "手数料"],
    "ポイント":           ["ポイント"],
    "その他費用":         ["値引額", "クーポン"],
    "合計金額":           ["合計金額", "総額"],
    "時間帯指定":         ["お届け指定時間", "時間帯"],
    "日付指定":           ["配送日指定", "お届け日", "配達日"],
    "商品名":             ["商品名"],
    "商品コード":         ["商品コード"],
    "商品価格":           ["商品単価", "単価"],
    "受注数量":           ["注文個数", "数量"],
    "消費税率（%）":      ["税率"],
}


def suggest_column(field, columns):
    """フィールド名のヒントから最適な入力列を推測する"""
    for hint in FIELD_HINTS.get(field, []):
        for col in columns:
            if hint in col:
                return col
    # フォールバック：フィールド名と列名の部分一致
    for col in columns:
        if field in col or col in field:
            return col
    return ""


# ── ユーティリティ ─────────────────────────────────────────
def to_int(val):
    try:
        return int(str(val).strip()) if val is not None and str(val).strip() else 0
    except (ValueError, AttributeError):
        return 0


def today_midnight():
    d = datetime.now()
    return f"{d.year}/{d.month}/{d.day} 0:00:00"


def order_datetime(raw):
    raw = raw.strip()
    if not raw:
        return ""
    try:
        dt = datetime.strptime(raw, "%Y/%m/%d %H:%M")
        return f"{dt.year}/{dt.month}/{dt.day} {dt.hour}:{dt.minute:02d}"
    except ValueError:
        return raw


def delivery_note(first):
    for key in ("カスタマー備考", "倉庫備考", "配送備考"):
        v = first.get(key, "").strip()
        if v:
            return v
    return ""


def receiver_info(first):
    orderer = first.get("注文者氏名", "").strip()
    ship_to = first.get("送付先氏名", "").strip()
    if orderer == ship_to:
        return {
            "郵便番号": TEAMEC["郵便番号"],
            "住所１":   TEAMEC["住所１"],
            "住所２":   TEAMEC["住所２"],
            "名前":     TEAMEC["名前"],
            "電話":     TEAMEC["電話"],
        }
    addr1 = first.get("都道府県", "") + first.get("市区町村", "") + first.get("町名・番地以降", "")
    return {
        "郵便番号": first.get("注文者郵便番号", ""),
        "住所１":   addr1,
        "住所２":   "",
        "名前":     orderer,
        "電話":     first.get("注文者電話番号", ""),
    }


# ── 商品マスタ読み込み ────────────────────────────────────
@st.cache_data(show_spinner="商品マスタを読み込み中...")
def load_master_from_file():
    if not MASTER_PATH.exists():
        return None, "master.csv が見つかりません"
    master = {}
    with open(MASTER_PATH, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            jan = row.get("JANコード", "").strip()
            if jan:
                master[jan] = {
                    "商品コード": row.get("商品コード", "").strip(),
                    "商品名":     row.get("商品名", "").strip(),
                }
    return master, None


def load_master_from_upload(file_bytes):
    text = file_bytes.decode("shift_jis", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    cols = reader.fieldnames or []
    jan_col  = next((c for c in cols if "JAN" in c or "ＪＡＮ" in c), None)
    code_col = next((c for c in cols if "商品コード" in c), None)
    name_col = next((c for c in cols if "商品名" in c and "英語" not in c), None)
    if not (jan_col and code_col and name_col):
        return None, "商品マスタのカラムが見つかりません"
    master = {}
    for row in reader:
        jan = row[jan_col].strip()
        if jan:
            master[jan] = {
                "商品コード": row[code_col].strip(),
                "商品名":     row[name_col].strip(),
            }
    return master, None


# ── 個口数マスタ ──────────────────────────────────────────
@st.cache_data(show_spinner="個口数マスタを読み込み中...")
def load_koguchi_from_file():
    """戻り値: {jan: [(下限, 上限, 個口数), ...]} 下限昇順。上限0=無制限。個口数は int or '宅配'。"""
    if not KOGUCHI_PATH.exists():
        return {}
    master = {}
    with open(KOGUCHI_PATH, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            jan   = (row.get("JANコード") or row.get("SKUコード") or "").strip()
            lower = to_int(row.get("数量（下限）") or row.get("数量") or "")
            upper = to_int(row.get("数量（上限）", "") or "")
            raw   = str(row.get("個口数", "") or "").strip()
            koguchi = "宅配" if raw == "宅配" else to_int(raw)
            if jan and lower and koguchi:
                master.setdefault(jan, []).append((lower, upper, koguchi))
    for jan in master:
        master[jan].sort(key=lambda x: x[0])
    return master


def koguchi_to_df(master):
    """個口数マスタ dict → DataFrame"""
    rows = []
    for jan, entries in master.items():
        for lower, upper, koguchi in entries:
            rows.append({
                "JANコード":    jan,
                "数量（下限）": lower,
                "数量（上限）": upper if upper else None,
                "個口数":       str(koguchi),
            })
    cols = ["JANコード", "数量（下限）", "数量（上限）", "個口数"]
    return pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)


def df_to_koguchi(df):
    """DataFrame → 個口数マスタ dict"""
    master = {}
    for _, row in df.iterrows():
        jan       = str(row.get("JANコード", "") or "").strip()
        lower     = to_int(row.get("数量（下限）"))
        upper     = to_int(row.get("数量（上限）") or 0)
        raw       = str(row.get("個口数", "") or "").strip()
        koguchi   = "宅配" if raw == "宅配" else to_int(raw)
        if jan and lower and koguchi:
            master.setdefault(jan, []).append((lower, upper, koguchi))
    for jan in master:
        master[jan].sort(key=lambda x: x[0])
    return master


def load_koguchi_from_csv_bytes(file_bytes):
    """アップロードされたCSVバイト列から個口数マスタを読み込む。UTF-8/Shift-JIS 自動判定。"""
    text = None
    for enc in ("utf-8-sig", "utf-8", "shift_jis"):
        try:
            text = file_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return None, "文字コードを判定できませんでした（UTF-8 または Shift-JIS で保存してください）"

    reader = csv.DictReader(io.StringIO(text))
    cols = reader.fieldnames or []
    jan_col     = next((c for c in cols if "JAN" in c or "ＪＡＮ" in c or "SKU" in c), None)
    lower_col   = next((c for c in cols if "下限" in c or c.strip() == "数量"), None)
    upper_col   = next((c for c in cols if "上限" in c), None)
    koguchi_col = next((c for c in cols if "個口" in c), None)

    if not (jan_col and lower_col and koguchi_col):
        missing = [n for n, c in [("JANコード", jan_col), ("数量（下限）", lower_col), ("個口数", koguchi_col)] if not c]
        return None, f"必要なカラムが見つかりません: {', '.join(missing)}"

    master = {}
    for row in reader:
        jan     = (row.get(jan_col) or "").strip()
        lower   = to_int(row.get(lower_col) or "")
        upper   = to_int(row.get(upper_col, "") or "") if upper_col else 0
        raw     = str(row.get(koguchi_col, "") or "").strip()
        koguchi = "宅配" if raw == "宅配" else to_int(raw)
        if jan and lower and koguchi:
            master.setdefault(jan, []).append((lower, upper, koguchi))
    for jan in master:
        master[jan].sort(key=lambda x: x[0])

    if not master:
        return None, "有効なデータが見つかりませんでした"
    return master, None


def save_koguchi_to_github(master):
    """個口数マスタをGitHubリポジトリに直接コミットする。"""
    token = st.secrets.get("GITHUB_TOKEN", "")
    repo  = st.secrets.get("GITHUB_REPO", "")
    if not token or not repo:
        return False, "GITHUB_TOKEN / GITHUB_REPO がSecretsに設定されていません"

    rows = []
    for jan, entries in master.items():
        for lower, upper, koguchi in sorted(entries):
            rows.append({
                "JANコード":    jan,
                "数量（下限）": lower,
                "数量（上限）": upper if upper else "",
                "個口数":       koguchi,
            })
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["JANコード", "数量（下限）", "数量（上限）", "個口数"])
    writer.writeheader()
    writer.writerows(rows)
    content_bytes = buf.getvalue().encode("utf-8")

    url     = f"https://api.github.com/repos/{repo}/contents/koguchimaster.csv"
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}

    r = requests.get(url, headers=headers)
    sha = r.json().get("sha") if r.ok else None

    payload = {
        "message": "Update koguchimaster.csv via app",
        "content": base64.b64encode(content_bytes).decode(),
    }
    if sha:
        payload["sha"] = sha

    r = requests.put(url, json=payload, headers=headers)
    if r.ok:
        load_koguchi_from_file.clear()
        return True, None
    return False, r.json().get("message", "不明なエラー")


def calc_koguchi(items, koguchi_master):
    """None / int / '宅配' を返す。複数アイテムで1つでも宅配なら宅配優先。
    複数SKUが混在する注文は個口数判定対象外。"""
    if not koguchi_master:
        return None
    # 複数SKU混在の注文はスキップ
    distinct_skus = {item.get("SKUコード", "").strip() for item in items}
    if len(distinct_skus) > 1:
        return None
    total = 0
    found_any = False
    is_takuhai = False
    for item in items:
        jan     = item.get("SKUコード", "").strip()
        qty     = to_int(item.get("注文個数", ""))
        entries = koguchi_master.get(jan)
        if entries is None:
            continue
        found_any = True
        for lower, upper, k in entries:
            if lower <= qty and (upper == 0 or qty <= upper):
                if k == "宅配":
                    is_takuhai = True
                else:
                    total += k
                break
    if not found_any:
        return None
    if is_takuhai:
        return "宅配"
    if total <= 1:
        return None
    return total


# ── 変換処理 ─────────────────────────────────────────────
def convert(order_bytes, master, koguchi_master):
    text = order_bytes.decode("shift_jis", errors="replace")
    all_rows = [r for r in csv.DictReader(io.StringIO(text)) if any(r.values())]

    orders = OrderedDict()
    for row in all_rows:
        oid = row["モール注文番号"].strip()
        orders.setdefault(oid, []).append(row)

    today = today_midnight()
    output_rows = []
    not_found = {}

    for order_id, items in orders.items():
        first = items[0]

        item_total = sum(to_int(r["商品単価"]) * to_int(r["注文個数"]) for r in items)
        tax        = sum(to_int(r.get("消費税", "")) for r in items)
        shipping   = to_int(first.get("配送料", ""))
        fee        = to_int(first.get("決済手数料", ""))
        points     = to_int(first.get("ポイント利用額", ""))
        coupon     = to_int(first.get("クーポン利用額", ""))
        wrapping   = to_int(first.get("ラッピング手数料", ""))
        total      = item_total + tax + shipping + fee + wrapping - points - coupon

        recv       = receiver_info(first)
        ship_addr1 = first.get("送付先都道府県", "") + first.get("送付先市区町村", "") + first.get("送付先町名・番地以降", "")
        ship_addr2 = delivery_note(first)

        worker_note = order_datetime(first.get("モール注文日時", ""))
        koguchi = calc_koguchi(items, koguchi_master)
        if koguchi == "宅配":
            worker_note += "【宅配変更】"
        elif koguchi:
            worker_note += f"【個口数{koguchi}】"

        for item in items:
            jan  = item.get("SKUコード", "").strip()
            prod = master.get(jan, {})
            if not prod:
                not_found.setdefault(jan, []).append(order_id)

            row = {
                "店舗伝票番号":       order_id,
                "受注日":             today,
                "受注郵便番号":       recv["郵便番号"],
                "受注住所１":         recv["住所１"],
                "受注住所２":         recv["住所２"],
                "受注名":             recv["名前"],
                "受注名カナ":         "",
                "受注電話番号":       recv["電話"],
                "受注メールアドレス": "",
                "発送郵便番号":       first.get("送付先郵便番号", ""),
                "発送先住所１":       ship_addr1,
                "発送先住所２":       ship_addr2,
                "発送先名":           first.get("送付先氏名", ""),
                "発送先カナ":         "",
                "発送電話番号":       first.get("送付先電話番号", ""),
                "支払方法":           "その他",
                "発送方法":           "ヤマト運輸",
                "商品計":             item_total or "",
                "税金":               tax or "",
                "発送料":             shipping or "",
                "手数料":             fee or "",
                "ポイント":           points or "",
                "その他費用":         coupon or "",
                "合計金額":           total or "",
                "ギフトフラグ":       "",
                "時間帯指定":         first.get("お届け指定時間帯", "").strip().lstrip("'"),
                "日付指定":           "",
                "作業者欄":           worker_note,
                "備考":               "",
                "商品名":             prod.get("商品名") or item.get("商品名", ""),
                "商品コード":         prod.get("商品コード") or jan,
                "商品価格":           item.get("商品単価", ""),
                "受注数量":           item.get("注文個数", ""),
                "商品オプション":     "",
                "出荷済フラグ":       "",
                "顧客区分":           "",
                "顧客コード":         "",
                "消費税率（%）":      item.get("税率", ""),
            }
            output_rows.append(row)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=OUT_HEADERS)
    writer.writeheader()
    writer.writerows(output_rows)
    csv_bytes = buf.getvalue().encode("shift_jis", errors="replace")

    return csv_bytes, len(orders), len(output_rows), not_found


# ── 出荷実績CSV生成 ──────────────────────────────────────
def convert_shipment(ne_bytes):
    text = ne_bytes.decode("cp932", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    cols = reader.fieldnames or []

    order_col    = next((c for c in cols if "受注番号" in c), None)
    method_col   = next((c for c in cols if "発送方法" in c), None)
    tracking_col = next((c for c in cols if "発送伝票番号" in c), None)

    missing = [n for n, c in [("受注番号", order_col), ("発送方法", method_col), ("発送伝票番号", tracking_col)] if not c]
    if missing:
        return None, 0, f"必要なカラムが見つかりません: {', '.join(missing)}"

    rows = []
    for row in reader:
        order_num = row[order_col].strip()
        if not order_num:
            continue
        raw_method   = row[method_col].strip()
        method       = "ネコポス" if "ネコポス" in raw_method else "ヤマト"
        tracking_raw = row[tracking_col].strip()
        tracking     = tracking_raw.split(",")[0].strip() if tracking_raw else ""
        rows.append({"モール注文番号": order_num, "配送方法": method, "送り状番号": tracking})

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["モール注文番号", "配送方法", "送り状番号"])
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8-sig"), len(rows), None


# ── カスタムマッピング：GitHub 保存・読み込み ───────────────
def load_mappings_from_github():
    token = st.secrets.get("GITHUB_TOKEN", "")
    repo  = st.secrets.get("GITHUB_REPO", "")
    if not token or not repo:
        return {}
    url     = f"https://api.github.com/repos/{repo}/contents/{MAPPING_GITHUB_PATH}"
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    r = requests.get(url, headers=headers)
    if not r.ok:
        return {}
    try:
        return json.loads(base64.b64decode(r.json()["content"]).decode("utf-8"))
    except Exception:
        return {}


def save_mappings_to_github(mappings):
    token = st.secrets.get("GITHUB_TOKEN", "")
    repo  = st.secrets.get("GITHUB_REPO", "")
    if not token or not repo:
        return False, "GITHUB_TOKEN / GITHUB_REPO がSecretsに設定されていません"

    content_bytes = json.dumps(mappings, ensure_ascii=False, indent=2).encode("utf-8")
    url     = f"https://api.github.com/repos/{repo}/contents/{MAPPING_GITHUB_PATH}"
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}

    r   = requests.get(url, headers=headers)
    sha = r.json().get("sha") if r.ok else None

    payload = {"message": "Update mapping_templates.json via app",
               "content": base64.b64encode(content_bytes).decode()}
    if sha:
        payload["sha"] = sha

    r = requests.put(url, json=payload, headers=headers)
    if r.ok:
        return True, None
    return False, r.json().get("message", "不明なエラー")


# ── カスタムマッピング：変換エンジン ─────────────────────────
def apply_custom_mapping(order_bytes, mapping_def, master, koguchi_master, encoding="shift_jis"):
    try:
        text = order_bytes.decode(encoding, errors="replace")
    except Exception:
        text = order_bytes.decode("utf-8", errors="replace")

    all_rows = [r for r in csv.DictReader(io.StringIO(text)) if any(r.values())]
    if not all_rows:
        return None, 0, 0, {}, "CSVにデータが見つかりません"

    group_key = mapping_def.get("group_key_column", "")
    sku_col   = mapping_def.get("sku_column", "")
    qty_col   = mapping_def.get("qty_column", "")
    fields    = mapping_def.get("fields", {})

    orders = OrderedDict()
    for i, row in enumerate(all_rows):
        oid = row.get(group_key, "").strip() if group_key else str(i)
        orders.setdefault(oid, []).append(row)

    today_str   = today_midnight()
    output_rows = []
    not_found   = {}

    for order_id, items in orders.items():
        koguchi_items = [
            {"SKUコード": r.get(sku_col, "").strip(), "注文個数": r.get(qty_col, "").strip()}
            for r in items
        ] if sku_col and qty_col else []
        koguchi = calc_koguchi(koguchi_items, koguchi_master) if koguchi_items else None

        for item in items:
            jan  = item.get(sku_col, "").strip() if sku_col else ""
            prod = master.get(jan, {}) if (master and jan) else {}
            if master and jan and not prod:
                not_found.setdefault(jan, []).append(order_id)

            out_row = {}
            for out_field in OUT_HEADERS:
                fd    = fields.get(out_field, {"type": "empty"})
                ftype = fd.get("type", "empty")

                if ftype == "fixed":
                    val = fd.get("value", "")
                elif ftype == "column":
                    val = item.get(fd.get("source", ""), "")
                elif ftype == "concat":
                    val = fd.get("sep", "").join(item.get(s, "") for s in fd.get("sources", []))
                elif ftype == "value_map":
                    raw = item.get(fd.get("source", ""), "")
                    val = fd.get("map", {}).get(raw, fd.get("default", raw))
                elif ftype == "special":
                    logic = fd.get("logic", "")
                    src   = fd.get("source", "")
                    if logic == "today":
                        val = today_str
                    elif logic == "order_datetime":
                        val = order_datetime(item.get(src, ""))
                    elif logic == "jan_master_name":
                        val = prod.get("商品名") or item.get("商品名", "")
                    elif logic == "jan_master_code":
                        val = prod.get("商品コード") or jan
                    elif logic == "koguchi_note":
                        note = order_datetime(item.get(src, "")) if src else ""
                        if koguchi == "宅配":
                            note += "【宅配変更】"
                        elif koguchi:
                            note += f"【個口数{koguchi}】"
                        val = note
                    else:
                        val = ""
                else:
                    val = ""
                out_row[out_field] = val
            output_rows.append(out_row)

    buf = io.StringIO()
    w   = csv.DictWriter(buf, fieldnames=OUT_HEADERS)
    w.writeheader()
    w.writerows(output_rows)
    csv_bytes = buf.getvalue().encode("shift_jis", errors="replace")
    return csv_bytes, len(orders), len(output_rows), not_found, None


# ── カスタムマッピング：フィールド設定UI ─────────────────────
def _field_config_ui(field, current, columns, pfx):
    """1フィールド分の設定UIを描画し、新しい config dict を返す"""
    col_opts = ["（未設定）"] + columns
    c_type   = current.get("type", "column")
    type_idx = _TYPE_KEYS.index(c_type) if c_type in _TYPE_KEYS else _TYPE_KEYS.index("column")

    if field in REQUIRED_FIELDS:
        display = f"🔴 {field}"
    elif field in SEMI_REQUIRED_FIELDS:
        display = f"🟡 {field}"
    else:
        display = field

    col_a, col_b = st.columns([2, 5])
    with col_a:
        chosen_label = st.selectbox(display, _TYPE_LABELS, index=type_idx, key=f"{pfx}_t_{field}")
    chosen_type = _TYPE_KEYS[_TYPE_LABELS.index(chosen_label)]
    new_cfg = {"type": chosen_type}

    with col_b:
        if chosen_type == "empty":
            st.caption("—")

        elif chosen_type == "fixed":
            new_cfg["value"] = st.text_input(
                "固定値", value=current.get("value", ""),
                key=f"{pfx}_fv_{field}", label_visibility="collapsed",
            )

        elif chosen_type == "column":
            src = current.get("source", "") or suggest_column(field, columns)
            idx = col_opts.index(src) if src in col_opts else 0
            sel = st.selectbox("列", col_opts, index=idx,
                               key=f"{pfx}_cs_{field}", label_visibility="collapsed")
            new_cfg["source"] = "" if sel == "（未設定）" else sel

        elif chosen_type == "concat":
            srcs = [s for s in current.get("sources", []) if s in columns]
            subs = st.multiselect("結合列", columns, default=srcs,
                                  key=f"{pfx}_cc_{field}", label_visibility="collapsed")
            new_cfg["sources"] = subs
            new_cfg["sep"] = current.get("sep", "")

        elif chosen_type == "value_map":
            v1, v2 = st.columns([1, 2])
            with v1:
                src = current.get("source", "")
                idx = col_opts.index(src) if src in col_opts else 0
                sel = st.selectbox("元の列", col_opts, index=idx, key=f"{pfx}_vms_{field}")
                new_cfg["source"]  = "" if sel == "（未設定）" else sel
                new_cfg["default"] = st.text_input(
                    "デフォルト値", value=current.get("default", ""), key=f"{pfx}_vmd_{field}",
                )
            with v2:
                existing = current.get("map", {})
                txt = "\n".join(f"{k} → {v}" for k, v in existing.items())
                edited = st.text_area(
                    "変換テーブル（元の値 → 変換後の値、1行1ペア）",
                    value=txt, height=90, key=f"{pfx}_vmt_{field}",
                )
                m = {}
                for ln in edited.strip().split("\n"):
                    if "→" in ln:
                        parts = ln.split("→", 1)
                        m[parts[0].strip()] = parts[1].strip()
                new_cfg["map"] = m

        elif chosen_type == "special":
            s_keys   = list(SPECIAL_LOGICS.keys())
            s_labels = list(SPECIAL_LOGICS.values())
            cur_logic = current.get("logic", "today")
            l_idx = s_keys.index(cur_logic) if cur_logic in s_keys else 0
            v1, v2 = st.columns([2, 2])
            with v1:
                sel_label    = st.selectbox("ロジック", s_labels, index=l_idx,
                                            key=f"{pfx}_sl_{field}", label_visibility="collapsed")
                chosen_logic = s_keys[s_labels.index(sel_label)]
                new_cfg["logic"] = chosen_logic
            if chosen_logic in ("order_datetime", "koguchi_note"):
                with v2:
                    src = current.get("source", "")
                    idx = col_opts.index(src) if src in col_opts else 0
                    sel = st.selectbox("参照列", col_opts, index=idx,
                                       key=f"{pfx}_sls_{field}", label_visibility="collapsed")
                    new_cfg["source"] = "" if sel == "（未設定）" else sel
    return new_cfg


# ── パスワード認証 ────────────────────────────────────────
def check_password():
    if st.session_state.get("authenticated"):
        return True
    st.title("🔐 ログイン")
    pw = st.text_input("パスワード", type="password")
    if st.button("ログイン"):
        if pw == st.secrets.get("APP_PASSWORD", ""):
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("パスワードが違います")
    return False


# ── メイン画面 ───────────────────────────────────────────
def main():
    st.set_page_config(page_title="汎用マスタCSV変換", page_icon="📦", layout="centered")

    if not check_password():
        return

    st.title("📦 汎用マスタCSV変換ツール")

    # ── 商品マスタの読み込み ──────────────────────────────
    if "master" not in st.session_state:
        master, err = load_master_from_file()
        if err:
            st.session_state["master"] = None
            st.session_state["master_info"] = "未読み込み"
        else:
            st.session_state["master"] = master
            mtime = Path(MASTER_PATH).stat().st_mtime
            dt = datetime.fromtimestamp(mtime).strftime("%Y/%m/%d")
            st.session_state["master_info"] = f"{len(master):,} 件（更新日: {dt}）"

    # ── 個口数マスタの読み込み ────────────────────────────
    if "koguchi_master" not in st.session_state:
        km = load_koguchi_from_file()
        st.session_state["koguchi_master"] = km

    # ── サイドバー ────────────────────────────────────────
    with st.sidebar:
        # 商品マスタ
        st.header("⚙️ 商品マスタ")
        if st.session_state.get("master"):
            st.success(st.session_state.get("master_info", "読み込み済み"))
        else:
            st.error("マスタ未読み込み")
        st.caption("マスタを更新する場合のみアップロード")
        new_master = st.file_uploader("新しい商品マスタCSV（Shift-JIS）", type="csv", key="up_master")
        if new_master:
            master, err = load_master_from_upload(new_master.read())
            if err:
                st.error(err)
            else:
                st.session_state["master"] = master
                st.session_state["master_info"] = f"{len(master):,} 件（今回アップロード）"
                st.success(f"更新しました：{len(master):,} 件")

        st.divider()

        # 個口数マスタ
        st.header("📦 個口数マスタ")
        km = st.session_state.get("koguchi_master", {})
        rule_count = sum(len(v) for v in km.values())
        st.caption(f"現在 {rule_count} ルール登録済み")

        koguchi_csv = st.file_uploader(
            "CSVで一括更新",
            type="csv",
            key="up_koguchi",
            help="JANコード・数量（下限）・数量（上限）・個口数 の列を含むCSV",
        )
        if koguchi_csv:
            new_km, err = load_koguchi_from_csv_bytes(koguchi_csv.read())
            if err:
                st.error(err)
            else:
                st.session_state["koguchi_master"] = new_km
                km = new_km
                total_rules = sum(len(v) for v in new_km.values())
                st.success(f"読み込み完了：{total_rules} ルール　→ 下のボタンで保存してください")

        df = koguchi_to_df(km)
        edited_df = st.data_editor(
            df,
            num_rows="dynamic",
            use_container_width=True,
            column_config={
                "JANコード":    st.column_config.TextColumn("JANコード",    width="small"),
                "数量（下限）": st.column_config.NumberColumn("数量（下限）", width="small", min_value=1, step=1),
                "数量（上限）": st.column_config.NumberColumn("数量（上限）", width="small", min_value=0, step=1,
                                help="空白 or 0 = 上限なし"),
                "個口数":       st.column_config.TextColumn("個口数",        width="small", help="数字 or 宅配"),
            },
            key="koguchi_editor",
        )

        if st.button("💾 GitHubに保存", type="primary", use_container_width=True):
            new_km = df_to_koguchi(edited_df)
            ok, err = save_koguchi_to_github(new_km)
            if ok:
                st.session_state["koguchi_master"] = new_km
                st.success("保存しました")
            else:
                st.error(f"保存失敗: {err}")

    # ── タブ ──────────────────────────────────────────────
    tab1, tab2, tab3 = st.tabs(["① 汎用マスタCSV変換", "② 出荷実績CSV生成", "③ カスタム変換"])

    with tab1:
        st.caption("先方受注CSV → ネクストエンジン汎用マスタCSV")
        st.divider()
        st.link_button(
            "📂 受注CSVフォルダを開く（Google Drive）",
            "https://drive.google.com/drive/u/2/folders/1Xil5jgZvxk3A-3s-W-7eYrcMu4R8qvQX",
            use_container_width=True,
        )
        st.subheader("受注CSV")
        order_file = st.file_uploader("先方からの受注ファイル", type="csv", key="order_upload")
        st.divider()

        master         = st.session_state.get("master")
        koguchi_master = st.session_state.get("koguchi_master", {})
        can_convert    = order_file is not None and master is not None

        if not master:
            st.error("商品マスタが読み込まれていません。サイドバーからアップロードしてください。")

        if st.button("🔄 変換する", type="primary", disabled=not can_convert, key="btn_convert"):
            with st.spinner("変換中..."):
                csv_bytes, n_orders, n_rows, not_found = convert(order_file.read(), master, koguchi_master)

            if not_found:
                st.error(f"⚠️ 商品マスタに存在しないJANコードがあります（{len(not_found)} 件）")
                for jan, orders in not_found.items():
                    st.markdown(f"- **JAN: `{jan}`** → 注文番号: {', '.join(orders)}")
                st.warning("上記の商品は入力値のまま出力されています。商品マスタを最新版に更新してください。")
                st.divider()

            st.success(f"変換完了：{n_orders} 件の注文 / {n_rows} 行")
            today = datetime.now().strftime("%Y%m%d")
            st.download_button(
                label="⬇️ 変換済みCSVをダウンロード",
                data=csv_bytes,
                file_name=f"hanyo_master_{today}.csv",
                mime="text/csv",
                key="dl_hanyo",
            )
            st.link_button(
                "📋 ネクストエンジンに登録する →",
                "https://main.next-engine.com/Usercsv",
                use_container_width=True,
            )

    with tab2:
        st.caption("NEの出荷完了CSV → 先方への出荷実績CSV")
        st.divider()
        st.subheader("NE出荷完了CSV")
        ne_file = st.file_uploader(
            "ネクストエンジンからダウンロードした出荷完了CSV（Shift-JIS）",
            type="csv",
            key="ne_upload",
        )
        st.divider()

        if st.button("🔄 生成する", type="primary", disabled=ne_file is None, key="btn_shipment"):
            with st.spinner("生成中..."):
                csv_bytes, n_rows, err = convert_shipment(ne_file.read())
            if err:
                st.error(err)
            else:
                st.success(f"生成完了：{n_rows} 件")
                today = datetime.now().strftime("%Y%m%d")
                st.download_button(
                    label="⬇️ 出荷実績CSVをダウンロード",
                    data=csv_bytes,
                    file_name=f"{today}_[出荷代行]出荷実績.csv",
                    mime="text/csv",
                    key="dl_shipment",
                )
                st.link_button(
                    "📂 格納フォルダを開く（Google Drive）",
                    "https://drive.google.com/drive/u/2/folders/1jhsvohRf7FLg3vrj8A-8qyEzQlqsYXrJ",
                    use_container_width=True,
                )


    # ── Tab③ カスタム変換 ──────────────────────────────────
    with tab3:
        if "custom_mappings" not in st.session_state:
            with st.spinner("マッピングテンプレートを読み込み中..."):
                st.session_state["custom_mappings"] = load_mappings_from_github()

        mappings = st.session_state.get("custom_mappings", {})

        exec_tab, setup_tab = st.tabs(["▶ 変換実行", "⚙ 紐づけ設定"])

        # ── 変換実行 ──────────────────────────────────────
        with exec_tab:
            st.caption("保存済みテンプレートを使って受注CSVを変換します")
            if not mappings:
                st.info("まず「紐づけ設定」タブでテンプレートを作成してください。")
            else:
                sel_name = st.selectbox("テンプレート", list(mappings.keys()), key="exec_tpl_select")
                enc_map  = {"Shift-JIS": "shift_jis", "UTF-8": "utf-8", "UTF-8 (BOM付き)": "utf-8-sig"}
                enc_lbl  = st.selectbox("文字コード", list(enc_map.keys()), key="exec_encoding")
                order3   = st.file_uploader("受注CSVをアップロード", type="csv", key="order3_upload")
                st.divider()

                master3         = st.session_state.get("master")
                koguchi_master3 = st.session_state.get("koguchi_master", {})
                if not master3:
                    st.warning("商品マスタが未読み込みです。サイドバーからアップロードしてください。")

                if st.button("🔄 変換する", type="primary", disabled=order3 is None, key="btn_custom_convert"):
                    with st.spinner("変換中..."):
                        csv_bytes3, n_orders3, n_rows3, not_found3, err3 = apply_custom_mapping(
                            order3.read(), mappings[sel_name],
                            master3 or {}, koguchi_master3, enc_map[enc_lbl],
                        )
                    if err3:
                        st.error(err3)
                    else:
                        if not_found3:
                            st.error(f"⚠️ 商品マスタに存在しないJANコードがあります（{len(not_found3)} 件）")
                            for jan, oids in not_found3.items():
                                st.markdown(f"- **JAN: `{jan}`** → 注文番号: {', '.join(oids)}")
                            st.warning("上記の商品は入力値のまま出力されています。")
                            st.divider()
                        st.success(f"変換完了：{n_orders3} 件の注文 / {n_rows3} 行")
                        today3 = datetime.now().strftime("%Y%m%d")
                        st.download_button(
                            label="⬇️ 変換済みCSVをダウンロード",
                            data=csv_bytes3,
                            file_name=f"hanyo_master_{today3}.csv",
                            mime="text/csv",
                            key="dl_custom",
                        )
                        st.link_button("📋 ネクストエンジンに登録する →",
                                       "https://main.next-engine.com/Usercsv",
                                       use_container_width=True)
                        st.link_button("📂 受注CSVフォルダ（Google Drive）",
                                       "https://drive.google.com/drive/u/2/folders/1Xil5jgZvxk3A-3s-W-7eYrcMu4R8qvQX",
                                       use_container_width=True)

        # ── 紐づけ設定 ──────────────────────────────────
        with setup_tab:
            st.caption("インプットCSVの列と出力フィールドの対応を定義し、テンプレートとして保存します")

            # ── テンプレート選択 ──────────────────────────
            tpl_options = ["＋ 新規作成"] + list(mappings.keys())
            sel_tpl     = st.selectbox("テンプレートを選択・新規作成", tpl_options, key="setup_tpl_select")
            is_new      = (sel_tpl == "＋ 新規作成")

            if is_new:
                tpl_name   = st.text_input("新しいテンプレート名", key="new_tpl_name")
                current_tpl = {}
            else:
                tpl_name    = sel_tpl
                current_tpl = mappings.get(sel_tpl, {})
                if st.button("🗑 このテンプレートを削除", key="btn_delete_tpl"):
                    st.session_state["_confirm_delete"] = True
                if st.session_state.get("_confirm_delete"):
                    st.warning(f"「{sel_tpl}」を削除します。よろしいですか？")
                    dc1, dc2 = st.columns(2)
                    with dc1:
                        if st.button("はい、削除する", key="btn_delete_confirm"):
                            del mappings[sel_tpl]
                            ok, derr = save_mappings_to_github(mappings)
                            if ok:
                                st.session_state["custom_mappings"] = mappings
                                st.session_state["_confirm_delete"] = False
                                st.success("削除しました")
                                st.rerun()
                            else:
                                st.error(f"削除失敗: {derr}")
                    with dc2:
                        if st.button("キャンセル", key="btn_delete_cancel"):
                            st.session_state["_confirm_delete"] = False
                            st.rerun()

            st.divider()

            # ── サンプルCSVで列名取得 ──────────────────────
            st.subheader("① インプットCSVの列を取得")
            sc1, sc2 = st.columns([3, 1])
            with sc2:
                s_enc_map   = {"Shift-JIS": "shift_jis", "UTF-8": "utf-8", "UTF-8 (BOM付き)": "utf-8-sig"}
                s_enc_label = st.selectbox("文字コード", list(s_enc_map.keys()), key="setup_enc")
            with sc1:
                sample_file = st.file_uploader("サンプルCSVをアップロード（列名取得用）",
                                               type="csv", key="setup_sample")

            col_ss_key = f"setup_cols_{sel_tpl}"
            # hash() はプロセス起動ごとに変わるため hashlib で安定した pfx を生成
            pfx        = "e" + hashlib.md5(sel_tpl.encode("utf-8")).hexdigest()[:8]
            if sample_file:
                try:
                    raw_text   = sample_file.read().decode(s_enc_map[s_enc_label], errors="replace")
                    found_cols = list(csv.DictReader(io.StringIO(raw_text)).fieldnames or [])
                    prev_cols  = st.session_state.get(col_ss_key, [])
                    if found_cols != prev_cols:
                        # 列が変わったら、未保存フィールドに推測値をセッションに直接書き込む
                        saved_fields = current_tpl.get("fields", {})
                        for _f in OUT_HEADERS:
                            if _f not in saved_fields:
                                suggested = suggest_column(_f, found_cols)
                                st.session_state[f"{pfx}_t_{_f}"] = "列マッピング"
                                st.session_state[f"{pfx}_cs_{_f}"] = suggested if suggested in found_cols else "（未設定）"
                    st.session_state[col_ss_key] = found_cols
                    st.success(f"{len(found_cols)} 列を検出しました")
                except Exception as ex:
                    st.error(f"列名の取得に失敗しました: {ex}")

            available_columns = st.session_state.get(col_ss_key, [])
            if available_columns:
                st.caption("検出列: " + "　".join(available_columns))
            else:
                st.info("サンプルCSVをアップロードすると列名が選択肢に表示されます。手動入力での固定値設定は列なしでも可能です。")

            st.divider()

            # ── グルーピング・SKU・数量列 ───────────────────
            st.subheader("② 注文グルーピング・SKU列の設定")
            col_base = ["（未設定）"] + available_columns
            gc1, gc2, gc3 = st.columns(3)
            with gc1:
                gk_cur = current_tpl.get("group_key_column", "")
                gk_idx = col_base.index(gk_cur) if gk_cur in col_base else 0
                group_key_sel = st.selectbox(
                    "注文グループキー列", col_base, index=gk_idx, key="setup_gk",
                    help="同一注文の複数行をまとめる列（例：注文番号）",
                )
            with gc2:
                sku_cur = current_tpl.get("sku_column", "")
                sku_idx = col_base.index(sku_cur) if sku_cur in col_base else 0
                sku_sel = st.selectbox(
                    "SKU / JANコード列", col_base, index=sku_idx, key="setup_sku",
                    help="JANマスタ検索・個口数計算に使う列",
                )
            with gc3:
                qty_cur = current_tpl.get("qty_column", "")
                qty_idx = col_base.index(qty_cur) if qty_cur in col_base else 0
                qty_sel = st.selectbox(
                    "数量列", col_base, index=qty_idx, key="setup_qty",
                    help="個口数計算に使う列",
                )

            st.divider()

            # ── フィールド紐づけ ────────────────────────────
            st.subheader("③ 出力フィールドの紐づけ（全38列）")
            st.caption("🔴 必須　🟡 電話／メールどちらか必須　マークなし：任意　 ／　「空欄」は出力がブランクになります。")

            new_fields = {}
            for group_name, group_fields in FIELD_GROUPS:
                with st.expander(group_name, expanded=True):
                    for field in group_fields:
                        cur_cfg         = current_tpl.get("fields", {}).get(field, {})
                        new_fields[field] = _field_config_ui(field, cur_cfg, available_columns, pfx)

            st.divider()

            # ── 保存 ────────────────────────────────────────
            new_tpl = {
                "group_key_column": "" if group_key_sel == "（未設定）" else group_key_sel,
                "sku_column":       "" if sku_sel       == "（未設定）" else sku_sel,
                "qty_column":       "" if qty_sel        == "（未設定）" else qty_sel,
                "fields":           new_fields,
            }
            btn_label = "💾 新規保存する" if is_new else "💾 変更を保存する"
            if st.button(btn_label, type="primary", key="btn_save_tpl"):
                if not tpl_name.strip():
                    st.error("テンプレート名を入力してください")
                else:
                    mappings[tpl_name.strip()] = new_tpl
                    ok, serr = save_mappings_to_github(mappings)
                    if ok:
                        st.session_state["custom_mappings"] = mappings
                        st.success(f"「{tpl_name.strip()}」を保存しました")
                    else:
                        st.error(f"保存失敗: {serr}")


if __name__ == "__main__":
    main()
