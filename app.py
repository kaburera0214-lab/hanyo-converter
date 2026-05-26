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
MAPPING_GITHUB_PATH           = "mapping_templates.json"
SHIPMENT_TEMPLATE_GITHUB_PATH = "shipment_templates.json"

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

_TYPE_LABELS = ["（空欄）", "固定値", "列マッピング", "列結合", "値変換", "特殊ロジック", "条件分岐"]
_TYPE_KEYS   = ["empty",   "fixed",  "column",      "concat", "value_map", "special", "conditional"]

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


# NEの通常テンプレート仕様（マウスオーバー表示用）
FIELD_SPECS = {
    "店舗伝票番号":       ("文字列", "50字",              "モール・カート側の注文番号。1受注ごとにユニーク",        "受注番号"),
    "受注日":             ("文字列", "19字",              "YYYY/MM/DD hh:mm:ss / YYYY/MM/DD / YYYYMMDD",        "受注日"),
    "受注郵便番号":       ("文字列", "20字",              "ハイフンは削除されて取込まれる",                        "郵便番号(購入者情報)"),
    "受注住所１":         ("文字列", "255字",             "",                                                     "住所1(購入者情報)"),
    "受注住所２":         ("文字列", "255字",             "",                                                     "住所2(購入者情報)"),
    "受注名":             ("文字列", "100字",             "購入者名",                                             "購入者名"),
    "受注名カナ":         ("文字列", "100字",             "全角カタカナを入力",                                   "フリガナ(購入者情報)"),
    "受注電話番号":       ("文字列", "20字",              "受注メールアドレスが空欄の場合は必須",                  "電話番号(購入者情報)"),
    "受注メールアドレス": ("文字列", "100字",             "受注電話番号が空欄の場合は必須",                       "メール(購入者情報)"),
    "発送郵便番号":       ("文字列", "20字",              "ハイフンは削除されて取込まれる",                        "郵便番号(送り先情報)"),
    "発送先住所１":       ("文字列", "255字",             "",                                                     "住所1(送り先情報)"),
    "発送先住所２":       ("文字列", "255字",             "",                                                     "住所2(送り先情報)"),
    "発送先名":           ("文字列", "100字",             "送り先名",                                             "送り先名"),
    "発送先カナ":         ("文字列", "100字",             "フリガナ情報",                                         "フリガナ(送り先情報)"),
    "発送電話番号":       ("文字列", "20字",              "ハイフンは削除されて取込まれる",                        "電話番号(送り先情報)"),
    "支払方法":           ("文字列", "20字",              "支払発送変換設定に登録した値",                          "支払方法"),
    "発送方法":           ("文字列", "20字",              "支払発送変換設定に登録した値",                          "発送方法"),
    "商品計":             ("数値",   "0〜1,000,000,000", "商品計金額",                                           "商品計"),
    "税金":               ("数値",   "0〜1,000,000,000", "税込金額の場合は0を入力",                              "税金"),
    "発送料":             ("数値",   "0〜1,000,000,000", "税込金額を入力",                                       "発送金"),
    "手数料":             ("数値",   "0〜1,000,000,000", "税込金額を入力",                                       "手数料"),
    "ポイント":           ("数値",   "0〜1,000,000,000", "利用ポイントを入力",                                   "ポイント"),
    "その他費用":         ("数値",   "±1,000,000,000",  "マイナス数値を入力可能",                               "その他費用"),
    "合計金額":           ("数値",   "0〜1,000,000,000", "商品計+税金+発送料+手数料-ポイント+その他費用",         "総合計"),
    "ギフトフラグ":       ("数値",   "0 または 1",       "0:無し　1:有り",                                       "ギフトフラグ"),
    "時間帯指定":         ("文字列", "20字",              "「時間帯指定＋備考」の値が備考へ取込まれる",            "備考"),
    "日付指定":           ("文字列", "10字",              "YYYY/MM/DD形式で入力",                                 "配達希望日"),
    "作業者欄":           ("文字列", "10,000字",         "作業用欄",                                             "作業用欄"),
    "備考":               ("文字列", "10,000字",         "「時間帯指定＋備考」の値が備考へ取込まれる",            "備考"),
    "商品名":             ("文字列", "255字",             "",                                                     "商品名"),
    "商品コード":         ("文字列", "30字",              "半角英数字または半角ハイフンのみ",                      "商品コード"),
    "商品価格":           ("文字列", "0〜1,000,000,000", "税区分設定にあわせた金額",                              "売単価"),
    "受注数量":           ("文字列", "0〜1,000,000,000", "受注数",                                               "受注数"),
    "商品オプション":     ("文字列", "65,000字",         "商品オプション",                                       "商品op"),
    "出荷済フラグ":       ("文字列", "0 または 1",       "0:通常取込　1:出荷確定済みで取込",                     "—"),
    "顧客区分":           ("文字列", "0 / 9 / 99",       "0:一般顧客　9:卸顧客　99:ブラック顧客",                "顧客区分"),
    "顧客コード":         ("文字列", "10字",              "卸先マスタの卸先コード",                               "顧客cd"),
    "消費税率（%）":      ("文字列", "2字",               "商品ごとの消費税率を入力",                             "消費税率（%）"),
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
        master[jan] = sorted(set(master[jan]), key=lambda x: x[0])
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
        # 重複行を除去してから下限昇順ソート
        master[jan] = sorted(set(master[jan]), key=lambda x: x[0])

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
        for lower, upper, koguchi in sorted(entries, key=lambda x: x[0]):
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
    text = None
    for enc in ("utf-8-sig", "utf-8", "shift_jis", "cp932"):
        try:
            text = order_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
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
                "発送郵便番号":       first.get("送付先郵便番号", "").strip(),
                "発送先住所１":       ship_addr1,
                "発送先住所２":       ship_addr2,
                "発送先名":           first.get("送付先氏名", ""),
                "発送先カナ":         "",
                "発送電話番号":       first.get("送付先電話番号", ""),
                "支払方法":           "その他",
                "発送方法":           "ヤマト運輸",
                "商品計":             item_total,
                "税金":               tax or "",
                "発送料":             shipping or "",
                "手数料":             fee or "",
                "ポイント":           points or "",
                "その他費用":         coupon or "",
                "合計金額":           total,
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
        tracking     = tracking_raw  # カンマ区切り複数送り状番号をそのまま保持
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


# ── カスタム出荷実績：日付変換ヘルパー ────────────────────────
def _format_date(raw):
    """各種日時文字列 → YYYY-MM-DD（LINE ギフト出荷日フォーマット）"""
    raw = str(raw).strip()
    if not raw:
        return ""
    for fmt in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw  # フォールバック：そのまま返す


# ── カスタム出荷実績テンプレート定数 ─────────────────────────
_SHIP_TYPE_LABELS = ["空欄", "固定値", "列マッピング", "日付変換"]
_SHIP_TYPE_KEYS   = ["empty", "fixed", "column", "date"]


def _ship_tpl_to_df(output_fields):
    """output_fields リスト → DataFrame（data_editor 用）"""
    rows = []
    for fd in output_fields:
        ftype = fd.get("type", "empty")
        label = _SHIP_TYPE_LABELS[_SHIP_TYPE_KEYS.index(ftype)] if ftype in _SHIP_TYPE_KEYS else "空欄"
        if ftype == "fixed":
            val = fd.get("value", "")
        elif ftype in ("column", "date"):
            val = fd.get("source", "")
        else:
            val = ""
        rows.append({"フィールド名": fd.get("name", ""), "タイプ": label, "値/参照列": val})
    cols = ["フィールド名", "タイプ", "値/参照列"]
    return pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)


def _df_to_ship_tpl(df):
    """data_editor の DataFrame → output_fields リスト"""
    fields = []
    for _, row in df.iterrows():
        name  = str(row.get("フィールド名", "") or "").strip()
        label = str(row.get("タイプ",     "空欄") or "空欄").strip()
        val   = str(row.get("値/参照列",  "") or "").strip()
        if not name:
            continue
        ftype = _SHIP_TYPE_KEYS[_SHIP_TYPE_LABELS.index(label)] if label in _SHIP_TYPE_LABELS else "empty"
        fd = {"name": name, "type": ftype}
        if ftype == "fixed":
            fd["value"] = val
        elif ftype in ("column", "date"):
            fd["source"] = val
        fields.append(fd)
    return fields


# ── カスタム出荷実績テンプレート：GitHub 保存・読み込み ─────────
def load_shipment_templates_from_github():
    token = st.secrets.get("GITHUB_TOKEN", "")
    repo  = st.secrets.get("GITHUB_REPO", "")
    if not token or not repo:
        return {}
    url     = f"https://api.github.com/repos/{repo}/contents/{SHIPMENT_TEMPLATE_GITHUB_PATH}"
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    r = requests.get(url, headers=headers)
    if not r.ok:
        return {}
    try:
        return json.loads(base64.b64decode(r.json()["content"]).decode("utf-8"))
    except Exception:
        return {}


def save_shipment_templates_to_github(templates):
    token = st.secrets.get("GITHUB_TOKEN", "")
    repo  = st.secrets.get("GITHUB_REPO", "")
    if not token or not repo:
        return False, "GITHUB_TOKEN / GITHUB_REPO がSecretsに設定されていません"

    content_bytes = json.dumps(templates, ensure_ascii=False, indent=2).encode("utf-8")
    url     = f"https://api.github.com/repos/{repo}/contents/{SHIPMENT_TEMPLATE_GITHUB_PATH}"
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}

    r   = requests.get(url, headers=headers)
    sha = r.json().get("sha") if r.ok else None

    payload = {"message": "Update shipment_templates.json via app",
               "content": base64.b64encode(content_bytes).decode()}
    if sha:
        payload["sha"] = sha

    r = requests.put(url, json=payload, headers=headers)
    if r.ok:
        return True, None
    return False, r.json().get("message", "不明なエラー")


