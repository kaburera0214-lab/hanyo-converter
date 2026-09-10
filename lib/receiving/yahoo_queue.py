# -*- coding: utf-8 -*-
"""
Yahoo反映待ちキュー（APIで反映できない項目を、人がまとめてアップするための待ち行列）。

いま待機キューを使うのは**配送グループだけ**（理由と経路の正本は lib/mall_routes.py）。
価格は2026-07-30からAPIで直接反映しているので、このキューには入らない。

入荷登録で配送グループが変わるたびDrive上のキューCSVに追記し（同一コードは最新値で上書き）、
管理者が1枚にまとめてストアクリエイターProへアップ → 「アップ済み」でキューを空にする
（内容はアーカイブへ自動退避）。人が忘れると永久に反映されないので、滞留は
batch/yahoo_queue_watch.py が毎日見張る。

- yahoo_pending_delivery.csv … 配送グループ（code, 配送グループ管理番号, 追加日時）
- yahoo_pending_prices.csv   … 価格。**2026-08-24に運用終了**。旧キューの復旧
  （価格改定ページの管理者用expander）だけが読んでいる
アーカイブは Drive の「Yahoo反映済み/」フォルダへ時刻付きで保存する。
"""
import datetime

import pandas as pd

from lib.invoice import csv_import, drive_master
from lib.pricing import export as ex

PRICE_PENDING = "yahoo_pending_prices.csv"
DELIVERY_PENDING = "yahoo_pending_delivery.csv"
# キューCSVの列名（Drive上の既存行がこの名前なので変えない。
# Yahooへアップするときのフィールド名は別物＝ex.YAHOO_DELIVERY_FIELD）。
DELIVERY_VALUE_COLUMN = "配送グループ管理番号"
ARCHIVE_FOLDER = "Yahoo反映済み"
CRLF = "\r\n"         # YahooのCSVはCRLF
_ENCODING = "cp932"   # Yahooストアクリエイターは Shift-JIS 系
# キューCSVの読込は毎rerunで発生する（表示用のexpander内）。この秒数はセッションに
# キャッシュしてDrive往復を省く（体感速度向上）。書き込み(_save)時はキャッシュ破棄で整合。
_QUEUE_TTL = 60
_QUEUE_CK = "_yahoo_queue_ck"


def _load(name, folder_id, use_cache=True):
    import time
    import streamlit as st
    ck = st.session_state.get(_QUEUE_CK) or {}
    ent = ck.get(name)
    if use_cache and ent and ent.get("folder") == folder_id \
            and (time.time() - ent["at"]) < _QUEUE_TTL:
        return ent["df"]
    f = drive_master.find_file(name, folder_id)
    if not f:
        df = pd.DataFrame()
    else:
        try:
            df = csv_import.read_csv_auto(drive_master.download_bytes(f["id"]))
        except Exception:  # noqa: BLE001
            df = pd.DataFrame()
    ck[name] = {"folder": folder_id, "at": time.time(), "df": df}
    st.session_state[_QUEUE_CK] = ck
    return df


def _save(df, name, folder_id):
    data = df.to_csv(index=False, lineterminator="\r\n").encode(_ENCODING, errors="replace")
    drive_master.upload_or_replace(data, name, folder_id, mimetype="text/csv")
    import streamlit as st
    ck = st.session_state.get(_QUEUE_CK) or {}
    ck.pop(name, None)                    # 書き込んだので次回読込はDriveから取り直す
    st.session_state[_QUEUE_CK] = ck


# 人がアップするまで反映されない経路なので、忘れられていないかを日数で見張る。
# この日数を超えたら画面に赤を出し、batch/yahoo_queue_watch.py が稼働監視へ上げる。
STALE_DAYS = 3


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def oldest_age_days(df):
    """キューの中で最も古い「追加日時」から何日経ったか。判定できなければ None。

    None（＝日時が読めない）を0日に丸めないこと。丸めると滞留していても
    正常に見えてしまい、この見張り自体が意味を失う。
    """
    if df is None or len(df) == 0 or "追加日時" not in getattr(df, "columns", []):
        return None
    stamps = pd.to_datetime(df["追加日時"], errors="coerce").dropna()
    if stamps.empty:
        return None
    return int((datetime.datetime.now() - stamps.min().to_pydatetime()).days)


def load_prices(folder_id):
    return _load(PRICE_PENDING, folder_id)


