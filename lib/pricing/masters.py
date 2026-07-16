# -*- coding: utf-8 -*-
"""
価格改定で使うマスタの読込・保存。

1) 送料・資材マスタ（項目1のサイズコード → 送料/資材/配送種別）
   正本は Drive の pricing_cost_master.csv（画面で行の追加・削除・編集→保存）。
   スプレッドシートとは紐づけない（初期値のみ同梱の lib/pricing/data/cost_master.csv）。

2) NE商品マスタ（JAN→商品コード・原価・項目1）
   汎用マスタ変換・請求書発行と共通（リポジトリの master.csv ／ Driveの master_YYYYMMDD_NNN.csv）。
   売価はNEで管理していない項目のため一切使わない（現販売価格は必ず楽天APIから取得）。

3) 楽天SKU対応表（NE商品コード → 商品管理番号・SKU管理番号）
   「楽天から現在価格を取得」時にRMS APIのレスポンスから自動構築し（rakuten_price）、
   Drive の rakuten_sku_master.csv にキャッシュする。CSVアップロードでの管理はしない。

Drive まわりは請求書モジュールの drive_master をそのまま使う。
"""
import os
import re
import unicodedata

import pandas as pd

from lib.invoice import csv_import, drive_master

# Drive上の固定ファイル名（フォルダはPRODUCT_MASTER_FOLDER_ID）
COST_MASTER_NAME = "pricing_cost_master.csv"
RAKUTEN_SKU_MASTER_NAME = "rakuten_sku_master.csv"

_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

# 列名のゆらぎ → 正規名
# ※売価はNEで管理していない項目のため扱わない（現販売価格は楽天APIから取得する）
_COL_ALIASES = {
    "JANコード": ("JANコード", "JAN", "jan", "JANCD", "JANcd"),
    "商品コード": ("商品コード", "商品CD", "商品cd", "syohin_code"),
    "原価": ("原価", "仕入原価", "下代", "NE原価", "仕入価格", "genka_tnk"),
    "項目1": ("項目1", "項目１"),
}


def norm_key(value):
    """サイズコード/JAN/商品コードの正規化: NFKC・前後空白・末尾".0"除去。"""
    s = unicodedata.normalize("NFKC", str(value)).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s


def _norm_columns(df):
    """列名をNFKC正規化し、ゆらぎを正規名に寄せる。"""
    df = df.rename(columns={c: unicodedata.normalize("NFKC", str(c)).strip() for c in df.columns})
    ren = {}
    for canon, aliases in _COL_ALIASES.items():
        if canon in df.columns:
            continue
        for a in aliases:
            if a in df.columns:
                ren[a] = canon
                break
    return df.rename(columns=ren)


# ── 送料・資材マスタ ──────────────────────────────────────

def _mail_or_takuhai(key):
    """項目1コードから配送種別を推定（数値サイズ=宅配便、メール便系コード=メール便）。"""
    return "メール便" if key in ("nekop", "yuup1", "yuup2", "yuup3", "1", "3") else "宅配便"


