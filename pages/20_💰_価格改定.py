# -*- coding: utf-8 -*-
"""
価格改定（納品価格変更・直送価格＆送料変更・梱包サイズ変更）

インプットCSV（JAN・新下代）から新販売価格を計算し、
楽天RMS・Yahoo!ショッピング・ネクストエンジンの価格更新CSVを出力する。
計算ロジックはGoogleスプレッドシート「パピー納品価格変更」の数式を再現（lib/pricing/calc.py）。
突合〜ルール適用は lib/pricing/pipeline.py（Streamlit非依存・テスト共用）。
出力形式は実際のアップロード実績ファイルに一致（lib/pricing/export.py）。
"""
import os

import pandas as pd
import streamlit as st

st.set_page_config(page_title="価格改定", page_icon="💰", layout="wide")

from lib.auth import require_role
require_role("pricing")  # 認証ゲート（AUTH_ENABLED=false なら素通り）

st.title("💰 価格改定")
st.caption("インプットCSV（JAN・新下代）→ 楽天・Yahoo・ネクストエンジンの価格更新CSVを作ります。"
           "アップロード（本番反映）は必ず内容を確認してから手動で行ってください。")

from lib.invoice import csv_import, drive_master
from lib.pricing import calc, export as ex, masters, pipeline, rakuten_price

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")

product_folder = st.secrets.get(
    "PRODUCT_MASTER_FOLDER_ID", "1pQJgn7tYX0KF4x70WY6mlOiruZWPInd-")


# ══ サイドバー: 計算パラメータ ══════════════════════════════
params = dict(calc.DEFAULT_PARAMS)
with st.sidebar:
    st.markdown("### ⚙️ 計算パラメータ")
    st.caption("この画面でのみ有効。恒久変更は lib/pricing/calc.py の DEFAULT_PARAMS を修正。")
    target_margin = st.number_input("目標利益率(%)", 0.0, 90.0, 20.0, 1.0,
                                    help="新価格 = 変動費 ÷ (1 − 目標利益率)")
    params["target_cost_ratio"] = 1 - target_margin / 100.0
    params["fee_rate"] = st.number_input("楽天手数料率(%)", 0.0, 30.0, 10.0, 0.5) / 100.0
    params["free_ship_line"] = st.number_input("送料込みライン(円)", 0, 100000, 3980, 10,
                                               help="この金額以上は送料込み扱い（利益計算での加算なし）")
    params["takuhai_add"] = st.number_input("宅配便の込み換算加算(円)", 0, 10000, 880, 10)
    params["mail_add"] = st.number_input("メール便の込み換算加算(円)", 0, 10000, 350, 10)
    params["margin_warn"] = st.number_input("利益率の警告ライン(%)", 0.0, 50.0, 10.0, 1.0) / 100.0


# ══ 送料・資材マスタ（Drive保存・画面で追加/削除/編集） ══════

def _init_cost_master():
    """session → Drive → スプレッドシート → 同梱CSV の順で初期化。"""
    if "pricing_cost_df" in st.session_state:
        return
    df, src = None, ""
    if product_folder:
        try:
            df = masters.load_cost_master_drive(product_folder)
            if df is not None:
                src = "Drive保存版（この画面で編集・保存）"
        except Exception as e:  # noqa: BLE001
            st.caption(f"（Driveの送料マスタ読込をスキップ: {e}）")
    if df is None:
        sheet_id = st.secrets.get("PRICING_SHEET_ID", "")
        if sheet_id:
            try:
                df = masters.load_cost_master_from_sheet(sheet_id)
                src = "スプレッドシートから初期取込（まだDrive未保存）"
            except Exception as e:  # noqa: BLE001
                st.caption(f"（シートからの取込をスキップ: {e}）")
    if df is None:
        df = masters.load_cost_master_bundled()
        src = "同梱CSV（初期値・まだDrive未保存）"
    st.session_state["pricing_cost_df"] = df
    st.session_state["pricing_cost_src"] = src


_init_cost_master()
cost_df = st.session_state["pricing_cost_df"]

