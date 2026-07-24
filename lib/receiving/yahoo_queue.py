# -*- coding: utf-8 -*-
"""
Yahoo反映待ちキュー（Yahoo APIが使えるようになるまでの手動アップ運用の効率化）。

入荷登録で価格・配送グループが変わるたび、Drive上の1つのキューCSVに追記していく
（同一コードは最新値で上書き）。管理者はまとめて1枚をダウンロードしてストアクリエイター
Proへ1回アップし、「アップ済み」でキューを空にする（内容はアーカイブへ自動退避）。

- yahoo_pending_prices.csv   … 価格（code, price）
- yahoo_pending_delivery.csv … 配送グループ（code, 配送グループ管理番号）
アーカイブは Drive の「Yahoo反映済み/」フォルダへ時刻付きで保存する。
"""
import datetime

import pandas as pd

from lib.invoice import csv_import, drive_master

PRICE_PENDING = "yahoo_pending_prices.csv"
DELIVERY_PENDING = "yahoo_pending_delivery.csv"
ARCHIVE_FOLDER = "Yahoo反映済み"
_ENCODING = "cp932"   # Yahooストアクリエイターは Shift-JIS 系


def _load(name, folder_id):
    f = drive_master.find_file(name, folder_id)
    if not f:
        return pd.DataFrame()
    try:
        return csv_import.read_csv_auto(drive_master.download_bytes(f["id"]))
    except Exception:  # noqa: BLE001
        return pd.DataFrame()


def _save(df, name, folder_id):
    data = df.to_csv(index=False, lineterminator="\r\n").encode(_ENCODING, errors="replace")
    drive_master.upload_or_replace(data, name, folder_id, mimetype="text/csv")


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def load_prices(folder_id):
    return _load(PRICE_PENDING, folder_id)


def load_delivery(folder_id):
    return _load(DELIVERY_PENDING, folder_id)


def append_prices(rows, folder_id):
    """rows=[{code, price}] を価格キューにupsert（同codeは最新価格で上書き）。総件数を返す。"""
    return _append(rows, PRICE_PENDING, ["code", "price", "追加日時"], "price", folder_id)


def append_delivery(rows, folder_id):
    """rows=[{code, 配送グループ管理番号}] を配送キューにupsert。総件数を返す。"""
    return _append(rows, DELIVERY_PENDING, ["code", "配送グループ管理番号", "追加日時"],
                   "配送グループ管理番号", folder_id)


def _append(rows, name, columns, value_col, folder_id):
    if not rows:
        return len(load_prices(folder_id) if name == PRICE_PENDING
                   else load_delivery(folder_id))
    cur = _load(name, folder_id)
    merged = {}
    if not cur.empty and "code" in cur.columns:
        for _, r in cur.iterrows():
            merged[str(r["code"])] = {c: r.get(c, "") for c in columns}
    now = _now()
    for row in rows:
        code = str(row["code"]).strip()
        if not code:
            continue
        merged[code] = {"code": code, value_col: row[value_col], "追加日時": now}
    out = pd.DataFrame(list(merged.values()), columns=columns)
    _save(out, name, folder_id)
    return len(out)


def _clear(name, columns, label, folder_id):
    cur = _load(name, folder_id)
    if cur.empty:
        return 0
    arch_id = drive_master.get_or_create_folder(ARCHIVE_FOLDER, folder_id)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    data = cur.to_csv(index=False, lineterminator="\r\n").encode(_ENCODING, errors="replace")
    drive_master.upload_bytes(data, f"{label}_{stamp}.csv", arch_id, "text/csv")
    _save(pd.DataFrame(columns=columns), name, folder_id)
    return len(cur)


def clear_prices(folder_id):
    """価格キューをアーカイブして空にする。アップ済みにした件数を返す。"""
    return _clear(PRICE_PENDING, ["code", "price", "追加日時"], "yahoo_prices", folder_id)


def clear_delivery(folder_id):
    return _clear(DELIVERY_PENDING, ["code", "配送グループ管理番号", "追加日時"],
                  "yahoo_delivery", folder_id)


def upload_csv_bytes(df, value_col):
    """Yahooアップ用CSV（code, value のみ・追加日時は落とす）のbytes。"""
    cols = ["code", value_col]
    slim = df[cols] if all(c in df.columns for c in cols) else pd.DataFrame(columns=cols)
    return slim.to_csv(index=False, lineterminator="\r\n").encode(_ENCODING, errors="replace")