def normalize_cost_df(df):
    """送料・資材マスタDataFrameの型を揃える（項目1=str、送料/資材=数値、配送種別を補完）。"""
    df = df.copy()
    df["項目1"] = df["項目1"].map(norm_key)
    for c in ("送料", "資材"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    if "配送種別" not in df.columns:
        df["配送種別"] = ""
    df["配送種別"] = [
        str(v).strip() if str(v).strip() in ("宅配便", "メール便") else _mail_or_takuhai(k)
        for k, v in zip(df["項目1"], df["配送種別"])
    ]
    df = df[df["項目1"] != ""].reset_index(drop=True)
    return df[["項目1", "送料", "資材", "配送種別"]]


def load_cost_master_bundled():
    """同梱CSV（シートから抽出したスナップショット）を読む。"""
    path = os.path.join(_DATA_DIR, "cost_master.csv")
    df = pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")
    return normalize_cost_df(df)


def load_cost_master_drive(folder_id):
    """Drive上の pricing_cost_master.csv を読む（無ければNone）。"""
    f = drive_master.find_file(COST_MASTER_NAME, folder_id)
    if not f:
        return None
    raw = drive_master.download_bytes(f["id"])
    df = _norm_columns(csv_import.read_csv_auto(raw))
    return normalize_cost_df(df)


def save_cost_master_drive(df, folder_id):
    """送料・資材マスタをDriveへ保存（上書き）。"""
    df = normalize_cost_df(df)
    data = df.to_csv(index=False, lineterminator="\r\n").encode("utf-8-sig")
    return drive_master.upload_or_replace(data, COST_MASTER_NAME, folder_id)


def cost_lookup(df):
    """DataFrame → {項目1: (送料, 資材, 配送種別)}。送料/資材はNaN→None。"""
    table = {}
    for _, r in df.iterrows():
        ship = pd.to_numeric(r["送料"], errors="coerce")
        mat = pd.to_numeric(r["資材"], errors="coerce")
        key = norm_key(r["項目1"])
        if not key or key == "nan":
            continue
        delivery = str(r.get("配送種別", "")).strip()
        if delivery not in ("宅配便", "メール便"):
            delivery = _mail_or_takuhai(key)
        table[key] = (
            None if pd.isna(ship) else float(ship),
            None if pd.isna(mat) else float(mat),
            delivery,
        )
    return table


# ── NE商品マスタ（汎用マスタ変換と共通） ─────────────────

def load_ne_master(file_bytes):
    """NEカスタム(商品マスタ)CSVを読む。商品コード必須。原価・JAN・項目1は有無を検査して返す。"""
    df = _norm_columns(csv_import.read_csv_auto(file_bytes))
    if "商品コード" not in df.columns:
        raise ValueError(f"必須列「商品コード」が見つかりません / 実際の列: {list(df.columns)}")
    missing = [c for c in ("JANコード", "原価", "項目1") if c not in df.columns]
    return df, missing


def load_repo_master(repo_root):
    """リポジトリ直下の master.csv（汎用マスタ変換と共有）を読む。無ければ(None, None)。"""
    path = os.path.join(repo_root, "master.csv")
    if not os.path.exists(path):
        return None, None
    with open(path, "rb") as f:
        return load_ne_master(f.read())


def build_lookup(ne_df):
    """
    NE商品マスタ → 突合用のインデックスを作る（10万行規模のためiterrowsは使わない）。
    返り値: (jan→商品コード dict, 商品コード(小文字)→{原価,項目1,商品名} dict)
    """
    cols = ne_df.columns
    codes = ne_df["商品コード"].map(norm_key).tolist()
    jans = ne_df["JANコード"].map(norm_key).tolist() if "JANコード" in cols else None
    names = ne_df["商品名"].astype(str).tolist() if "商品名" in cols else None
    costs = ne_df["原価"].tolist() if "原価" in cols else None
    item1 = ne_df["項目1"].map(norm_key).tolist() if "項目1" in cols else None

    jan_map = {}
    info = {}
    for i, code in enumerate(codes):
        if not code or code == "nan":
            continue
        if jans is not None:
            jan = jans[i]
            if jan and jan != "nan" and jan not in jan_map:
                jan_map[jan] = code
        info[code.lower()] = {
            "商品コード": code,
            "商品名": names[i] if names is not None else "",
            "原価": costs[i] if costs is not None else "",
            "項目1": item1[i] if item1 is not None else "",
        }
    return jan_map, info


# ── 確定した出力CSVの版数管理（Driveバックアップ） ─────────

HISTORY_FOLDER_NAME = "価格改定履歴"


def save_run_to_drive(files, label, folder_id):
    """
    確定した出力CSV一式を、Driveの「価格改定履歴/YYYYMMDD_連番_ラベル」フォルダへ保存する。
    誰がいつどのCSVを作ったかの証跡（版数管理）。同日内は連番が自動で増える。
    files: {ファイル名: bytes} / 返り値: (実行名, フォルダID)
    """
    import datetime
    hist_id = drive_master.get_or_create_folder(HISTORY_FOLDER_NAME, folder_id)
    today = datetime.datetime.now().strftime("%Y%m%d")
    service = drive_master._service()
    q = (f"'{hist_id}' in parents and name contains '{today}_' "
         "and mimeType = 'application/vnd.google-apps.folder' and trashed = false")
    res = service.files().list(q=q, fields="files(name)", pageSize=1000).execute()
    vers = []
    for f in res.get("files", []):
        parts = f["name"].split("_")
        if len(parts) >= 2 and parts[0] == today:
            try:
                vers.append(int(parts[1]))
            except ValueError:
                pass
    run_name = f"{today}_{(max(vers) + 1 if vers else 1):03d}_{label}"
    run_id = drive_master.get_or_create_folder(run_name, hist_id)
    for name, data in files.items():
        drive_master.upload_bytes(data, name, run_id, "text/csv")
    return run_name, run_id


# ── 楽天SKU対応表 ─────────────────────────────────────────

def sku_table_to_df(table):
    """SKU対応表dict {code: (商品管理番号, SKU管理番号, 連携番号)} → 保存用DataFrame。"""
    return pd.DataFrame([
        {"NE商品コード": code, "商品管理番号": v[0], "SKU管理番号": v[1],
         "システム連携用SKU番号": v[2]}
        for code, v in sorted(table.items())
    ], columns=["NE商品コード", "商品管理番号", "SKU管理番号", "システム連携用SKU番号"])


def load_sku_master_drive(folder_id):
    """Drive上の rakuten_sku_master.csv を読む（無ければNone）。"""
    f = drive_master.find_file(RAKUTEN_SKU_MASTER_NAME, folder_id)
    if not f:
        return None
    df = csv_import.read_csv_auto(drive_master.download_bytes(f["id"]))
    return df


def save_sku_master_drive(df, folder_id):
    """楽天SKU対応表をDriveへ保存（上書き）。"""
    data = df.to_csv(index=False, lineterminator="\r\n").encode("utf-8-sig")
    return drive_master.upload_or_replace(data, RAKUTEN_SKU_MASTER_NAME, folder_id)


def sku_lookup(df):
    """SKU対応表 → {NE商品コード(小文字): (商品管理番号, SKU管理番号, システム連携用SKU番号)}"""
    table = {}
    for _, r in df.iterrows():
        code = norm_key(r["NE商品コード"]).lower()
        if code and code not in table:
            table[code] = (norm_key(r["商品管理番号"]), norm_key(r["SKU管理番号"]),
                           norm_key(r.get("システム連携用SKU番号", "")))
    return table


def parent_code(code):
    """NE商品コード→親コード推定（末尾の -数字 を除去。kei0001-01→kei0001）。"""
    return re.sub(r"-\d+$", "", str(code))