with st.expander("🚚 送料・資材マスタ（行の追加・削除・編集ができます）", expanded=False):
    st.caption(f"取得元: {st.session_state['pricing_cost_src']}。"
               "表を直接編集し、行の追加（最下行）・削除（行選択→ゴミ箱）ができます。"
               "**変更したら「Driveに保存」を押してください**（次回から保存版を自動で読み込みます）。")
    edited_cost = st.data_editor(
        cost_df, key="cost_editor", num_rows="dynamic",
        use_container_width=True, hide_index=True,
        column_config={
            "項目1": st.column_config.TextColumn("項目1（サイズコード）", required=True),
            "送料": st.column_config.NumberColumn("送料(円)"),
            "資材": st.column_config.NumberColumn("資材(円)"),
            "配送種別": st.column_config.SelectboxColumn("配送種別", options=["宅配便", "メール便"]),
        })
    c1, c2, c3 = st.columns(3)
    if c1.button("💾 Driveに保存", key="cost_save", type="primary"):
        try:
            masters.save_cost_master_drive(edited_cost, product_folder)
            st.session_state["pricing_cost_df"] = edited_cost
            st.session_state["pricing_cost_src"] = "Drive保存版（この画面で編集・保存）"
            st.success("送料・資材マスタをDriveに保存しました。")
            st.rerun()
        except Exception as e:  # noqa: BLE001
            st.error(f"Drive保存に失敗しました: {e}")
    if c2.button("📥 スプレッドシートから取り込み直す", key="cost_reimport",
                 help="「送料表」「費用」タブの現在値を取り込みます（保存するまでDriveには反映されません）"):
        sheet_id = st.secrets.get("PRICING_SHEET_ID", "")
        if not sheet_id:
            st.error("Secrets に PRICING_SHEET_ID が未設定です。")
        else:
            try:
                st.session_state["pricing_cost_df"] = masters.load_cost_master_from_sheet(sheet_id)
                st.session_state["pricing_cost_src"] = "スプレッドシートから再取込（まだDrive未保存）"
                st.rerun()
            except Exception as e:  # noqa: BLE001
                st.error(f"シートからの取込に失敗: {e}")
    if c3.button("🔄 Driveの保存版を読み直す", key="cost_reload"):
        for k in ("pricing_cost_df", "pricing_cost_src"):
            st.session_state.pop(k, None)
        st.rerun()

# 計算にはこの画面での編集内容を即時反映（保存前でも有効）
cost_table = masters.cost_lookup(edited_cost)


# ══ NE商品マスタ（汎用マスタ変換と共通） ════════════════════

ne_df = st.session_state.get("pricing_ne_df")
ne_meta = st.session_state.get("pricing_ne_meta", "")
ne_missing = st.session_state.get("pricing_ne_missing", [])

if ne_df is None:
    candidates = []
    if product_folder:
        try:
            f = drive_master.find_latest(product_folder, "master")
            if f:
                mdf, missing = masters.load_ne_master(drive_master.download_bytes(f["id"]))
                candidates.append((mdf, missing, f"Drive保存版 {f['name']}（汎用と共通・{len(mdf):,}件）"))
        except Exception as e:  # noqa: BLE001
            st.caption(f"（DriveのNE商品マスタ読込をスキップ: {e}）")
    try:
        mdf, missing = masters.load_repo_master(_REPO_ROOT)
        if mdf is not None:
            candidates.append((mdf, missing, f"共有master.csv（汎用と共通・{len(mdf):,}件）"))
    except Exception as e:  # noqa: BLE001
        st.caption(f"（共有master.csvの読込をスキップ: {e}）")
    # 売価・原価が揃っているものを優先。無ければ先頭（Drive優先）を使う
    chosen = next(((d, m, meta) for d, m, meta in candidates
                   if "売価" not in m and "原価" not in m), None)
    if chosen is None and candidates:
        chosen = candidates[0]
    if chosen:
        ne_df, ne_missing, ne_meta = chosen
        st.session_state["pricing_ne_df"] = ne_df
        st.session_state["pricing_ne_meta"] = ne_meta
        st.session_state["pricing_ne_missing"] = ne_missing