# ── カスタム出荷実績：変換エンジン ───────────────────────────
def apply_custom_shipment(inputs_data, template, ne_encoding="cp932"):
    """カスタム出荷実績CSV を生成する。

    inputs_data:
      - bytes / bytearray: 旧来シングル入力（後方互換）
      - list[{"label": str, "rows": list[dict]}]: 新マルチ入力
    template: テンプレート dict（output_fields, _inputs, _columns）
    """
    _inputs_cfg = template.get("_inputs", [])

    # ── 旧来モード（単一 bytes） ──────────────────────────────
    if isinstance(inputs_data, (bytes, bytearray)):
        try:
            text = inputs_data.decode(ne_encoding, errors="replace")
        except Exception:
            text = inputs_data.decode("utf-8", errors="replace")
        rows = [r for r in csv.DictReader(io.StringIO(text)) if any(r.values())]
        if not rows:
            return None, 0, "CSVにデータが見つかりません"
        merged_rows = rows  # ソースキーはプレフィックスなし

    # ── 新マルチ入力モード ────────────────────────────────────
    else:
        if not inputs_data:
            return None, 0, "インプットデータがありません"

        # プライマリのインデックスを決定（_inputs の role="primary"、なければ先頭）
        primary_idx  = next(
            (i for i, c in enumerate(_inputs_cfg) if c.get("role") == "primary"),
            0,
        )
        primary_key  = chr(65 + primary_idx)   # "A", "B", ...（不変キー）
        primary_rows = inputs_data[primary_idx]["rows"] if primary_idx < len(inputs_data) else []
        if not primary_rows:
            return None, 0, f"プライマリ（{primary_key}）にデータがありません"

        # セカンダリは行順で合体（positional join）
        # キーは常に位置インデックスから決定（ラベルに依存しない）
        secondary_positional = {}   # key("B"...) → [row, ...]
        for sec_idx, cfg in enumerate(_inputs_cfg):
            if sec_idx == primary_idx:
                continue
            sec_key  = chr(65 + sec_idx)
            sec_rows = inputs_data[sec_idx]["rows"] if sec_idx < len(inputs_data) else []
            secondary_positional[sec_key] = sec_rows

        # プレフィックス付きマージ行を生成（区切り文字は全角：）
        # プレフィックスは常に位置キー(A/B/C...)を使用 ← ラベルを変えても影響なし
        merged_rows = []
        for idx, pr in enumerate(primary_rows):
            merged = {f"{primary_key}：{k}": v for k, v in pr.items()}
            for sec_key, sec_rows in secondary_positional.items():
                if idx < len(sec_rows):
                    merged.update({f"{sec_key}：{k}": v for k, v in sec_rows[idx].items()})
            merged_rows.append(merged)

    # ── 出力 CSV を生成 ───────────────────────────────────────
    output_fields = template.get("output_fields", [])
    if not output_fields:
        return None, 0, "出力フィールドが設定されていません"

    field_names = [f["name"] for f in output_fields]
    output_rows = []
    for row in merged_rows:
        out = {}
        for fd in output_fields:
            name  = fd["name"]
            ftype = fd.get("type", "empty")
            if ftype == "fixed":
                out[name] = fd.get("value", "")
            elif ftype == "column":
                out[name] = row.get(fd.get("source", ""), "")
            elif ftype == "date":
                out[name] = _format_date(row.get(fd.get("source", ""), ""))
            elif ftype == "value_map":
                src_val   = row.get(fd.get("source", ""), "")
                vmap      = fd.get("map", {})
                out[name] = vmap.get(src_val, fd.get("default", ""))
            elif ftype == "condition":
                result = fd.get("default", "")
                for branch in fd.get("branches", []):
                    col     = branch.get("if_col", "")
                    op      = branch.get("op", "eq")
                    val     = branch.get("if_val", "")
                    out_val = branch.get("then", "")
                    cell    = str(row.get(col, ""))
                    matched = False
                    if   op == "eq":         matched = cell == val
                    elif op == "contains":   matched = val in cell
                    elif op == "startswith": matched = cell.startswith(val)
                    elif op == "endswith":   matched = cell.endswith(val)
                    elif op == "neq":        matched = cell != val
                    if matched:
                        result = out_val
                        break
                out[name] = result
            else:
                out[name] = ""
        output_rows.append(out)

    buf = io.StringIO()
    w   = csv.DictWriter(buf, fieldnames=field_names)
    w.writeheader()
    w.writerows(output_rows)
    return buf.getvalue().encode("utf-8-sig"), len(output_rows), None


# ── カスタム出荷実績：自動紐づけ検出 ──────────────────────────
def auto_detect_shipment_mapping(input_data, output_rows):
    """インプット・アウトプットの実データを行比較してテンプレートを自動生成する。

    input_data:
      - list[dict]: 旧来フラット行リスト
      - list[{"label": str, "rows": list[dict], ...}]: 新マルチ入力
    戻り値: (output_fields リスト, サマリー dict {col -> "fixed"/"column"/"date"/"unknown"})
    """
    if not input_data or not output_rows:
        return [], {}

    # ── 入力形式を判別 ────────────────────────────────────────
    if isinstance(input_data[0], dict) and "label" not in input_data[0]:
        # 旧来フラット行
        input_rows = input_data
    else:
        # 新マルチ入力: プレフィックス付きで行マージ
        valid_entries = [e for e in input_data if e.get("rows")]
        if not valid_entries:
            return [], {}
        n_in = min(len(e["rows"]) for e in valid_entries)
        input_rows = []
        for i in range(n_in):
            merged = {}
            for entry in valid_entries:
                lbl = entry["label"]
                if i < len(entry["rows"]):
                    for k, v in entry["rows"][i].items():
                        merged[f"{lbl}：{k}"] = v
            input_rows.append(merged)

    if not input_rows:
        return [], {}

    n           = min(len(input_rows), len(output_rows))
    input_cols  = list(input_rows[0].keys())
    output_cols = list(output_rows[0].keys())

    output_fields = []
    summary       = {}

    for out_col in output_cols:
        out_vals = [str(output_rows[i].get(out_col, "")).strip() for i in range(n)]

        # 1. 全行が空欄
        if all(v == "" for v in out_vals):
            output_fields.append({"name": out_col, "type": "empty"})
            summary[out_col] = "empty"
            continue

        # 2. 全行が同じ値 → 固定値
        if len(set(out_vals)) == 1:
            output_fields.append({"name": out_col, "type": "fixed", "value": out_vals[0]})
            summary[out_col] = "fixed"
            continue

        # 3. 列マッピング（完全一致）
        matched_col = None
        for in_col in input_cols:
            in_vals = [str(input_rows[i].get(in_col, "")).strip() for i in range(n)]
            if in_vals == out_vals:
                matched_col = in_col
                break
        if matched_col:
            output_fields.append({"name": out_col, "type": "column", "source": matched_col})
            summary[out_col] = f"column:{matched_col}"
            continue

        # 4. 日付変換（_format_date 適用後に一致）
        matched_date_col = None
        for in_col in input_cols:
            in_vals   = [str(input_rows[i].get(in_col, "")).strip() for i in range(n)]
            converted = [_format_date(v) for v in in_vals]
            if converted == out_vals and any(v for v in out_vals):
                matched_date_col = in_col
                break
        if matched_date_col:
            output_fields.append({"name": out_col, "type": "date", "source": matched_date_col})
            summary[out_col] = f"date:{matched_date_col}"
            continue

        # 5. 判定不能 → 空欄で仮置き
        output_fields.append({"name": out_col, "type": "empty"})
        summary[out_col] = "unknown"

    return output_fields, summary


