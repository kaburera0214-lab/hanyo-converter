# -*- coding: utf-8 -*-
"""
CSV取込ユーティリティ（請求書発行機能専用）。

送料表など、スプレッドシートで管理している複雑なマスタを
CSVで丸ごと取り込むためのパーサ群。文字コードの自動判定、
桁区切りカンマ（"1,460"）の除去などを行う。
"""
import io
import re
import pandas as pd


def load_concat(uploaded_files, loader):
    """
    複数のアップロードファイルを loader で読み込み、縦に結合して返す。
    loader: bytes -> DataFrame（各CSVの検証もloaderが行う）。
    NEの出荷確定など、1000件単位で分割されたファイルをまとめて扱うため。

    1ファイルが失敗しても他は活かす。返り値: (結合DataFrame or None, errors)
      errors: [(ファイル名, エラー文), ...]
    """
    import pandas as pd
    dfs, errors = [], []
    for f in uploaded_files:
        name = getattr(f, "name", "(ファイル)")
        try:
            dfs.append(loader(f.getvalue()))
        except Exception as e:  # noqa: BLE001
            errors.append((name, str(e)))
    df = pd.concat(dfs, ignore_index=True) if dfs else None
    return df, errors


def read_csv_auto(file_bytes):
    """
    bytesからDataFrameを読む。UTF-8(BOM可)→CP932の順で文字コードを試す。
    全セルは文字列として読み込む（数値変換は呼び出し側で行う）。
    """
    last_err = None
    for enc in ("utf-8-sig", "cp932", "utf-8"):
        # まず標準(C)パーサ、ダメなら寛容なPythonパーサで再試行（行のばらつき対策）
        for kwargs in ({}, {"engine": "python", "on_bad_lines": "skip"}):
            try:
                return pd.read_csv(
                    io.BytesIO(file_bytes), dtype=str, encoding=enc, **kwargs).fillna("")
            except Exception as e:  # noqa: BLE001
                last_err = e
    raise ValueError(f"CSVを読み込めませんでした（文字コード/形式不明）: {last_err}")


def _to_number(value):
    """ "1,460" や " 930 " を 1460/930 に変換。空欄は0。"""
    if value is None:
        return 0
    s = str(value).strip().replace(",", "")
    if s == "" or s == "-" or s == "－":
        return 0
    try:
        f = float(s)
        return int(f) if f == int(f) else f
    except ValueError:
        return 0


def _normalize_size(value):
    """ "60サイズ" -> "60"、"3cm以内" はそのまま。前後空白除去。"""
    s = str(value).strip()
    m = re.fullmatch(r"(\d+)\s*サイズ", s)
    return m.group(1) if m else s


def parse_shipping_table_csv(file_bytes, areas):
    """
    送料表CSVを取り込み、[{配送業者,配送区分,サイズ, 各エリア:運賃}] を返す。
    想定ヘッダ: 配送業者,配送区分,サイズ,<地域...>
    地域列は areas（送料表マスタの地域順）に存在するものだけ採用する。
    """
    df = read_csv_auto(file_bytes)
    cols = {c.strip(): c for c in df.columns}
    required = ["配送業者", "配送区分", "サイズ"]
    missing = [c for c in required if c not in cols]
    if missing:
        raise ValueError(
            f"必須列が見つかりません: {', '.join(missing)} / 実際の列: {list(df.columns)}")

    rows = []
    for _, r in df.iterrows():
        size = _normalize_size(r[cols["サイズ"]])
        if not size:
            continue
        rec = {
            "配送業者": str(r[cols["配送業者"]]).strip(),
            "配送区分": str(r[cols["配送区分"]]).strip(),
            "サイズ": size,
        }
        for area in areas:
            rec[area] = _to_number(r[cols[area]]) if area in cols else 0
        rows.append(rec)
    return rows


def parse_irregular_csv(file_bytes):
    """
    イレギュラー作業のスプレッドシートCSVを取り込む。
    [{日付,時間数,人数,作業項目,作業詳細,備考}]
    列名の表記ゆれ（時間数(h)等）を吸収。合計時間は取込側で再計算するため無視。
    """
    import unicodedata
    df = read_csv_auto(file_bytes)
    cols = {unicodedata.normalize("NFKC", str(c)).strip(): c for c in df.columns}

    def pick(*cands):
        for c in cands:
            if c in cols:
                return cols[c]
        # 部分一致
        for key, orig in cols.items():
            if any(c in key for c in cands):
                return orig
        return None

    c_date = pick("日付", "日付")
    c_hours = pick("時間数(h)", "時間数", "時間")
    c_people = pick("人数")
    c_item = pick("作業項目", "項目")
    c_detail = pick("作業詳細", "詳細")
    c_note = pick("備考")

    rows = []
    for _, r in df.iterrows():
        date = str(r[c_date]).strip() if c_date else ""
        item = str(r[c_item]).strip() if c_item else ""
        hours = _to_number(r[c_hours]) if c_hours else 0
        people = _to_number(r[c_people]) if c_people else 0
        if not date and not item and not hours:
            continue
        rows.append({
            "日付": date,
            "時間数": hours,
            "人数": people if people else 1,
            "作業項目": item,
            "作業詳細": str(r[c_detail]).strip() if c_detail else "",
            "備考": str(r[c_note]).strip() if c_note else "",
        })
    return rows


def parse_size_rate_csv(file_bytes):
    """
    配送種別単価CSVを取り込み、[{配送種別, 出荷作業料, 資材費}] を返す。
    想定ヘッダ: 配送種別,出荷作業料,資材費（列名の表記ゆれを多少吸収）。
    """
    df = read_csv_auto(file_bytes)
    cols = {c.strip(): c for c in df.columns}

    def pick(*cands):
        for c in cands:
            if c in cols:
                return cols[c]
        return None

    c_type = pick("配送種別", "種別", "サイズ", "配送区分")
    c_ship = pick("出荷作業料", "出荷作業", "出荷作業単価")
    c_mat = pick("資材費", "資材", "資材単価")
    if c_type is None:
        raise ValueError(f"配送種別の列が見つかりません。実際の列: {list(df.columns)}")

    rows = []
    for _, r in df.iterrows():
        t = _normalize_size(r[c_type])
        if not t:
            continue
        rows.append({
            "配送種別": t,
            "出荷作業料": _to_number(r[c_ship]) if c_ship else 0,
            "資材費": _to_number(r[c_mat]) if c_mat else 0,
        })
    return rows