with st.expander("📚 NE商品マスタ（汎用マスタ変換と共通・毎回アップ不要）", expanded=(ne_df is None)):
    st.caption("汎用マスタ変換・請求書発行と同じ商品マスタを使います。"
               "価格改定には **商品コード・JANコード・売価・原価・項目1** の列が必要です。"
               "ここでアップロードするとDriveにバックアップされ（master_日付_版数.csv）、"
               "他のページからも最新版として参照されます。"
               "**原価（旧下代）はNEカスタムの項目に含めてアップしてください。**")
    if product_folder:
        st.link_button("📁 商品マスタのDriveフォルダを開く",
                       f"https://drive.google.com/drive/folders/{product_folder}",
                       use_container_width=True)
    if ne_df is not None:
        st.success(f"NE商品マスタ利用中: {ne_meta}")
        if ne_missing:
            st.warning("このマスタには次の列がありません: " + "・".join(ne_missing) + "。"
                       "**売価・原価が無いと価格計算ができません。** "
                       "次回マスタ更新時は、NEカスタムのダウンロード項目に売価・原価も含めてください。")
    else:
        st.info("NE商品マスタが見つかりません。NEカスタムCSVをアップロードしてください。")
    if st.button("🔄 マスタを再取得（最新を読み直す）", key="pricing_ne_reload"):
        for k in ("pricing_ne_df", "pricing_ne_meta", "pricing_ne_missing"):
            st.session_state.pop(k, None)
        st.rerun()
    new_master = st.file_uploader("NEカスタム（商品マスタ）CSVをアップロード／差し替え",
                                  type=["csv"], key="pricing_ne_upload")
    if new_master is not None:
        data = new_master.getvalue()
        try:
            mdf, missing = masters.load_ne_master(data)
        except Exception as e:  # noqa: BLE001
            st.error(f"NE商品マスタCSVの読込に失敗: {e}")
            mdf = None
        if mdf is not None:
            ne_df, ne_missing = mdf, missing
            ne_meta = f"アップロード版（{len(mdf):,}件）"
            st.session_state["pricing_ne_df"] = mdf
            st.session_state["pricing_ne_meta"] = ne_meta
            st.session_state["pricing_ne_missing"] = missing
            st.success(f"NE商品マスタを更新しました（{len(mdf):,}件）。")
            if product_folder:
                try:
                    bn = drive_master.upload_versioned(data, "master", product_folder)
                    st.caption(f"Driveにバックアップしました（{bn}・汎用/請求書と共通の最新版になります）。")
                except Exception as e:  # noqa: BLE001
                    st.warning(f"⚠️ Driveバックアップに失敗しました（今回のセッションでは利用可能）: {e}")

if ne_df is not None:
    jan_map, code_info = masters.build_lookup(ne_df)
else:
    jan_map, code_info = {}, {}


# ══ 楽天SKU対応表（RMS商品一括DLから作成・Drive保存） ═══════

sku_df = st.session_state.get("pricing_sku_df")
if sku_df is None and product_folder:
    try:
        sku_df = masters.load_sku_master_drive(product_folder)
        if sku_df is not None:
            st.session_state["pricing_sku_df"] = sku_df
    except Exception:  # noqa: BLE001
        pass

with st.expander("🔴 楽天SKU対応表（NE商品コード → 商品管理番号・SKU管理番号）", expanded=False):
    st.caption("楽天の価格CSV（normal-item.csv）はSKU形式で、**SKU管理番号（楽天側の採番）**が必要です。"
               "RMSの**商品一括ダウンロードCSV**をここにアップすると対応表を作ってDriveに保存します。"
               "対応表に無い枝番付き商品（-01等）は楽天CSVから除外して警告します"
               "（枝番なしの単品はそのまま出力できます）。")
    if sku_df is not None:
        st.success(f"楽天SKU対応表 利用中（{len(sku_df):,}SKU）")
    else:
        st.info("SKU対応表が未保存です。RMSの商品一括ダウンロードCSVをアップしてください。")
    new_sku = st.file_uploader("RMS商品一括ダウンロードCSV（normal-item.csv形式）",
                               type=["csv"], key="pricing_sku_upload")
    if new_sku is not None:
        try:
            parsed = masters.parse_rakuten_item_csv(new_sku.getvalue())
            st.session_state["pricing_sku_df"] = parsed
            sku_df = parsed
            st.success(f"SKU対応表を更新しました（{len(parsed):,}SKU）。")
            if product_folder:
                try:
                    masters.save_sku_master_drive(parsed, product_folder)
                    st.caption(f"Driveに保存しました（{masters.RAKUTEN_SKU_MASTER_NAME}・次回から自動読込）。")
                except Exception as e:  # noqa: BLE001
                    st.warning(f"⚠️ Drive保存に失敗（今回のセッションでは利用可能）: {e}")
        except Exception as e:  # noqa: BLE001
            st.error(f"SKU対応表の作成に失敗: {e}")