# ── カスタムマッピング：変換エンジン ─────────────────────────
def apply_custom_mapping(order_bytes, mapping_def, master, koguchi_master, encoding="shift_jis"):
    try:
        text = order_bytes.decode(encoding, errors="replace")
    except Exception:
        text = order_bytes.decode("utf-8", errors="replace")

    all_rows = [r for r in csv.DictReader(io.StringIO(text)) if any(r.values())]
    if not all_rows:
        return None, 0, 0, {}, "CSVにデータが見つかりません"

    fields = mapping_def.get("fields", {})

    # ── グループキー：店舗伝票番号フィールドの source 列を使用（旧形式フォールバックあり）
    den_cfg   = fields.get("店舗伝票番号", {})
    group_key = den_cfg.get("source", "") if den_cfg.get("type") == "column" else ""
    if not group_key:
        group_key = mapping_def.get("group_key_column", "")

    # ── SKU列：JANマスタ特殊ロジックの参照列を使用（旧形式フォールバックあり）
    sku_col = ""
    for fd in fields.values():
        if fd.get("type") == "special" and fd.get("logic") in ("jan_master_name", "jan_master_code"):
            sku_col = fd.get("source", "")
            if sku_col:
                break
    if not sku_col:
        sku_col = mapping_def.get("sku_column", "")

    # ── 数量列：受注数量フィールドの source 列を使用（旧形式フォールバックあり）
    qty_cfg = fields.get("受注数量", {})
    qty_col = qty_cfg.get("source", "") if qty_cfg.get("type") == "column" else ""
    if not qty_col:
        qty_col = mapping_def.get("qty_column", "")

    # JANマスタ特殊ロジック使用時に参照列が未設定なら事前エラー
    uses_jan_master = any(
        fd.get("type") == "special" and fd.get("logic") in ("jan_master_name", "jan_master_code")
        for fd in fields.values()
    )
    if uses_jan_master and not sku_col:
        return None, 0, 0, {}, (
            "「JANマスタ→商品名」または「JANマスタ→商品コード」を使用していますが、"
            "参照列が設定されていません。\n"
            "「商品名」または「商品コード」の特殊ロジック設定で JANコード列を参照列として選択してください。"
        )

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
                elif ftype == "conditional":
                    col_a_name = fd.get("col_a", "")
                    col_b_name = fd.get("col_b", "")
                    op         = fd.get("op", "eq")
                    val_a      = item.get(col_a_name, "").strip()
                    val_b      = item.get(col_b_name, "").strip()
                    matched    = (val_a == val_b) if op == "eq" else (val_a != val_b)
                    branch     = "true" if matched else "false"
                    btype      = fd.get(f"{branch}_type", "fixed")
                    bvalue     = fd.get(f"{branch}_value", "")
                    val        = item.get(bvalue, "") if btype == "column" else bvalue
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

    spec = FIELD_SPECS.get(field)
    if spec:
        ftype, flimit, frule, fticket = spec
        lines = [f"[タイプ] {ftype}", f"[上限文字数] {flimit}"]
        if frule:
            lines.append(f"[ルール] {frule}")
        lines.append(f"[伝票項目名] {fticket}")
        help_text = "  \n".join(lines)
    else:
        help_text = None

    col_a, col_b = st.columns([2, 5])
    with col_a:
        chosen_label = st.selectbox(display, _TYPE_LABELS, index=type_idx,
                                    key=f"{pfx}_t_{field}", help=help_text)
    chosen_type = _TYPE_KEYS[_TYPE_LABELS.index(chosen_label)]
    new_cfg = {"type": chosen_type}

    with col_b:
        # label_visibility="hidden" でラベル高さ分のスペースを確保し col_a のドロップダウンと高さを揃える
        if chosen_type == "empty":
            st.text_input("_", value="（空欄）", disabled=True,
                          key=f"{pfx}_em_{field}", label_visibility="hidden")

        elif chosen_type == "fixed":
            new_cfg["value"] = st.text_input(
                "固定値", value=current.get("value", ""),
                key=f"{pfx}_fv_{field}", label_visibility="hidden",
            )

        elif chosen_type == "column":
            src = current.get("source", "") or suggest_column(field, columns)
            idx = col_opts.index(src) if src in col_opts else 0
            sel = st.selectbox("列", col_opts, index=idx,
                               key=f"{pfx}_cs_{field}", label_visibility="hidden")
            new_cfg["source"] = "" if sel == "（未設定）" else sel

        elif chosen_type == "concat":
            srcs = [s for s in current.get("sources", []) if s in columns]
            subs = st.multiselect("結合列", columns, default=srcs,
                                  key=f"{pfx}_cc_{field}", label_visibility="hidden")
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
                                            key=f"{pfx}_sl_{field}", label_visibility="hidden")
                chosen_logic = s_keys[s_labels.index(sel_label)]
                new_cfg["logic"] = chosen_logic
            if chosen_logic in ("order_datetime", "koguchi_note", "jan_master_name", "jan_master_code"):
                with v2:
                    src = current.get("source", "")
                    idx = col_opts.index(src) if src in col_opts else 0
                    sel = st.selectbox("参照キー", col_opts, index=idx,
                                       key=f"{pfx}_sls_{field}")
                    new_cfg["source"] = "" if sel == "（未設定）" else sel

        elif chosen_type == "conditional":
            branch_type_opts = ["固定値", "列の値"]
            branch_type_keys = ["fixed",  "column"]

            # 条件設定（列A ○○ 列B）
            c1, c2, c3 = st.columns([3, 2, 3])
            with c1:
                ca_src = current.get("col_a", "")
                ca_idx = col_opts.index(ca_src) if ca_src in col_opts else 0
                ca_val = st.selectbox("列 A", col_opts, index=ca_idx,
                                      key=f"{pfx}_cca_{field}")
                new_cfg["col_a"] = "" if ca_val == "（未設定）" else ca_val
            with c2:
                op_labels = ["と等しい", "と異なる"]
                op_keys   = ["eq",       "ne"]
                cur_op    = current.get("op", "eq")
                op_idx    = op_keys.index(cur_op) if cur_op in op_keys else 0
                op_val    = st.selectbox("条件", op_labels, index=op_idx,
                                         key=f"{pfx}_cop_{field}")
                new_cfg["op"] = op_keys[op_labels.index(op_val)]
            with c3:
                cb_src = current.get("col_b", "")
                cb_idx = col_opts.index(cb_src) if cb_src in col_opts else 0
                cb_val = st.selectbox("列 B", col_opts, index=cb_idx,
                                      key=f"{pfx}_ccb_{field}")
                new_cfg["col_b"] = "" if cb_val == "（未設定）" else cb_val

            # 一致する場合 / 一致しない場合
            for branch_label, branch_key in [("✅ 一致する場合", "true"), ("❌ 一致しない場合", "false")]:
                b1, b2 = st.columns([2, 4])
                with b1:
                    cur_btype = current.get(f"{branch_key}_type", "fixed")
                    btype_idx = branch_type_keys.index(cur_btype) if cur_btype in branch_type_keys else 0
                    btype_val = st.selectbox(branch_label, branch_type_opts, index=btype_idx,
                                             key=f"{pfx}_c{branch_key}t_{field}")
                    new_cfg[f"{branch_key}_type"] = branch_type_keys[branch_type_opts.index(btype_val)]
                with b2:
                    cur_bval = current.get(f"{branch_key}_value", "")
                    if new_cfg[f"{branch_key}_type"] == "fixed":
                        bval = st.text_input("値", value=cur_bval,
                                             key=f"{pfx}_c{branch_key}vf_{field}",
                                             label_visibility="collapsed")
                    else:
                        bval_idx = col_opts.index(cur_bval) if cur_bval in col_opts else 0
                        bval     = st.selectbox("列", col_opts, index=bval_idx,
                                                key=f"{pfx}_c{branch_key}vc_{field}",
                                                label_visibility="collapsed")
                        bval = "" if bval == "（未設定）" else bval
                    new_cfg[f"{branch_key}_value"] = bval

    return new_cfg