def load_delivery(folder_id):
    return _load(DELIVERY_PENDING, folder_id)


def append_prices(rows, folder_id):
    """rows=[{code, price}] を価格キューにupsert（同codeは最新価格で上書き）。総件数を返す。"""
    return _append(rows, PRICE_PENDING, ["code", "price", "追加日時"], "price", folder_id)


def append_delivery(rows, folder_id):
    """rows=[{code, 配送グループ管理番号}] を配送キューにupsert。総件数を返す。"""
    return _append(rows, DELIVERY_PENDING, ["code", DELIVERY_VALUE_COLUMN, "追加日時"],
                   DELIVERY_VALUE_COLUMN, folder_id)


def _append(rows, name, columns, value_col, folder_id):
    if not rows:
        return len(load_prices(folder_id) if name == PRICE_PENDING
                   else load_delivery(folder_id))
    cur = _load(name, folder_id, use_cache=False)   # 読み書きは最新を読む
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
    cur = _load(name, folder_id, use_cache=False)   # 読み書きは最新を読む
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
    return _clear(DELIVERY_PENDING, ["code", DELIVERY_VALUE_COLUMN, "追加日時"],
                  "yahoo_delivery", folder_id)


def resolve_delivery(codes, folder_id):
    """反映が確認できたコードだけをキューから外す（内容はアーカイブへ退避）。

    キュー全体を空にする clear_delivery と違い、**確認できたものだけ**を消す。
    まとめて空にすると、反映されていない行まで「済んだこと」になってしまう。
    """
    codes = {str(c).strip().lower() for c in codes if str(c).strip()}
    if not codes:
        return 0
    cur = _load(DELIVERY_PENDING, folder_id, use_cache=False)
    if cur.empty or "code" not in cur.columns:
        return 0
    hit = cur["code"].astype(str).str.strip().str.lower().isin(codes)
    if not hit.any():
        return 0
    arch_id = drive_master.get_or_create_folder(ARCHIVE_FOLDER, folder_id)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    data = cur[hit].to_csv(index=False, lineterminator=CRLF).encode(_ENCODING, errors="replace")
    drive_master.upload_bytes(data, f"yahoo_delivery_reflected_{stamp}.csv", arch_id, "text/csv")
    _save(cur[~hit], DELIVERY_PENDING, folder_id)
    return int(hit.sum())


def expected_no(df_row, group_no):
    """キューの1行から「あるべき配送グループNo」を求める。分からなければ空文字。"""
    return str((group_no or {}).get(bin_of(df_row[DELIVERY_VALUE_COLUMN]), "")).strip()


def upload_csv_bytes(df, value_col):
    """Yahooアップ用CSV（code, value のみ・追加日時は落とす）のbytes。"""
    cols = ["code", value_col]
    slim = df[cols] if all(c in df.columns for c in cols) else pd.DataFrame(columns=cols)
    return slim.to_csv(index=False, lineterminator="\r\n").encode(_ENCODING, errors="replace")


def bin_of(value):
    """キューの保存値 → 便種名。NT/NM は内部表記、便種名がそのまま入っていても通す。"""
    value = str(value).strip()
    return ex.YAHOO_BIN_BY_VALUE.get(value, value)


def missing_group_bins(df, group_no):
    """キューの中で、配送グループNoが設定されていない便種の一覧。

    「Noが分からない」を空欄や0で埋めない。間違ったNoを送ると、アップロードは
    成功したように見えて送料設定だけ静かに壊れる。
    """
    if df is None or len(df) == 0 or DELIVERY_VALUE_COLUMN not in getattr(df, "columns", []):
        return []
    bins = {bin_of(v) for v in df[DELIVERY_VALUE_COLUMN]}
    return sorted(b for b in bins if not str((group_no or {}).get(b, "")).strip())


def delivery_upload_csv(df, group_no):
    """待機キュー → Yahoo「項目指定」アップロード用CSV（code, postage-set）のbytes。

    見出しは半角のフィールド名でなければならない（日本語見出しは U-004-0020 で弾かれる）。
    値は配送グループの「No」で、店舗設定なので group_no から引く。
    """
    rows = [{"商品管理番号": r["code"], "新便種": bin_of(r[DELIVERY_VALUE_COLUMN])}
            for _, r in df.iterrows()]
    return ex.yahoo_delivery_csv(rows, group_no)