sku_table = masters.sku_lookup(sku_df) if sku_df is not None else {}


# ══ 画面部品 ════════════════════════════════════════════════

def rakuten_price_controls(matched, key):
    """楽天から現在販売価格を取得するボタン＋状態表示。取得済み価格の辞書を返す。

    楽天販売価格は楽天でしか管理していないため、RMS Item API 2.0でSKU単位で取得する。
    未取得・未設定の場合はNE売価×1.1で計算する（結果表の「価格取得元」列で区別できる）。
    """
    codes = [info["商品コード"] for _, info in matched]
    cache = st.session_state.setdefault("pricing_rk_prices", {})
    have = sum(1 for c in codes if c.lower() in cache)
    c1, c2 = st.columns([1, 2])
    if c1.button("📡 楽天から現在価格を取得", key=key + "_rkfetch",
                 disabled=not rakuten_price.is_configured(),
                 help="RMS Item API 2.0でSKU単位の販売価格を取得します"):
        pairs = rakuten_price.resolve_pairs(codes, sku_table)
        parents = list(dict.fromkeys(p for p, _ in pairs.values()))
        bar = st.progress(0.0, text=f"楽天から取得中… 0/{len(parents)}商品")
        sku_prices, errors, warnings = rakuten_price.fetch_sku_prices(
            parents,
            on_progress=lambda done, total: bar.progress(
                done / max(total, 1), text=f"楽天から取得中… {done}/{total}商品"))
        bar.empty()
        cache.update(rakuten_price.prices_by_code(codes, sku_table, sku_prices))
        for w in warnings:
            st.warning(w)
        if errors:
            st.warning(f"取得できなかった商品 {len(errors)}件: "
                       + ", ".join(list(errors)[:10]) + (" …" if len(errors) > 10 else ""))
        st.rerun()
    if not rakuten_price.is_configured():
        c2.caption("RMSキー（RMS_SERVICE_SECRET / RMS_LICENSE_KEY）未設定のため取得不可。"
                   "NE売価×1.1で計算します。")
    else:
        c2.caption(f"楽天価格 取得済み {have}/{len(codes)}件。未取得分はNE売価×1.1で計算します"
                   "（結果表の「価格取得元」列で確認できます）。")
    return cache


def show_unmatched(unmatched):
    if unmatched:
        st.error(f"⚠️ NE商品マスタに存在しない行が {len(unmatched)} 件あります（計算から除外）")
        st.dataframe(pd.DataFrame({"未マッチ": unmatched}),
                     use_container_width=True, hide_index=True)
        st.warning("NE商品マスタが古い可能性があります。上の「NE商品マスタ」から最新版に差し替えてください。")


def editable_result(df, key):
    """新販売価格だけ編集可能な結果テーブル。編集は overrides に保存して再計算・再描画。"""
    disabled = [c for c in df.columns if c != "新販売価格"]
    edited = st.data_editor(
        df, key=key + "_editor", use_container_width=True, hide_index=True, disabled=disabled,
        column_config={
            "新販売価格": st.column_config.NumberColumn("新販売価格（手修正可）", step=1),
            "新利益率": st.column_config.NumberColumn(format="percent"),
            "旧利益率": st.column_config.NumberColumn(format="percent"),
        })
    overrides = st.session_state.setdefault(key + "_overrides", {})
    changed = False
    for i in range(len(df)):
        new_v = calc.to_number(edited.iloc[i]["新販売価格"], 0)
        if new_v != calc.to_number(df.iloc[i]["新販売価格"], 0):
            overrides[str(edited.iloc[i]["商品コード"])] = new_v
            changed = True
    if changed:
        st.rerun()  # 手修正を利益額・利益率に反映して描き直す
    return edited


