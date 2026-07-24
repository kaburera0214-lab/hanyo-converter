# -*- coding: utf-8 -*-
"""
商品マスタの共通ストア（全機能で共有・2026-07-16ユーザー確定設計）。

- 正本は Drive（PRODUCT_MASTER_FOLDER_ID）の master_YYYYMMDD_NNN.csv 最新版に一本化。
  GitHubのmaster.csvは更新停止（読み込みにも使わない）。
- アップロードはどのページからでも save_master() → 版数付きで保存＝全機能に即時反映。
  NEからのダウンロードは「全カラム」を推奨し、各機能は必要な列だけを拾う。
- 読み込みは「実行時」に load_master() を呼ぶ。毎回Driveで最新版を確認し、
  同じ版ならセッション内のパース済みデータを再利用する（鮮度と速度の両立）。
"""
import re
import unicodedata

import streamlit as st

from lib.invoice import csv_import, drive_master

DEFAULT_FOLDER = "1pQJgn7tYX0KF4x70WY6mlOiruZWPInd-"
_SS_KEY = "_master_store"

# 列名のゆらぎ → 正規名（NE全カラムDLの表記に追従して増やしてよい）
_COL_ALIASES = {
    "JANコード": ("JANコード", "JAN", "jan", "JANCD", "JANcd"),
    "商品コード": ("商品コード", "商品CD", "商品cd", "syohin_code"),
    "商品名": ("商品名", "商品名称"),
    "原価": ("原価", "仕入原価", "下代", "NE原価", "仕入価格", "genka_tnk", "原価単価"),
    "項目1": ("項目1", "項目１"),
    "先方コード": ("先方コード", "先方商品コード"),
    "ロケーションコード": ("ロケーションコード", "ロケーション", "location"),
}


def folder_id():
    return st.secrets.get("PRODUCT_MASTER_FOLDER_ID", DEFAULT_FOLDER)


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


def _parse_master_name(name):
    """master_YYYYMMDD_NNN.csv / master_auto_YYYYMMDD_NNN.csv → (日付, 版, 出所)。
    末尾の 日付8桁_連番 で新旧を判定する（手動 master_ とAPI自動 master_auto_ を横断）。
    パースできなければ None。"""
    base = name[:-4] if name.lower().endswith(".csv") else name
    m = re.search(r"(\d{8})_(\d+)$", base)
    if not m:
        return None
    origin = "自動(API)" if "auto" in base.lower() else "手動アップ"
    return m.group(1), int(m.group(2)), origin


def _latest_master(folder):
    """手動(master_*)とAPI自動(master_auto_*)を横断し、日付・版が最大のファイルを返す。
    返り値: (meta dict, 出所文字列) ／ 無ければ (None, None)。
    ※find_latestの名前降順ソートだと master_auto_ が常に master_ より後に並び誤判定するため、
      末尾の日付+版をパースして厳密に新しい方を選ぶ。"""
    files = drive_master.list_files(folder, "master_")
    best = None
    for f in files:
        p = _parse_master_name(f["name"])
        if not p:
            continue
        key = (p[0], p[1])
        if best is None or key > best[0]:
            best = (key, f, p[2])
    return (best[1], best[2]) if best else (None, None)


def latest_file():
    """Drive上の最新マスタ（手動/API自動を横断）のメタ情報。無ければNone。表示用。"""
    try:
        f, _origin = _latest_master(folder_id())
        return f
    except Exception:  # noqa: BLE001
        return None