# ── 出荷実績テンプレート：フィールド設定UI ─────────────────────
def _ship_field_config_ui(field, current, columns, pfx):
    """出荷実績テンプレートの1フィールド分UIを描画し、新しい config dict を返す"""
    col_opts    = ["（未設定）"] + columns
    type_labels = ["（空欄）", "固定値", "列マッピング", "日付変換", "値マッピング", "条件分岐"]
    type_keys   = ["empty",   "fixed",  "column",      "date",    "value_map",   "condition"]

    field_hash = hashlib.md5(field.encode("utf-8")).hexdigest()[:8]
    c_type   = current.get("type", "column")
    type_idx = type_keys.index(c_type) if c_type in type_keys else 2  # default: 列マッピング

    col_a, col_b = st.columns([2, 5])
    with col_a:
        chosen_label = st.selectbox(field, type_labels, index=type_idx,
                                    key=f"{pfx}_st_{field}")
    chosen_type = type_keys[type_labels.index(chosen_label)]
    new_cfg = {"name": field, "type": chosen_type}

    with col_b:
        if chosen_type == "empty":
            st.text_input("_", value="（空欄）", disabled=True,
                          key=f"{pfx}_se_{field}", label_visibility="hidden")
        elif chosen_type == "fixed":
            new_cfg["value"] = st.text_input(
                "固定値", value=current.get("value", ""),
                key=f"{pfx}_sfv_{field}", label_visibility="hidden",
            )
        elif chosen_type in ("column", "date"):
            src = current.get("source", "")
            # 保存済みsourceが選択肢にない場合でも末尾に追加して表示
            _copts = col_opts + ([src] if src and src not in col_opts else [])
            idx = _copts.index(src) if src in _copts else 0
            lbl = "日付列" if chosen_type == "date" else "参照列"
            sel = st.selectbox(lbl, _copts, index=idx,
                               key=f"{pfx}_ssc_{field}", label_visibility="hidden")
            new_cfg["source"] = "" if sel == "（未設定）" else sel
        elif chosen_type == "value_map":
            # 値マッピング：元の列を選択して「元の値 → 出力値」テーブルを設定
            vm1, vm2 = st.columns([1, 2])
            with vm1:
                src = current.get("source", "")
                _copts = col_opts + ([src] if src and src not in col_opts else [])
                idx = _copts.index(src) if src in _copts else 0
                sel = st.selectbox("元の列", _copts, index=idx,
                                   key=f"{pfx}_svms_{field}")
                new_cfg["source"]  = "" if sel == "（未設定）" else sel
                new_cfg["default"] = st.text_input(
                    "デフォルト値（一致しない場合）",
                    value=current.get("default", ""),
                    key=f"{pfx}_svmd_{field}",
                )
            with vm2:
                existing = current.get("map", {})
                txt = "\n".join(f"{k} → {v}" for k, v in existing.items())
                edited = st.text_area(
                    "変換テーブル（元の値 → 出力値、1行1ペア）",
                    value=txt, height=100, key=f"{pfx}_svmt_{field}",
                    help="例：\nヤマト宅配 → 宅配便\nネコポス → メール便",
                )
                m = {}
                for ln in edited.strip().split("\n"):
                    if "→" in ln:
                        pts = ln.split("→", 1)
                        m[pts[0].strip()] = pts[1].strip()
                new_cfg["map"] = m
        elif chosen_type == "condition":
            st.caption("↓ 条件を設定してください（上から順に評価、最初にマッチした値を出力）")

    # ─── 条件分岐の詳細UI（col_b の外に展開） ───────────────────
    if chosen_type == "condition":
        branch_key = f"{pfx}_cnd_{field_hash}"

        # 初回のみテンプレート保存値で初期化
        if branch_key not in st.session_state:
            saved_branches = current.get("branches", [])
            st.session_state[branch_key] = saved_branches if saved_branches else [
                {"if_col": "", "op": "eq", "if_val": "", "then": ""}
            ]

        op_labels = ["完全一致", "含む",     "で始まる",    "で終わる",  "一致しない"]
        op_keys   = ["eq",       "contains", "startswith", "endswith", "neq"]

        # ヘッダー
        _h1, _h2, _h3, _h4, _h5 = st.columns([3, 2, 2, 2, 1])
        with _h1: st.caption("🔍 条件列")
        with _h2: st.caption("演算子")
        with _h3: st.caption("比較値")
        with _h4: st.caption("→ 出力値")
        with _h5: st.caption("")

        to_delete    = None
        new_branches = []
        for bi, branch in enumerate(st.session_state[branch_key]):
            b1, b2, b3, b4, b5 = st.columns([3, 2, 2, 2, 1])
            with b1:
                ic      = branch.get("if_col", "")
                _copts  = col_opts + ([ic] if ic and ic not in col_opts else [])
                ci      = _copts.index(ic) if ic in _copts else 0
                if_col  = st.selectbox(f"条件列{bi}", _copts, index=ci,
                                       key=f"{pfx}_cnd_col_{field_hash}_{bi}",
                                       label_visibility="collapsed")
            with b2:
                op     = branch.get("op", "eq")
                oi     = op_keys.index(op) if op in op_keys else 0
                op_lbl = st.selectbox(f"演算子{bi}", op_labels, index=oi,
                                      key=f"{pfx}_cnd_op_{field_hash}_{bi}",
                                      label_visibility="collapsed")
                op_val = op_keys[op_labels.index(op_lbl)]
            with b3:
                if_val = st.text_input(f"比較値{bi}", value=branch.get("if_val", ""),
                                       key=f"{pfx}_cnd_val_{field_hash}_{bi}",
                                       label_visibility="collapsed", placeholder="値")
            with b4:
                then_val = st.text_input(f"出力値{bi}", value=branch.get("then", ""),
                                         key=f"{pfx}_cnd_then_{field_hash}_{bi}",
                                         label_visibility="collapsed", placeholder="出力値")
            with b5:
                if st.button("✕", key=f"{pfx}_cnd_rm_{field_hash}_{bi}", help="この行を削除"):
                    to_delete = bi
            new_branches.append({
                "if_col": "" if if_col == "（未設定）" else if_col,
                "op":     op_val,
                "if_val": if_val,
                "then":   then_val,
            })

        if to_delete is not None:
            del new_branches[to_delete]
            st.session_state[branch_key] = new_branches
            st.rerun()
        else:
            st.session_state[branch_key] = new_branches

        ca, cd = st.columns([2, 3])
        with ca:
            if st.button("＋ 条件を追加", key=f"{pfx}_cnd_add_{field_hash}"):
                st.session_state[branch_key].append(
                    {"if_col": "", "op": "eq", "if_val": "", "then": ""}
                )
                st.rerun()
        with cd:
            default_val = st.text_input(
                "デフォルト値（どの条件にも一致しない場合）",
                value=current.get("default", ""),
                key=f"{pfx}_cnd_def_{field_hash}",
            )
        new_cfg["branches"] = new_branches
        new_cfg["default"]  = default_val

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
                total_rules = sum(len(v) for v in new_km.values())
                with st.spinner("GitHubに保存中..."):
                    ok, save_err = save_koguchi_to_github(new_km)
                if ok:
                    load_koguchi_from_file.clear()
                    st.session_state["koguchi_master"] = new_km
                    km = new_km
                    st.success(f"読み込み・保存完了：{total_rules} ルール")
                else:
                    st.warning(f"読み込みは完了しましたが保存に失敗しました（{save_err}）。下のボタンで再試行してください。")
                    st.session_state["koguchi_master"] = new_km
                    km = new_km

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
                _bytes, _orders, _rows, _nf = convert(order_file.read(), master, koguchi_master)
            st.session_state["tab1_result"] = {
                "csv_bytes": _bytes, "n_orders": _orders,
                "n_rows": _rows, "not_found": _nf,
                "filename": f"hanyo_master_{datetime.now().strftime('%Y%m%d')}.csv",
            }

        res1 = st.session_state.get("tab1_result")
        if res1:
            if res1["not_found"]:
                st.error(f"⚠️ 商品マスタに存在しないJANコードがあります（{len(res1['not_found'])} 件）")
                for jan, orders in res1["not_found"].items():
                    st.markdown(f"- **JAN: `{jan}`** → 注文番号: {', '.join(orders)}")
                st.warning("上記の商品は入力値のまま出力されています。商品マスタを最新版に更新してください。")
                st.divider()
            st.success(f"変換完了：{res1['n_orders']} 件の注文 / {res1['n_rows']} 行")
            st.download_button(
                label="⬇️ 変換済みCSVをダウンロード",
                data=res1["csv_bytes"],
                file_name=res1["filename"],
                mime="text/csv",
                key="dl_hanyo",
            )
            st.link_button(
                "📋 ネクストエンジンに登録する →",
                "https://main.next-engine.com/Usercsv",
                use_container_width=True,
            )

    with tab2:
        if "shipment_templates" not in st.session_state:
            with st.spinner("出荷テンプレートを読み込み中..."):
                st.session_state["shipment_templates"] = load_shipment_templates_from_github()

        ship_std_tab, ship_custom_tab, ship_setup_tab = st.tabs(
            ["▶ 標準変換（出荷代行）", "📋 カスタム出荷実績", "⚙ テンプレート設定"]
        )

        # ── 標準変換（出荷代行） ──────────────────────────────
        with ship_std_tab:
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
                    _bytes2, _rows2, _err2 = convert_shipment(ne_file.read())
                if _err2:
                    st.error(_err2)
                else:
                    st.session_state["tab2_result"] = {
                        "csv_bytes": _bytes2, "n_rows": _rows2,
                        "filename": f"{datetime.now().strftime('%Y%m%d')}_[出荷代行]出荷実績.csv",
                    }

            res2 = st.session_state.get("tab2_result")
            if res2:
                st.success(f"生成完了：{res2['n_rows']} 件")
                st.download_button(
                    label="⬇️ 出荷実績CSVをダウンロード",
                    data=res2["csv_bytes"],
                    file_name=res2["filename"],
                    mime="text/csv",
                    key="dl_shipment",
                )
                st.link_button(
                    "📂 格納フォルダを開く（Google Drive）",
                    "https://drive.google.com/drive/u/2/folders/1jhsvohRf7FLg3vrj8A-8qyEzQlqsYXrJ",
                    use_container_width=True,
                )

        # ── カスタム出荷実績 ──────────────────────────────────
        with ship_custom_tab:
            ship_tpls = st.session_state.get("shipment_templates", {})
            if not ship_tpls:
                st.info("まず「テンプレート設定」タブでテンプレートを作成してください。")
            else:
                st.caption("保存済みテンプレートを使ってカスタム出荷実績CSVを生成します")
                sel_ship_tpl   = st.selectbox("テンプレート", list(ship_tpls.keys()), key="ship_custom_tpl_sel")
                sel_tpl_data   = ship_tpls.get(sel_ship_tpl, {})
                tpl_inputs_cfg = sel_tpl_data.get("_inputs", [])
                cust_enc_map   = {"Shift-JIS (cp932)": "cp932", "UTF-8": "utf-8", "UTF-8 (BOM付き)": "utf-8-sig"}

                if len(tpl_inputs_cfg) > 1:
                    # ── マルチ入力モード ──────────────────────────────
                    cust_inputs_data = []
                    all_uploaded     = True
                    for ci, inp_cfg in enumerate(tpl_inputs_cfg):
                        key      = chr(65 + ci)
                        lbl      = inp_cfg.get("label", key)
                        role_str = "（プライマリ）" if inp_cfg.get("role") == "primary" else "（セカンダリ）"
                        st.markdown(f"**{key}：{lbl} {role_str}**")
                        cx1, cx2 = st.columns([3, 1])
                        with cx2:
                            c_enc_lbl = st.selectbox(
                                "文字コード", list(cust_enc_map.keys()),
                                key=f"ship_cust_enc_{ci}_{sel_ship_tpl}",
                            )
                            c_enc = cust_enc_map[c_enc_lbl]
                        with cx1:
                            c_file = st.file_uploader(
                                f"{lbl} CSV", type="csv",
                                key=f"ship_cust_file_{ci}_{sel_ship_tpl}",
                                label_visibility="hidden",
                            )
                        cust_rows_key = f"ship_cust_rows_{ci}_{sel_ship_tpl}"
                        vcol_check    = inp_cfg.get("validate_col", "").strip()
                        if c_file:
                            try:
                                ctxt  = c_file.read().decode(c_enc, errors="replace")
                                crows = [r for r in csv.DictReader(io.StringIO(ctxt)) if any(r.values())]
                                if vcol_check and crows and vcol_check not in crows[0]:
                                    st.error(
                                        f"⚠️ **{lbl}** のファイルに「{vcol_check}」列が見つかりません。\n\n"
                                        f"正しいファイルをアップロードしてください。"
                                    )
                                else:
                                    st.session_state[cust_rows_key] = crows
                            except Exception as cex:
                                st.error(f"読み込みエラー ({lbl}): {cex}")
                        stored_c = st.session_state.get(cust_rows_key, [])
                        if stored_c:
                            vcol_hint = f"　（識別列：{vcol_check} ✅）" if vcol_check else ""
                            st.success(f"✅ {lbl}：{len(stored_c)} 行{vcol_hint}")
                        else:
                            if vcol_check:
                                st.info(f"{lbl} のCSVをアップロードしてください。（識別列：**{vcol_check}**）")
                            else:
                                st.info(f"{lbl} のCSVをアップロードしてください。")
                            all_uploaded = False
                        cust_inputs_data.append({"label": lbl, "rows": stored_c})

                    st.divider()
                    if st.button("🔄 生成する", type="primary", disabled=not all_uploaded, key="btn_ship_custom"):
                        with st.spinner("生成中..."):
                            _sb, _sr, _se = apply_custom_shipment(cust_inputs_data, sel_tpl_data)
                        if _se:
                            st.error(_se)
                        else:
                            st.session_state["tab2_custom_result"] = {
                                "csv_bytes": _sb, "n_rows": _sr,
                                "filename": f"{datetime.now().strftime('%Y%m%d')}_{sel_ship_tpl}_出荷実績.csv",
                            }
                else:
                    # ── 旧来シングル入力モード ────────────────────────
                    ship_ne_enc_lbl = st.selectbox("文字コード", list(cust_enc_map.keys()), key="ship_custom_enc")
                    ship_ne_file    = st.file_uploader("CSVをアップロード", type="csv", key="ship_ne_upload")
                    st.divider()
                    if st.button("🔄 生成する", type="primary", disabled=ship_ne_file is None, key="btn_ship_custom"):
                        with st.spinner("生成中..."):
                            _sb, _sr, _se = apply_custom_shipment(
                                ship_ne_file.read(),
                                sel_tpl_data,
                                cust_enc_map[ship_ne_enc_lbl],
                            )
                        if _se:
                            st.error(_se)
                        else:
                            st.session_state["tab2_custom_result"] = {
                                "csv_bytes": _sb, "n_rows": _sr,
                                "filename": f"{datetime.now().strftime('%Y%m%d')}_{sel_ship_tpl}_出荷実績.csv",
                            }

                res2c = st.session_state.get("tab2_custom_result")
                if res2c:
                    st.success(f"生成完了：{res2c['n_rows']} 件")
                    st.download_button(
                        label="⬇️ カスタム出荷実績CSVをダウンロード",
                        data=res2c["csv_bytes"],
                        file_name=res2c["filename"],
                        mime="text/csv",
                        key="dl_ship_custom",
                    )

        # ── テンプレート設定 ──────────────────────────────────
        with ship_setup_tab:
            ship_tpls = st.session_state.get("shipment_templates", {})
            st.caption("出荷実績CSVの出力フォーマットテンプレートを設定します")

            if ship_tpls:
                _se_tab, _sn_tab = st.tabs(["✏ テンプレートを編集", "⚙ 新規作成"])
            else:
                _se_tab = None
                _sn_tab = st.container()

            # ═══════════════════════════════════════════════════════
            # ✏ 編集タブ
            # ═══════════════════════════════════════════════════════
            if ship_tpls:
              with _se_tab:
                st.caption("保存済みテンプレートを選択して設定を変更します")
                _edit_sel     = st.selectbox("編集するテンプレート", list(ship_tpls.keys()), key="ship_edit_sel")
                _edit_tpl     = ship_tpls.get(_edit_sel, {})
                _edit_cols    = _edit_tpl.get("_columns", [])
                _edit_inp_cfg = _edit_tpl.get("_inputs", [])
                _e_pfx        = "she" + hashlib.md5(_edit_sel.encode("utf-8")).hexdigest()[:8]
                _e_col_key    = f"ship_edit_cols_{_edit_sel}"

                # 削除ボタン
                if st.button("🗑 このテンプレートを削除", key="btn_ship_edit_del"):
                    st.session_state["_ship_edit_confirm_del"] = True
                if st.session_state.get("_ship_edit_confirm_del"):
                    st.warning(f"「{_edit_sel}」を削除します。よろしいですか？")
                    _ded1, _ded2 = st.columns(2)
                    with _ded1:
                        if st.button("はい、削除する", key="btn_ship_edit_del_ok"):
                            del ship_tpls[_edit_sel]
                            ok, derr = save_shipment_templates_to_github(ship_tpls)
                            if ok:
                                st.session_state["shipment_templates"] = ship_tpls
                                st.session_state["_ship_edit_confirm_del"] = False
                                st.success("削除しました")
                                st.rerun()
                            else:
                                st.error(f"削除失敗: {derr}")
                    with _ded2:
                        if st.button("キャンセル", key="btn_ship_edit_del_cancel"):
                            st.session_state["_ship_edit_confirm_del"] = False
                            st.rerun()

                st.divider()

                # ── ① インプット設定（保存内容を表示・編集） ────────────
                st.subheader("① インプット設定")
                if _edit_inp_cfg:
                    for _ei, _eic in enumerate(_edit_inp_cfg):
                        st.markdown(f"**インプット {chr(65 + _ei)}**")
                        _elc1, _elc2 = st.columns([2, 1])
                        with _elc1:
                            st.text_input(
                                "ファイル名称（表示用）",
                                value=_eic.get("label", ""),
                                key=f"{_e_pfx}_lbl_{_ei}",
                                placeholder="例：ネクストエンジンCSV",
                            )
                        st.text_input(
                            "🔑 識別列",
                            value=_eic.get("validate_col", ""),
                            key=f"{_e_pfx}_vcol_{_ei}",
                            placeholder="このファイルに必ず存在する列名（例：注文ID）",
                            help="変換実行時、ここで指定した列が存在しない場合はエラーを表示します。",
                        )
                        if _ei < len(_edit_inp_cfg) - 1:
                            st.markdown("---")
                    # プライマリファイル選択
                    if len(_edit_inp_cfg) > 1:
                        _e_primary_opts = [
                            st.session_state.get(f"{_e_pfx}_lbl_{_ei}", "")
                            or _edit_inp_cfg[_ei].get("label", f"ファイル{_ei + 1}")
                            for _ei in range(len(_edit_inp_cfg))
                        ]
                        _e_saved_primary = next(
                            (_ei for _ei, _ec in enumerate(_edit_inp_cfg) if _ec.get("role") == "primary"),
                            0,
                        )
                        st.selectbox(
                            "📌 プライマリファイル（出力の行数の基準）",
                            range(len(_edit_inp_cfg)),
                            format_func=lambda x: _e_primary_opts[x],
                            index=_e_saved_primary,
                            key=f"{_e_pfx}_primary_idx",
                            help="変換時にこのファイルの行数を基準にします。もう一方のファイルは行順で対応付けられます。",
                        )
                else:
                    st.caption("（インプット設定なし）")

                # 列リストの表示
                _avail_e = st.session_state.get(_e_col_key, _edit_cols)

                # _columns が空の場合、保存済み output_fields の source 値から再構築
                if not _avail_e:
                    _recon = []
                    for _ef in _edit_tpl.get("output_fields", []):
                        _s = _ef.get("source", "")
                        if _s and _s not in _recon:
                            _recon.append(_s)
                        for _br in _ef.get("branches", []):
                            _ic = _br.get("if_col", "")
                            if _ic and _ic not in _recon:
                                _recon.append(_ic)
                    if _recon:
                        _avail_e = _recon

                if _avail_e:
                    st.success(f"✅ 利用可能な列：{len(_avail_e)} 列")
                    st.caption("　".join(_avail_e[:12]) + ("…" if len(_avail_e) > 12 else ""))
                else:
                    st.info("列情報がありません。下の「列リストを更新する」からCSVをアップロードしてください。")

                with st.expander("▶ 列リストを更新する（オプション：インプットCSVを再アップロード）"):
                    _e_enc_map = {"Shift-JIS (cp932)": "cp932", "UTF-8": "utf-8", "UTF-8 (BOM付き)": "utf-8-sig"}
                    _efc1, _efc2 = st.columns([3, 1])
                    with _efc2:
                        _e_enc = _e_enc_map[st.selectbox("文字コード", list(_e_enc_map.keys()), key=f"{_e_pfx}_enc")]
                    with _efc1:
                        _e_upd_file = st.file_uploader("インプットCSV（列更新用）", type="csv",
                                                        key=f"{_e_pfx}_upd_file", label_visibility="hidden")
                    if _e_upd_file:
                        try:
                            _e_rows = [r for r in csv.DictReader(
                                io.StringIO(_e_upd_file.read().decode(_e_enc, errors="replace"))
                            ) if any(r.values())]
                            if _e_rows:
                                _new_e_cols = list(_e_rows[0].keys())
                                st.session_state[_e_col_key] = _new_e_cols
                                _avail_e = _new_e_cols
                                st.success(f"更新：{len(_new_e_cols)} 列")
                        except Exception as _eex:
                            st.error(f"読み込みエラー: {_eex}")

                st.divider()

                # ── ② 出力フィールドの設定 ──────────────────────
                st.subheader("② 出力フィールドの設定")
                st.caption("タイプや参照列を変更して「変更を保存する」を押してください。")

                _e_fld_names = [f["name"] for f in _edit_tpl.get("output_fields", [])]
                _e_fld_dict  = {f["name"]: f for f in _edit_tpl.get("output_fields", [])}
                _new_out_e   = []

                if _e_fld_names:
                    for _efn in _e_fld_names:
                        _cur_e = _e_fld_dict.get(_efn, {"type": "column"})
                        _new_out_e.append(_ship_field_config_ui(_efn, _cur_e, _avail_e, _e_pfx))
                else:
                    st.info("出力フィールドが設定されていません。新規作成タブでテンプレートを再作成してください。")

                st.divider()

                if st.button("💾 変更を保存する", type="primary", key="btn_ship_edit_save"):
                    if not _new_out_e:
                        st.error("出力フィールドがありません")
                    else:
                        # _inputs の ファイル名称・プライマリ・識別列 を更新して保存
                        _e_primary_idx = int(st.session_state.get(f"{_e_pfx}_primary_idx", 0))
                        _upd_inputs = []
                        for _ei, _eic in enumerate(_edit_inp_cfg):
                            _ic = dict(_eic)
                            # ファイル名称
                            _new_lbl = st.session_state.get(f"{_e_pfx}_lbl_{_ei}", "").strip()
                            if _new_lbl:
                                _ic["label"] = _new_lbl
                            # プライマリ／セカンダリ
                            _ic["role"] = "primary" if _ei == _e_primary_idx else "secondary"
                            # 識別列
                            _vcol = st.session_state.get(f"{_e_pfx}_vcol_{_ei}", "").strip()
                            if _vcol:
                                _ic["validate_col"] = _vcol
                            else:
                                _ic.pop("validate_col", None)
                            _upd_inputs.append(_ic)
                        _upd = dict(_edit_tpl)
                        _upd["output_fields"] = _new_out_e
                        _upd["_columns"]      = _avail_e
                        _upd["_inputs"]       = _upd_inputs
                        ship_tpls[_edit_sel]  = _upd
                        ok, serr = save_shipment_templates_to_github(ship_tpls)
                        if ok:
                            st.session_state["shipment_templates"] = ship_tpls
                            st.success(f"「{_edit_sel}」を保存しました")
                        else:
                            st.error(f"保存失敗: {serr}")

            # ═══════════════════════════════════════════════════════
            # ⚙ 新規作成タブ
            # ═══════════════════════════════════════════════════════
            with _sn_tab:
                st.caption("新しいテンプレートを作成します")
                ship_tpl_name_s    = st.text_input("新しいテンプレート名", key="ship_new_name")
                current_ship_tpl_s = {}

                st.divider()

                # セッションキー
                _skey             = ship_tpl_name_s or "_new"
                ship_out_rows_key = f"ship_out_rows_{_skey}"
                ship_out_flds_key = f"ship_out_flds_{_skey}"
                ship_detected_key = f"ship_detected_{_skey}"
                ship_num_key      = f"ship_num_inputs_{_skey}"
                ship_multi_key    = f"ship_multi_inputs_{_skey}"
                ship_join_key     = f"ship_join_config_{_skey}"
                pfx_ship          = "sh" + hashlib.md5(_skey.encode("utf-8")).hexdigest()[:8]

                # ─── ① インプットCSV ──────────────────────────────────
                st.subheader("① インプットCSV")
                st.caption("変換に使うCSVをアップロードします。複数ある場合は「＋ 追加」してください。")

                # インプット数を初期化（既存テンプレートの _inputs から）
                if ship_num_key not in st.session_state:
                    _saved_inp_cfg0 = current_ship_tpl_s.get("_inputs", [])
                    st.session_state[ship_num_key] = max(1, len(_saved_inp_cfg0))
                num_inputs = st.session_state[ship_num_key]

                # マルチインプットデータを初期化
                if ship_multi_key not in st.session_state:
                    st.session_state[ship_multi_key] = [
                        {"label": "", "rows": [], "cols": []} for _ in range(num_inputs)
                    ]
                multi_inputs = st.session_state[ship_multi_key]
                while len(multi_inputs) < num_inputs:
                    multi_inputs.append({"label": "", "rows": [], "cols": []})
                st.session_state[ship_multi_key] = multi_inputs

                saved_inp_cfg = current_ship_tpl_s.get("_inputs", [])
                enc_setup_map = {"Shift-JIS (cp932)": "cp932", "UTF-8": "utf-8", "UTF-8 (BOM付き)": "utf-8-sig"}

                for i in range(num_inputs):
                    saved_lbl_i  = saved_inp_cfg[i]["label"]         if i < len(saved_inp_cfg) else ""
                    saved_vcol_i = saved_inp_cfg[i].get("validate_col", "") if i < len(saved_inp_cfg) else ""
                    default_lbl  = multi_inputs[i].get("label") or saved_lbl_i or ""
                    st.markdown(f"**インプット {chr(65 + i)}**")
                    ic1, ic2, ic3 = st.columns([2, 3, 1])
                    with ic1:
                        inp_lbl = st.text_input(
                            "ファイル名称（表示用）", value=default_lbl,
                            key=f"{pfx_ship}_inp_lbl_{i}", placeholder="例：ネクストエンジンCSV",
                        )
                    with ic3:
                        inp_enc_lbl = st.selectbox(
                            "文字コード", list(enc_setup_map.keys()),
                            key=f"{pfx_ship}_inp_enc_{i}",
                        )
                        inp_enc = enc_setup_map[inp_enc_lbl]
                    with ic2:
                        inp_file = st.file_uploader(
                            f"CSV {i + 1}", type="csv",
                            key=f"{pfx_ship}_inp_file_{i}", label_visibility="hidden",
                        )
                    st.text_input(
                        "🔑 識別列（変換実行時にファイルの正しさを検証）",
                        value=saved_vcol_i,
                        key=f"{pfx_ship}_inp_vcol_{i}",
                        placeholder="このファイルに必ず存在する列名（例：注文ID）",
                        help="変換実行時、ここで指定した列が存在しない場合はエラーを表示します。省略可。",
                    )
                    if inp_file:
                        try:
                            raw  = inp_file.read()
                            txt  = raw.decode(inp_enc, errors="replace")
                            irows = [r for r in csv.DictReader(io.StringIO(txt)) if any(r.values())]
                            icols = list(irows[0].keys()) if irows else []
                            multi_inputs[i] = {"label": inp_lbl or f"ファイル{i + 1}", "rows": irows, "cols": icols}
                            st.session_state[ship_multi_key] = multi_inputs
                        except Exception as ex:
                            st.error(f"読み込みエラー (インプット{i + 1}): {ex}")

                    # インライン状態（コンパクト）
                    entry = multi_inputs[i]
                    if entry["rows"]:
                        lbl_disp = inp_lbl or entry["label"] or f"ファイル{i + 1}"
                        cols_prev = "　".join(
                            f"{lbl_disp}：{c}" for c in entry["cols"][:6]
                        ) + ("…" if len(entry["cols"]) > 6 else "")
                        st.caption(f"✅ {lbl_disp}：{len(entry['cols'])} 列 / {len(entry['rows'])} 行　　{cols_prev}")
                    else:
                        st.caption(f"⬆️ インプット {i + 1} のCSVをアップロードしてください")

                    if i < num_inputs - 1:
                        st.markdown("---")

                # プライマリファイルの選択（複数インプットの場合）
                if num_inputs > 1:
                    _primary_name_opts = [
                        st.session_state.get(f"{pfx_ship}_inp_lbl_{_pi}", "")
                        or multi_inputs[_pi].get("label", "")
                        or f"ファイル{_pi + 1}"
                        for _pi in range(num_inputs)
                    ]
                    _saved_primary_idx = next(
                        (_pi for _pi, _sc in enumerate(saved_inp_cfg) if _sc.get("role") == "primary"),
                        0,
                    )
                    st.selectbox(
                        "📌 プライマリファイル（出力の行数の基準）",
                        range(num_inputs),
                        format_func=lambda x: _primary_name_opts[x],
                        index=_saved_primary_idx,
                        key=f"{pfx_ship}_primary_idx",
                        help="変換時にこのファイルの行数を基準にします。もう一方のファイルは行順で対応付けられます。",
                    )

                # ＋追加 / 削除ボタン
                bi1, bi2 = st.columns(2)
                with bi1:
                    if st.button("＋ インプットを追加", key=f"{pfx_ship}_add_input"):
                        st.session_state[ship_num_key] += 1
                        multi_inputs.append({"label": "", "rows": [], "cols": []})
                        st.session_state[ship_multi_key] = multi_inputs
                        st.rerun()
                with bi2:
                    if num_inputs > 1 and st.button("－ 最後を削除", key=f"{pfx_ship}_rm_input"):
                        st.session_state[ship_num_key] -= 1
                        multi_inputs.pop()
                        st.session_state[ship_multi_key] = multi_inputs
                        st.rerun()

                # ── 読み込み状態サマリー（常時表示・目立つ） ──────────
                _loaded_parts   = []
                _unloaded_parts = []
                for _si in range(num_inputs):
                    _se   = multi_inputs[_si]
                    _slbl = st.session_state.get(f"{pfx_ship}_inp_lbl_{_si}", "") or _se.get("label", "") or f"ファイル{_si + 1}"
                    if _se.get("rows"):
                        _loaded_parts.append(f"**{_slbl}**：{len(_se['cols'])} 列 / {len(_se['rows'])} 行")
                    else:
                        _unloaded_parts.append(_slbl)
                if _loaded_parts and not _unloaded_parts:
                    st.success("✅ 読み込み済み　　" + "　　|　　".join(_loaded_parts))
                elif _loaded_parts:
                    st.warning(
                        "⚠️ 一部未読み込み　　" + "　　|　　".join(_loaded_parts) +
                        f"　　　　【未読み込み: {', '.join(_unloaded_parts)}】"
                    )
                else:
                    st.info("① のCSVをアップロードしてください。（②をアップロードしても①のデータは保持されます）")

                # avail_ship_cols をプレフィックス付きで構築（全角：区切り）
                # プレフィックスは位置キー(A/B/C...)を固定使用 ← ファイル名称を変えても影響なし
                avail_ship_cols = []
                for i, entry in enumerate(multi_inputs[:num_inputs]):
                    key = chr(65 + i)   # A, B, C... （不変）
                    for c in entry.get("cols", []):
                        avail_ship_cols.append(f"{key}：{c}")
                # フォールバック：保存済みテンプレートの列
                if not avail_ship_cols and current_ship_tpl_s.get("_columns"):
                    avail_ship_cols = current_ship_tpl_s["_columns"]

                has_input_data = any(e.get("rows") for e in multi_inputs[:num_inputs])

                st.divider()

                # ─── ② アウトプット参照CSV（自動検出用） ─────────────────
                st.subheader("② アウトプット参照CSVをアップロード")
                st.caption("客先に渡す出荷実績CSV（実データ入り）をアップロードすると自動で紐づけを検出します。")

                ship_output_csv = st.file_uploader("アウトプット参照CSV（実データ入り）", type="csv", key="ship_output_csv")
                if ship_output_csv:
                    raw_bytes_out   = ship_output_csv.read()
                    all_output_rows = None
                    for enc_try in ("utf-8-sig", "utf-8", "shift_jis", "cp932"):
                        try:
                            txt      = raw_bytes_out.decode(enc_try)
                            rows_try = [r for r in csv.DictReader(io.StringIO(txt)) if any(r.values())]
                            if rows_try:
                                all_output_rows = rows_try
                                break
                        except Exception:
                            continue
                    if all_output_rows:
                        st.session_state[ship_out_rows_key] = all_output_rows
                        st.session_state[ship_out_flds_key] = list(all_output_rows[0].keys())
                    else:
                        st.error("アウトプットCSVの読み込みに失敗しました")

                stored_out_rows = st.session_state.get(ship_out_rows_key, [])
                if stored_out_rows:
                    out_cols_disp = list(stored_out_rows[0].keys())
                    st.success(f"✅ 読み込み済み：{len(out_cols_disp)} 列 / {len(stored_out_rows)} 行")
                    st.caption("出力列: " + "　".join(out_cols_disp))
                else:
                    st.info("アウトプット参照CSVをアップロードしてください。")

                if st.button("🔍 自動検出する", type="secondary",
                             disabled=not (has_input_data and bool(stored_out_rows)),
                             key="btn_ship_autodetect",
                             help="①②両方アップロード後にクリックしてください"):
                    inputs_for_detect = [
                        {
                            "label": st.session_state.get(f"{pfx_ship}_inp_lbl_{i}", "") or multi_inputs[i].get("label", "") or chr(65 + i),
                            "rows":  multi_inputs[i]["rows"],
                            "cols":  multi_inputs[i]["cols"],
                        }
                        for i in range(num_inputs)
                        if multi_inputs[i].get("rows")
                    ]
                    detected_fds, det_summary = auto_detect_shipment_mapping(
                        inputs_for_detect,
                        st.session_state[ship_out_rows_key],
                    )
                    st.session_state[ship_detected_key] = detected_fds
                    # per-field ウィジェットをクリアして自動検出値で再描画
                    for fd in detected_fds:
                        for sfx in ["_st_", "_se_", "_sfv_", "_ssc_"]:
                            st.session_state.pop(f"{pfx_ship}{sfx}{fd['name']}", None)
                    fixed_cnt    = sum(1 for s in det_summary.values() if s == "fixed")
                    col_cnt      = sum(1 for s in det_summary.values() if s.startswith("column"))
                    date_cnt     = sum(1 for s in det_summary.values() if s.startswith("date"))
                    unknown_cols = [c for c, s in det_summary.items() if s == "unknown"]
                    st.info(f"✅ 検出完了：固定値 {fixed_cnt} / 列マッピング {col_cnt} / 日付変換 {date_cnt}")
                    if unknown_cols:
                        st.warning("⚠️ 以下は自動検出できませんでした。③で手動設定してください: " +
                                   "　".join(unknown_cols))
                    st.rerun()

                st.divider()

                # ─── ③ 出力フィールドの設定（per-field ドロップダウン） ──
                st.subheader("③ 出力フィールドの設定")
                st.caption("各フィールドのタイプと参照列を設定してください。")

                out_field_names = st.session_state.get(ship_out_flds_key, [])
                if not out_field_names:
                    saved_fds = current_ship_tpl_s.get("output_fields", [])
                    out_field_names = [f["name"] for f in saved_fds]
                    if out_field_names:
                        st.session_state[ship_out_flds_key] = out_field_names

                if ship_detected_key in st.session_state:
                    field_cfg_list = st.session_state[ship_detected_key]
                else:
                    field_cfg_list = current_ship_tpl_s.get("output_fields", [])
                field_cfg_dict = {f["name"]: f for f in field_cfg_list}

                new_output_fields = []
                if out_field_names:
                    for fname in out_field_names:
                        cur_cfg = field_cfg_dict.get(fname, {"type": "column"})
                        new_output_fields.append(
                            _ship_field_config_ui(fname, cur_cfg, avail_ship_cols, pfx_ship)
                        )
                else:
                    st.info("② アウトプット参照CSVをアップロードすると出力フィールドが自動表示されます。")
                    st.caption("または出力フィールド名を手動で入力（1行1フィールド）:")
                    manual_flds_txt = st.text_area("フィールド名（1行1つ）", height=120,
                                                   key=f"{pfx_ship}_manual_flds",
                                                   label_visibility="collapsed")
                    if manual_flds_txt.strip():
                        out_field_names = [ln.strip() for ln in manual_flds_txt.strip().splitlines() if ln.strip()]
                        st.session_state[ship_out_flds_key] = out_field_names
                        for fname in out_field_names:
                            cur_cfg = field_cfg_dict.get(fname, {"type": "column"})
                            new_output_fields.append(
                                _ship_field_config_ui(fname, cur_cfg, avail_ship_cols, pfx_ship)
                            )

                st.divider()

                # 保存ボタン
                save_btn_lbl_s = "💾 新規保存する"
                if st.button(save_btn_lbl_s, type="primary", key="btn_ship_save"):
                    save_name_s = ship_tpl_name_s.strip()
                    if not save_name_s:
                        st.error("テンプレート名を入力してください")
                    elif not new_output_fields:
                        st.error("出力フィールドを1つ以上設定してください")
                    else:
                        # _inputs config を構築
                        _saved_jc    = st.session_state.get(ship_join_key, [])
                        _primary_idx = int(st.session_state.get(f"{pfx_ship}_primary_idx", 0))
                        save_inp_cfg = []
                        for i in range(num_inputs):
                            lbl_i  = st.session_state.get(f"{pfx_ship}_inp_lbl_{i}", "") or multi_inputs[i].get("label", "") or f"ファイル{i + 1}"
                            inp_cf = {"label": lbl_i, "role": "primary" if i == _primary_idx else "secondary"}
                            if i > 0:
                                jc_i = _saved_jc[i - 1] if i - 1 < len(_saved_jc) else {}
                                inp_cf["join_key_from"] = jc_i.get("from_col", "")
                                inp_cf["join_key_to"]   = jc_i.get("to_col", "")
                            vcol_i = st.session_state.get(f"{pfx_ship}_inp_vcol_{i}", "").strip()
                            if vcol_i:
                                inp_cf["validate_col"] = vcol_i
                            save_inp_cfg.append(inp_cf)
                        ship_tpls[save_name_s] = {
                            "_inputs":       save_inp_cfg,
                            "_columns":      avail_ship_cols,
                            "output_fields": new_output_fields,
                        }
                        ok, serr = save_shipment_templates_to_github(ship_tpls)
                        if ok:
                            st.session_state["shipment_templates"] = ship_tpls
                            st.session_state.pop(ship_detected_key, None)
                            st.success(f"「{save_name_s}」を保存しました")
                        else:
                            st.error(f"保存失敗: {serr}")


    # ── Tab③ カスタム変換 ──────────────────────────────────
    with tab3:
        if "custom_mappings" not in st.session_state:
            with st.spinner("マッピングテンプレートを読み込み中..."):
                st.session_state["custom_mappings"] = load_mappings_from_github()

        mappings = st.session_state.get("custom_mappings", {})

        s_enc_map = {"Shift-JIS": "shift_jis", "UTF-8": "utf-8", "UTF-8 (BOM付き)": "utf-8-sig"}

        exec_tab, edit_tab, setup_tab = st.tabs(["▶ 変換実行", "✏ テンプレートを編集", "⚙ 新規作成"])

        # ── 変換実行 ──────────────────────────────────────
        with exec_tab:
            st.caption("保存済みテンプレートを使って受注CSVを変換します")
            if not mappings:
                st.info("まず「新規作成」タブでテンプレートを作成してください。")
            else:
                sel_name = st.selectbox("テンプレート", list(mappings.keys()), key="exec_tpl_select")
                enc_lbl  = st.selectbox("文字コード", list(s_enc_map.keys()), key="exec_encoding")
                order3   = st.file_uploader("受注CSVをアップロード", type="csv", key="order3_upload")
                st.divider()

                master3         = st.session_state.get("master")
                koguchi_master3 = st.session_state.get("koguchi_master", {})
                if not master3:
                    st.warning("商品マスタが未読み込みです。サイドバーからアップロードしてください。")

                if st.button("🔄 変換する", type="primary", disabled=order3 is None, key="btn_custom_convert"):
                    with st.spinner("変換中..."):
                        _b3, _o3, _r3, _nf3, _e3 = apply_custom_mapping(
                            order3.read(), mappings[sel_name],
                            master3 or {}, koguchi_master3, s_enc_map[enc_lbl],
                        )
                    if _e3:
                        st.error(_e3)
                    else:
                        st.session_state["tab3_result"] = {
                            "csv_bytes": _b3, "n_orders": _o3, "n_rows": _r3,
                            "not_found": _nf3,
                            "filename": f"hanyo_master_{datetime.now().strftime('%Y%m%d')}.csv",
                        }

                res3 = st.session_state.get("tab3_result")
                if res3:
                    if res3["not_found"]:
                        st.error(f"⚠️ 商品が見つかりません（{len(res3['not_found'])} 件）")
                        for jan, oids in res3["not_found"].items():
                            st.markdown(f"- **JAN: `{jan}`** → 注文番号: {', '.join(oids)}")
                        st.warning("上記のJANコードは商品マスタに存在しません。商品マスタを最新版に更新してください。")
                        st.divider()
                    st.success(f"変換完了：{res3['n_orders']} 件の注文 / {res3['n_rows']} 行")
                    st.download_button(
                        label="⬇️ 変換済みCSVをダウンロード",
                        data=res3["csv_bytes"],
                        file_name=res3["filename"],
                        mime="text/csv",
                        key="dl_custom",
                    )
                    st.link_button("📋 ネクストエンジンに登録する →",
                                   "https://main.next-engine.com/Usercsv",
                                   use_container_width=True)
                    st.link_button("📂 受注CSVフォルダ（Google Drive）",
                                   "https://drive.google.com/drive/u/2/folders/1Xil5jgZvxk3A-3s-W-7eYrcMu4R8qvQX",
                                   use_container_width=True)

        # ── テンプレートを編集 ────────────────────────────
        with edit_tab:
            st.caption("既存テンプレートの紐づけを変更・保存します")
            if not mappings:
                st.info("まず「新規作成」タブでテンプレートを作成してください。")
            else:
                sel_edit      = st.selectbox("編集するテンプレート", list(mappings.keys()), key="edit_tpl_sel")
                current_tpl_e = mappings.get(sel_edit, {})

                # 削除
                if st.button("🗑 このテンプレートを削除", key="btn_delete_e"):
                    st.session_state["_confirm_delete_e"] = True
                if st.session_state.get("_confirm_delete_e"):
                    st.warning(f"「{sel_edit}」を削除します。よろしいですか？")
                    dc1e, dc2e = st.columns(2)
                    with dc1e:
                        if st.button("はい、削除する", key="btn_delete_confirm_e"):
                            del mappings[sel_edit]
                            ok, derr = save_mappings_to_github(mappings)
                            if ok:
                                st.session_state["custom_mappings"] = mappings
                                st.session_state["_confirm_delete_e"] = False
                                st.success("削除しました")
                                st.rerun()
                            else:
                                st.error(f"削除失敗: {derr}")
                    with dc2e:
                        if st.button("キャンセル", key="btn_delete_cancel_e"):
                            st.session_state["_confirm_delete_e"] = False
                            st.rerun()

                st.divider()

                # サンプルCSVで列名取得
                st.subheader("① インプットCSVの列を取得")
                sc1e, sc2e = st.columns([3, 1])
                with sc2e:
                    e_enc_label = st.selectbox("文字コード", list(s_enc_map.keys()), key="edit_enc")
                with sc1e:
                    e_sample = st.file_uploader("サンプルCSVをアップロード（列名取得用）",
                                                type="csv", key="edit_sample")

                col_ss_key_e = f"edit_cols_{sel_edit}"
                pfx_e        = "ed" + hashlib.md5(sel_edit.encode("utf-8")).hexdigest()[:8]

                if e_sample:
                    try:
                        raw_text_e   = e_sample.read().decode(s_enc_map[e_enc_label], errors="replace")
                        found_cols_e = list(csv.DictReader(io.StringIO(raw_text_e)).fieldnames or [])
                        prev_cols_e  = st.session_state.get(col_ss_key_e, [])
                        if found_cols_e != prev_cols_e:
                            saved_fields_e = current_tpl_e.get("fields", {})
                            for _f in OUT_HEADERS:
                                if _f not in saved_fields_e:
                                    suggested = suggest_column(_f, found_cols_e)
                                    st.session_state[f"{pfx_e}_t_{_f}"] = "列マッピング"
                                    st.session_state[f"{pfx_e}_cs_{_f}"] = (
                                        suggested if suggested in found_cols_e else "（未設定）"
                                    )
                        st.session_state[col_ss_key_e] = found_cols_e
                        st.success(f"{len(found_cols_e)} 列を検出しました")
                    except Exception as ex:
                        st.error(f"列名の取得に失敗しました: {ex}")

                avail_cols_e = st.session_state.get(col_ss_key_e, [])
                # セッションにない場合はテンプレート保存済みの列リストを自動復元
                if not avail_cols_e and current_tpl_e.get("_columns"):
                    avail_cols_e = current_tpl_e["_columns"]
                    st.session_state[col_ss_key_e] = avail_cols_e
                if avail_cols_e:
                    st.caption("検出列: " + "　".join(avail_cols_e))
                else:
                    st.info("サンプルCSVをアップロードすると列名が選択肢に表示されます。固定値設定は列なしでも可能です。")

                st.divider()

                # フィールド紐づけ
                st.subheader("② 出力フィールドの紐づけ（全38列）")
                st.caption("🔴 必須　🟡 電話／メールどちらか必須　マークなし：任意　 ／　「空欄」は出力がブランクになります。")

                new_fields_e = {}
                for group_name, group_fields in FIELD_GROUPS:
                    with st.expander(group_name, expanded=True):
                        for field in group_fields:
                            cur_cfg_e = current_tpl_e.get("fields", {}).get(field, {})
                            new_fields_e[field] = _field_config_ui(field, cur_cfg_e, avail_cols_e, pfx_e)

                st.divider()

                new_tpl_e = {
                    "_columns": avail_cols_e,
                    "fields":   new_fields_e,
                }
                if st.button("💾 変更を保存する", type="primary", key="btn_save_edit"):
                    mappings[sel_edit] = new_tpl_e
                    ok, serr = save_mappings_to_github(mappings)
                    if ok:
                        st.session_state["custom_mappings"] = mappings
                        st.success(f"「{sel_edit}」を保存しました")
                    else:
                        st.error(f"保存失敗: {serr}")

        # ── 新規作成 ──────────────────────────────────────
        with setup_tab:
            st.caption("新しいテンプレートを作成します")

            tpl_name = st.text_input("新しいテンプレート名", key="new_tpl_name")

            st.divider()

            pfx_n        = "nw" + hashlib.md5((tpl_name or "_new").encode("utf-8")).hexdigest()[:8]
            col_ss_key_n = f"new_cols_{tpl_name}"

            # サンプルCSVで列名取得
            st.subheader("① インプットCSVの列を取得")
            sc1n, sc2n = st.columns([3, 1])
            with sc2n:
                n_enc_label = st.selectbox("文字コード", list(s_enc_map.keys()), key="new_enc")
            with sc1n:
                n_sample = st.file_uploader("サンプルCSVをアップロード（列名取得用）",
                                            type="csv", key="new_sample")

            if n_sample:
                try:
                    raw_text_n   = n_sample.read().decode(s_enc_map[n_enc_label], errors="replace")
                    found_cols_n = list(csv.DictReader(io.StringIO(raw_text_n)).fieldnames or [])
                    prev_cols_n  = st.session_state.get(col_ss_key_n, [])
                    if found_cols_n != prev_cols_n:
                        for _f in OUT_HEADERS:
                            suggested = suggest_column(_f, found_cols_n)
                            st.session_state[f"{pfx_n}_t_{_f}"] = "列マッピング"
                            st.session_state[f"{pfx_n}_cs_{_f}"] = (
                                suggested if suggested in found_cols_n else "（未設定）"
                            )
                    st.session_state[col_ss_key_n] = found_cols_n
                    st.success(f"{len(found_cols_n)} 列を検出しました")
                except Exception as ex:
                    st.error(f"列名の取得に失敗しました: {ex}")

            avail_cols_n = st.session_state.get(col_ss_key_n, [])
            if avail_cols_n:
                st.caption("検出列: " + "　".join(avail_cols_n))
            else:
                st.info("サンプルCSVをアップロードすると列名が選択肢に表示されます。手動入力での固定値設定は列なしでも可能です。")

            st.divider()

            # フィールド紐づけ
            st.subheader("② 出力フィールドの紐づけ（全38列）")
            st.caption("🔴 必須　🟡 電話／メールどちらか必須　マークなし：任意　 ／　「空欄」は出力がブランクになります。")

            new_fields_n = {}
            for group_name, group_fields in FIELD_GROUPS:
                with st.expander(group_name, expanded=True):
                    for field in group_fields:
                        new_fields_n[field] = _field_config_ui(field, {}, avail_cols_n, pfx_n)

            st.divider()

            new_tpl_n = {
                "_columns": avail_cols_n,
                "fields":   new_fields_n,
            }
            if st.button("💾 新規保存する", type="primary", key="btn_save_new_tpl"):
                if not tpl_name.strip():
                    st.error("テンプレート名を入力してください")
                elif tpl_name.strip() in mappings:
                    st.error(
                        f"「{tpl_name.strip()}」は既に存在します。"
                        "「テンプレートを編集」タブから編集してください。"
                    )
                else:
                    mappings[tpl_name.strip()] = new_tpl_n
                    ok, serr = save_mappings_to_github(mappings)
                    if ok:
                        st.session_state["custom_mappings"] = mappings
                        st.success(f"「{tpl_name.strip()}」を作成しました")
                    else:
                        st.error(f"保存失敗: {serr}")


if __name__ == "__main__":
    main()