def download_buttons(result_df, key_prefix, include_unchanged):
    """楽天・Yahoo・NE・明細の4ダウンロードボタン。"""
    ok = result_df[result_df["新販売価格"].notna() & (result_df["新販売価格"] > 0)]
    changed = ok if include_unchanged else ok[ok["新販売価格"] != ok["現販売価格"]]
    mall_rows = [{"商品コード": r["商品コード"],
                  "楽天販売価格": r["新販売価格"], "Yahoo販売価格": r["新販売価格"]}
                 for _, r in changed.iterrows()]
    ne_rows = [{"商品コード": r["商品コード"], "NE売価": r["NE売価"], "NE原価": r["新下代"]}
               for _, r in ok.iterrows()]

    rak_records, rak_missing = ex.rakuten_rows(mall_rows, sku_table)
    yah_records, yah_diff = ex.yahoo_rows(mall_rows, sku_table)
    if rak_missing:
        st.warning(f"🔴 楽天CSVから {len(rak_missing)}件 を除外しました"
                   f"（SKU対応表に無い枝番付き商品）: {', '.join(rak_missing[:10])}"
                   f"{' …' if len(rak_missing) > 10 else ''}　"
                   "→ 上の「楽天SKU対応表」に最新のRMS商品一括DLをアップしてください。")
    if yah_diff:
        st.caption(f"🟡 Yahooは親コード単位のため、SKUで価格が割れた {len(yah_diff)}件 は最高値を採用: "
                   f"{', '.join(yah_diff[:10])}{' …' if len(yah_diff) > 10 else ''}")

    st.caption(f"モール向け: {len(mall_rows)}件（価格変更あり{'＋据え置き' if include_unchanged else 'のみ'}）"
               f" ／ NE向け: {len(ne_rows)}件（原価更新のため据え置きも含む）")
    c1, c2, c3, c4 = st.columns(4)
    c1.download_button("🔴 楽天 normal-item.csv", ex.rakuten_csv(rak_records),
                       "normal-item.csv", "text/csv",
                       key=f"{key_prefix}_dl_rakuten", disabled=not rak_records,
                       use_container_width=True)
    c2.download_button("🟡 Yahoo data.csv", ex.yahoo_csv(yah_records),
                       "yahoo_data.csv", "text/csv",
                       key=f"{key_prefix}_dl_yahoo", disabled=not yah_records,
                       use_container_width=True)
    c3.download_button("🟢 NE商品マスタ更新CSV", ex.ne_csv(ne_rows),
                       "ne_price_update.csv", "text/csv",
                       key=f"{key_prefix}_dl_ne", disabled=not ne_rows,
                       use_container_width=True)
    c4.download_button("📄 計算明細CSV", ex.detail_csv(result_df),
                       "price_detail.csv", "text/csv",
                       key=f"{key_prefix}_dl_detail", use_container_width=True)


def result_section(rows, key_prefix):
    """結果テーブル＋警告＋ダウンロード一式。"""
    df = pd.DataFrame(rows)
    ng = df[df["警告"].astype(str) != ""]
    if len(ng):
        st.warning(f"⚠️ 警告あり {len(ng)}件（表の「警告」列を確認してください）")
    df_view = editable_result(df, key_prefix)
    up = df_view[df_view["新販売価格"].notna() & (df_view["新販売価格"] > df_view["現販売価格"])]
    keep = df_view[df_view["新販売価格"] == df_view["現販売価格"]]
    c1, c2, c3 = st.columns(3)
    c1.metric("値上げ", f"{len(up)}件")
    c2.metric("据え置き", f"{len(keep)}件")
    c3.metric("計算不可・要確認", f"{len(df_view) - len(up) - len(keep)}件")
    include_unchanged = st.checkbox("据え置き行もモールCSVに含める", value=False,
                                    key=f"{key_prefix}_inc_unchanged")
    download_buttons(df_view, key_prefix, include_unchanged)


# ══ タブ ════════════════════════════════════════════════════
tab1, tab2, tab3 = st.tabs(["📦 納品価格変更", "🚛 直送価格＆送料変更", "📐 梱包サイズ変更"])