def load_master():
    """
    実行時に最新の商品マスタを取得する（手動アップ master_* とAPI自動 master_auto_* を横断）。
    毎回Driveで最新版を確認し、同じ版ならセッション内のパース済みDataFrameを再利用。
    返り値: (df, meta文字列) ／ 失敗時: (None, 理由)
    """
    try:
        f, origin = _latest_master(folder_id())
    except Exception as e:  # noqa: BLE001
        return None, f"Driveに接続できません: {e}"
    if not f:
        return None, ("Driveに商品マスタ（master_*）が見つかりません。"
                      "「商品マスタを更新する」からアップロードしてください。")
    key = (f["id"], str(f.get("modifiedTime", "")))
    cached = st.session_state.get(_SS_KEY)
    if cached and cached.get("key") == key:
        return cached["df"], cached["meta"]
    raw = drive_master.download_bytes(f["id"])
    df = _norm_columns(csv_import.read_csv_auto(raw))
    if "商品コード" not in df.columns:
        return None, (f"最新マスタ {f['name']} に「商品コード」列がありません。"
                      f"実際の列: {list(df.columns)[:15]}")
    meta = (f"{f['name']}（{origin or '不明'}・{len(df):,}件・"
            f"更新 {str(f.get('modifiedTime', ''))[:10]}）")
    st.session_state[_SS_KEY] = {"key": key, "df": df, "meta": meta, "lookups": {}}
    return df, meta


def save_master(file_bytes):
    """
    商品マスタCSV（NE全カラムDL推奨）を検証し、版数付きでDriveへ保存する。
    どのページから呼んでも同じ保存先＝全機能に即時反映。
    返り値: (df, 保存ファイル名) ／ 検証NGは ValueError
    """
    df = _norm_columns(csv_import.read_csv_auto(file_bytes))
    if "商品コード" not in df.columns:
        raise ValueError(f"「商品コード」列が見つかりません / 実際の列: {list(df.columns)[:15]}")
    name = drive_master.upload_versioned(file_bytes, "master", folder_id())
    st.session_state.pop(_SS_KEY, None)  # 次のload_masterで新版を読み直す
    return df, name


def memo(name, builder):
    """現在読み込んでいるマスタ版に紐づく索引キャッシュ（版が変われば自動で作り直し）。"""
    cached = st.session_state.get(_SS_KEY)
    if cached is None:
        return builder()
    if name not in cached["lookups"]:
        cached["lookups"][name] = builder()
    return cached["lookups"][name]


def jan_dict(df):
    """JANコード→{商品コード, 商品名}（汎用マスタ変換用）。マスタ版ごとにキャッシュ。"""
    def build():
        jd = {}
        if "JANコード" not in df.columns:
            return jd
        jans = df["JANコード"].astype(str).tolist()
        codes = df["商品コード"].astype(str).tolist()
        names = df["商品名"].astype(str).tolist() if "商品名" in df.columns else None
        for i, jan in enumerate(jans):
            j = jan.strip()
            if j and j != "nan" and j not in jd:
                jd[j] = {"商品コード": codes[i].strip(),
                         "商品名": (names[i].strip() if names else "")}
        return jd
    return memo("jan_dict", build)


def supplier_dict(df):
    """先方コード→{商品コード, 商品名}。列が無ければ空（テンプレート管理のため通常は不使用）。"""
    def build():
        sd = {}
        if "先方コード" not in df.columns:
            return sd
        sups = df["先方コード"].astype(str).tolist()
        codes = df["商品コード"].astype(str).tolist()
        names = df["商品名"].astype(str).tolist() if "商品名" in df.columns else None
        for i, sup in enumerate(sups):
            s = sup.strip()
            if s and s != "nan" and s not in sd:
                sd[s] = {"商品コード": codes[i].strip(),
                         "商品名": (names[i].strip() if names else "")}
        return sd
    return memo("supplier_dict", build)


def upload_widget(key):
    """マスタ差し替え用の共通アップローダ。保存に成功したらTrueを返す。"""
    up = st.file_uploader("NE商品マスタCSV（全カラムDL推奨）をアップロード／差し替え",
                          type=["csv"], key=key)
    if up is not None:
        if st.button("📥 商品マスタを更新する", key=key + "_btn", type="primary"):
            try:
                df, name = save_master(up.getvalue())
                st.success(f"商品マスタを更新しました: **{name}**（{len(df):,}件）。"
                           "全ページの次回実行から自動で使われます。")
                return True
            except Exception as e:  # noqa: BLE001
                st.error(f"マスタ更新に失敗しました: {e}")
    return False