# ── タブ1: 納品価格変更（本流） ─────────────────────────────
with tab1:
    st.markdown("##### 入力CSV: **JAN（またはNE商品コード）・新下代** ＋ 任意列（指定価格／値上げ率／項目1）")
    st.caption("値下げ→据え置き、値上げ→「利益20%確保価格」と「値上げ率価格（現価格×新下代÷旧下代）」の高い方。"
               "旧下代はNE商品マスタの原価を使います。")

    prefill = st.session_state.get("pricing_tab1_prefill")
    if prefill is not None:
        st.info(f"📐 梱包サイズ変更タブから {len(prefill)}件 を引き継いでいます（利益NG品の価格再設定）。")
        if st.button("引き継ぎを解除してCSVアップロードに戻す", key="t1_clear_prefill"):
            st.session_state.pop("pricing_tab1_prefill", None)
            st.rerun()

    up1 = st.file_uploader("価格変更の入力CSV", type=["csv"], key="t1_upload",
                           disabled=prefill is not None)
    in_df = None
    if prefill is not None:
        in_df = prefill
    elif up1 is not None:
        try:
            in_df = csv_import.read_csv_auto(up1.getvalue())
        except Exception as e:  # noqa: BLE001
            st.error(f"CSVの読込に失敗: {e}")

    if in_df is not None and ne_df is None:
        st.error("先に上の「NE商品マスタ」からNEカスタムCSVをアップロードしてください。")
    elif in_df is not None:
        c_jan = pipeline.pick_col(in_df, "JANコード", "JAN", "jan")
        c_code = pipeline.pick_col(in_df, "商品コード")
        c_cost = pipeline.pick_col(in_df, "新下代", "下代", "仕入価格", "納品価格", "新仕入")
        c_fixed = pipeline.pick_col(in_df, "指定価格")
        c_pct = pipeline.pick_col(in_df, "値上げ率")
        c_size = pipeline.pick_col(in_df, "新項目1", "項目1", "新サイズ")
        if not c_jan and not c_code:
            st.error(f"JAN列（または商品コード列）が見つかりません。実際の列: {list(in_df.columns)}")
        elif not c_cost:
            st.error(f"新下代の列が見つかりません（新下代／下代／仕入価格）。実際の列: {list(in_df.columns)}")
        else:
            st.caption(f"列の割り当て: JAN={c_jan or '－'} / 商品コード={c_code or '－'} / 新下代={c_cost}"
                       f" / 指定価格={c_fixed or '－'} / 値上げ率={c_pct or '－'} / 項目1上書き={c_size or '－'}")
            force = st.checkbox("下代が同じ・値下げでも利益20%価格に再設定する（サイズ変更由来など）",
                                value=prefill is not None, key="t1_force")
            matched, unmatched = pipeline.match_input(in_df, c_code, c_jan, jan_map, code_info)
            show_unmatched(unmatched)
            if matched:
                cur_prices = rakuten_price_controls(matched, "t1")
                rows = pipeline.build_price_rows(
                    matched, c_cost, cost_table, params, mode="normal",
                    c_fixed=c_fixed, c_pct=c_pct, c_size=c_size, force_reprice=force,
                    overrides=st.session_state.get("t1_result_overrides"),
                    cur_prices=cur_prices)
                result_section(rows, "t1_result")

# ── タブ2: 直送価格＆送料変更 ───────────────────────────────
with tab2:
    st.markdown("##### 入力CSV: **JAN（またはNE商品コード）・新下代・新送料**")
    st.caption("直送品: 資材0円・送料は入力値・送料込み換算なし（利益計算価格=現販売価格）。"
               "価格ルールは納品価格変更と同じです。")
    up2 = st.file_uploader("直送価格変更の入力CSV", type=["csv"], key="t2_upload")
    if up2 is not None and ne_df is None:
        st.error("先に上の「NE商品マスタ」からNEカスタムCSVをアップロードしてください。")
    elif up2 is not None:
        try:
            in_df2 = csv_import.read_csv_auto(up2.getvalue())
        except Exception as e:  # noqa: BLE001
            st.error(f"CSVの読込に失敗: {e}")
            in_df2 = None
        if in_df2 is not None:
            c_jan = pipeline.pick_col(in_df2, "JANコード", "JAN", "jan")
            c_code = pipeline.pick_col(in_df2, "商品コード")
            c_cost = pipeline.pick_col(in_df2, "新下代", "下代", "仕入価格")
            c_ship = pipeline.pick_col(in_df2, "新送料", "送料")
            c_fixed = pipeline.pick_col(in_df2, "指定価格")
            c_pct = pipeline.pick_col(in_df2, "値上げ率")
            if not c_jan and not c_code:
                st.error(f"JAN列（または商品コード列）が見つかりません。実際の列: {list(in_df2.columns)}")
            elif not c_cost or not c_ship:
                st.error(f"新下代・新送料の列が必要です。実際の列: {list(in_df2.columns)}")
            else:
                matched, unmatched = pipeline.match_input(in_df2, c_code, c_jan, jan_map, code_info)
                show_unmatched(unmatched)
                if matched:
                    cur_prices = rakuten_price_controls(matched, "t2")
                    rows = pipeline.build_price_rows(
                        matched, c_cost, cost_table, params, mode="direct",
                        c_fixed=c_fixed, c_pct=c_pct, c_ship=c_ship,
                        overrides=st.session_state.get("t2_result_overrides"),
                        cur_prices=cur_prices)
                    result_section(rows, "t2_result")

# ── タブ3: 梱包サイズ変更 ───────────────────────────────────
with tab3:
    st.markdown("##### 入力CSV: **JAN（またはNE商品コード）・新項目1（新サイズ）** ＋ 任意列（楽天販売価格）")
    st.caption("①販売価格チェック（NE売価×1.1＝楽天価格か・楽天販売価格列がある場合のみ） "
               "②利益チェック（新サイズの送料・資材で利益率が警告ライン以上か） "
               "③配送設定修正（宅配便⇔メール便が変わるか）")
    up3 = st.file_uploader("サイズ変更の入力CSV", type=["csv"], key="t3_upload")
    if up3 is not None and ne_df is None:
        st.error("先に上の「NE商品マスタ」からNEカスタムCSVをアップロードしてください。")
    elif up3 is not None:
        try:
            in_df3 = csv_import.read_csv_auto(up3.getvalue())
        except Exception as e:  # noqa: BLE001
            st.error(f"CSVの読込に失敗: {e}")
            in_df3 = None
        if in_df3 is not None:
            c_jan = pipeline.pick_col(in_df3, "JANコード", "JAN", "jan")
            c_code = pipeline.pick_col(in_df3, "商品コード")
            c_size = pipeline.pick_col(in_df3, "新項目1", "新サイズ", "項目1")
            c_rprice = pipeline.pick_col(in_df3, "楽天販売価格", "楽天価格")
            if not c_jan and not c_code:
                st.error(f"JAN列（または商品コード列）が見つかりません。実際の列: {list(in_df3.columns)}")
            elif not c_size:
                st.error(f"新項目1（新サイズ）の列が必要です。実際の列: {list(in_df3.columns)}")
            else:
                matched, unmatched = pipeline.match_input(in_df3, c_code, c_jan, jan_map, code_info)
                show_unmatched(unmatched)
                cur_prices = rakuten_price_controls(matched, "t3") if matched else {}
                rows3 = pipeline.size_change_rows(matched, c_size, c_rprice, cost_table, params,
                                                  cur_prices=cur_prices)
                if rows3:
                    df3 = pd.DataFrame(rows3)
                    st.dataframe(df3, use_container_width=True, hide_index=True,
                                 column_config={"新利益率": st.column_config.NumberColumn(format="percent")})
                    ng = df3[df3["利益チェック"] == "×"]
                    fix = df3[df3["配送設定"] == "要修正"]
                    c1, c2 = st.columns(2)
                    c1.metric("利益NG（価格再設定を推奨）", f"{len(ng)}件")
                    c2.metric("モール配送設定の修正が必要", f"{len(fix)}件")
                    d1, d2 = st.columns(2)
                    item1_rows = [{"商品コード": r["商品コード"], "新項目1": r["新項目1"]}
                                  for _, r in df3.iterrows()]
                    d1.download_button("🟢 NE項目1更新CSV", ex.ne_item1_csv(item1_rows),
                                       "ne_item1_update.csv", "text/csv",
                                       key="t3_dl_ne", use_container_width=True)
                    d2.download_button("📄 チェック結果CSV", ex.detail_csv(df3),
                                       "size_change_check.csv", "text/csv",
                                       key="t3_dl_detail", use_container_width=True)
                    if len(ng):
                        st.markdown("###### 利益NG品の価格再設定")
                        st.caption("下代は変わっていないため、「納品価格変更」タブへ引き継いで"
                                   "利益20%確保価格に再設定します（新サイズの送料・資材で計算）。")
                        if st.button(f"📦 利益NG {len(ng)}件を「納品価格変更」タブへ送る", key="t3_to_t1"):
                            pre = pd.DataFrame([{
                                "商品コード": r["商品コード"],
                                "新下代": code_info.get(str(r["商品コード"]).lower(), {}).get("原価", ""),
                                "項目1": r["新項目1"],
                            } for _, r in ng.iterrows()])
                            st.session_state["pricing_tab1_prefill"] = pre
                            st.success("引き継ぎました。「📦 納品価格変更」タブを開いてください。")
