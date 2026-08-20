# -*- coding: utf-8 -*-
"""
価格改定（納品価格変更・直送価格＆送料変更）

インプットCSV（JAN・新下代）から新販売価格を計算し、
楽天RMS・Yahoo!ショッピング・ネクストエンジンへ**APIで直接反映**する（2026-08-20）。
計算ロジックはGoogleスプレッドシート「パピー納品価格変更」の数式を再現（lib/pricing/calc.py）。
突合〜ルール適用は lib/pricing/pipeline.py（Streamlit非依存・テスト共用）。
API反映は lib/pricing/apply.py（同・テスト共用。tests/test_pricing_apply.py）。
出力形式は実際のアップロード実績ファイルに一致（lib/pricing/export.py）。

CSV出力は廃止していない。APIが未設定・失敗した分はCSVアップロードで反映できる安全網として
残してあり、確定した内容は今までどおりDriveに証跡として保存される。
※梱包サイズ変更は「📥 入荷登録」ページ（pages/21）へ移設した（2026-07-21）。
"""
import pandas as pd
import streamlit as st

st.set_page_config(page_title="価格改定", page_icon="💰", layout="wide")

from lib.auth import require_role
require_role("pricing")  # 認証ゲート（AUTH_ENABLED=false なら素通り）

st.title("💰 価格改定")
import datetime as _dt
import os as _os
_build = _dt.datetime.fromtimestamp(_os.path.getmtime(__file__)).strftime("%Y-%m-%d %H:%M")
st.caption("インプットCSV（JAN・新下代）→ 新販売価格を計算し、楽天・Yahoo・ネクストエンジンへ"
           "**APIで直接反映**します（CSVアップロード不要。CSVも従来どおり出せます）。"
           "反映は必ず内容を確認・確定してから実行してください。"
           f"　（app更新: {_build}）")

from lib import master_store
from lib.invoice import csv_import
from lib.ne_api import client as ne_client, usage as ne_usage
from lib.pricing import apply, calc, export as ex, masters, pipeline, rakuten_price
from lib.receiving import yahoo_queue as yq
from lib.yahoo_api import client as yahoo_client

product_folder = master_store.folder_id()


# ══ サイドバー: 計算パラメータ ══════════════════════════════
params = dict(calc.DEFAULT_PARAMS)
with st.sidebar:
    st.markdown("### ⚙️ 計算パラメータ")
    st.caption("この画面でのみ有効。恒久変更は lib/pricing/calc.py の DEFAULT_PARAMS を修正。")
    target_margin = st.number_input("目標利益率(%)", 0.0, 80.0, 15.0, 1.0,
                                    help="新価格はこの利益率にちょうど着地するよう逆算します"
                                         "（送料込みの入金ベース・楽天手数料込み）")
    params["target_margin"] = target_margin / 100.0
    params["fee_rate"] = st.number_input("楽天手数料率(%)", 0.0, 30.0, 10.0, 0.5,
                                         help="送料込みの決済総額に掛かります") / 100.0
    params["free_ship_line"] = st.number_input("送料込みライン(円)", 0, 100000, 3980, 10,
                                               help="この金額以上は送料込み扱い（利益計算での加算なし）")
    params["ship_included_line"] = st.number_input(
        "送料無料維持ライン(本体価格・円)", 0, 100000,
        int(calc.DEFAULT_PARAMS["ship_included_line"]), 10,
        help="本体価格（送料を引いた価格）がこの金額を超える商品は、送料込み・送料無料で"
             "価格を設定します（同時購入を狙える帯）。以下なら従来どおりお客様が送料負担。")
    params["takuhai_add"] = st.number_input("宅配便の込み換算加算(円)", 0, 10000, 880, 10)
    params["mail_add"] = st.number_input("メール便の込み換算加算(円)", 0, 10000, 350, 10)
    params["margin_warn"] = st.number_input("利益率の警告ライン(%)", 0.0, 50.0, 10.0, 1.0) / 100.0


# ══ 送料・資材マスタ（Drive保存・画面で追加/削除/編集） ══════
# スプレッドシートとは紐づけず、この画面だけで管理する（初期値のみ同梱CSV）。

def _init_cost_master():
    """session → Drive保存版 → 同梱CSV（初期値） の順で初期化。"""
    if "pricing_cost_df" in st.session_state:
        return
    df, src = None, ""
    if product_folder:
        try:
            with st.spinner("送料・資材マスタをDriveから読み込み中…（初回のみ）"):
                df = masters.load_cost_master_drive(product_folder)
            if df is not None:
                src = "Drive保存版"
        except Exception as e:  # noqa: BLE001
            st.caption(f"（Driveの送料マスタ読込をスキップ: {e}）")
    if df is None:
        df = masters.load_cost_master_bundled()
        src = "初期値（まだDrive未保存）"
    st.session_state["pricing_cost_df"] = df
    st.session_state["pricing_cost_src"] = src


_init_cost_master()
cost_df = st.session_state["pricing_cost_df"]

with st.expander("🚚 送料・資材マスタ（この画面だけで管理・行の追加・削除・編集）", expanded=False):
    st.caption(f"取得元: {st.session_state['pricing_cost_src']}。"
               "行の追加＝最下段の空行に入力、行の削除＝左端のチェックで行を選び右上のゴミ箱。"
               "**編集したら「更新」を押して保存してください**"
               "（保存前でも、この画面での計算には編集内容が反映されます）。")
    edited_cost = st.data_editor(
        cost_df, key="cost_editor", num_rows="dynamic",
        use_container_width=True, hide_index=True,
        column_config={
            "項目1": st.column_config.TextColumn("項目1（サイズコード）", required=True),
            "送料": st.column_config.NumberColumn("送料(円)"),
            "資材": st.column_config.NumberColumn("資材(円)"),
            "配送種別": st.column_config.SelectboxColumn("配送種別", options=["宅配便", "メール便"]),
        })
    edited_norm = masters.normalize_cost_df(edited_cost)
    changed = not edited_norm.equals(cost_df)
    if changed:
        st.info("未保存の変更があります。「更新」を押すと確定します。")
    if st.button("💾 更新（Driveに保存）", key="cost_save", type="primary",
                 disabled=not changed):
        try:
            masters.save_cost_master_drive(edited_norm, product_folder)
            st.session_state["pricing_cost_df"] = edited_norm
            st.session_state["pricing_cost_src"] = "Drive保存版"
            st.success("送料・資材マスタを更新しました。")
            st.rerun()
        except Exception as e:  # noqa: BLE001
            st.error(f"Driveへの保存に失敗しました: {e}")

# 計算にはこの画面での編集内容を即時反映（保存前でも有効）
cost_table = masters.cost_lookup(edited_norm)


# ══ NE商品マスタ（全機能共通ストア・実行時に最新を自動取得） ═

with st.expander("📚 NE商品マスタ（全機能共通・実行時に最新を自動取得）", expanded=False):
    st.caption("汎用マスタ変換・請求書発行と同じ商品マスタ（Driveの最新版）を、"
               "**計算の実行時に自動で読み込みます**（アップは不要）。"
               "価格改定で使う列は **商品コード・JANコード・原価（旧下代）・項目1**。"
               "売価は使いません（現販売価格は必ず📡楽天から取得します）。")
    _f = master_store.latest_file()
    if _f:
        st.success(f"Driveの最新版: {_f['name']}（更新 {str(_f.get('modifiedTime', ''))[:10]}）")
    else:
        st.info("Driveに商品マスタ（master_*）がありません。下からアップロードしてください。")
    st.link_button("📁 商品マスタのDriveフォルダを開く",
                   f"https://drive.google.com/drive/folders/{master_store.folder_id()}",
                   use_container_width=True)
    if master_store.upload_widget("pricing_master_up"):
        st.rerun()


def get_master_lookup():
    """実行時にDriveの最新商品マスタを読み込み、突合用の索引を返す。
    返り値: (jan_map, code_info)。読めない場合は (None, None) でエラー表示済み。"""
    with st.spinner("商品マスタの最新版を確認中…"):
        ne_df, meta = master_store.load_master()
    if ne_df is None:
        st.error(meta)
        return None, None
    missing = [c for c in ("JANコード", "原価", "項目1") if c not in ne_df.columns]
    if missing:
        msgs = {"JANコード": "JANでの突合ができません",
                "原価": "値上げ/値下げの判定と原価更新ができません",
                "項目1": "送料・資材を引けません"}
        st.warning("マスタに次の列がありません: "
                   + "／".join(f"{c}（{msgs.get(c, '')}）" for c in missing)
                   + "。NEから全カラムでDLしてアップし直してください。")
    st.caption(f"使用マスタ: {meta}")
    return master_store.memo("pricing_lookup", lambda: masters.build_lookup(ne_df))


# ══ 楽天SKU対応表（画面には出さない・📡取得時にRMS APIから自動構築しDrive保存） ═

if "pricing_sku_table" not in st.session_state:
    table = {}
    if product_folder:
        try:
            sku_df = masters.load_sku_master_drive(product_folder)
            if sku_df is not None:
                table = masters.sku_lookup(sku_df)
        except Exception:  # noqa: BLE001
            pass
    st.session_state["pricing_sku_table"] = table

sku_table = st.session_state["pricing_sku_table"]


def _save_sku_table():
    """SKU対応表をDriveへ保存（ベストエフォート）。"""
    if not product_folder:
        return
    try:
        masters.save_sku_master_drive(
            masters.sku_table_to_df(st.session_state["pricing_sku_table"]), product_folder)
    except Exception:  # noqa: BLE001
        pass


# ══ 画面部品 ════════════════════════════════════════════════

def rakuten_price_controls(matched, key):
    """楽天から現在販売価格を取得するボタン＋状態表示。取得済み価格の辞書を返す。

    楽天販売価格は楽天でしか管理していないため、RMS Item API 2.0でSKU単位で取得する。
    同じレスポンスからSKU対応表（楽天CSV出力に必要）も自動構築してDriveに保存する。
    販売価格は楽天でのみ管理しているため、未取得の商品は計算不可になる（NE売価での代用はしない）。
    """
    codes = [info["商品コード"] for _, info in matched]
    cache = st.session_state.setdefault("pricing_rk_prices", {})
    have = sum(1 for c in codes if c.lower() in cache)
    c1, c2 = st.columns([1, 2])
    if c1.button("📡 楽天から現在価格を取得", key=key + "_rkfetch",
                 disabled=not rakuten_price.is_configured(),
                 help="RMS Item API 2.0でSKU単位の販売価格とSKU番号を取得します"):
        bar = st.progress(0.0, text="楽天から取得中…")
        info, errors, warnings = rakuten_price.fetch_for_codes(
            codes, sku_table,
            on_progress=lambda done, total: bar.progress(
                done / max(total, 1), text=f"楽天から取得中… {done}/{total}商品"))
        bar.empty()
        cache.update(rakuten_price.to_prices(info))
        # 取得を試みたコードを記録（取れなかった商品＝楽天未登録の可能性→対象から除外する判定に使う）
        st.session_state.setdefault("pricing_rk_attempted", set()).update(
            str(c).lower() for c in codes)
        st.session_state["pricing_sku_table"].update(rakuten_price.to_sku_table(info))
        _save_sku_table()
        for w in warnings:
            st.warning(w)
        if errors:
            st.warning(f"取得できなかった商品 {len(errors)}件: "
                       + ", ".join(list(errors)[:10]) + (" …" if len(errors) > 10 else ""))
        st.rerun()
    if not rakuten_price.is_configured():
        c2.caption("⚠️ RMSキー（RMS_SERVICE_SECRET / RMS_LICENSE_KEY）未設定のため取得できません。"
                   "現販売価格が無いと計算できません。")
    else:
        c2.caption(f"楽天価格 取得済み {have}/{len(codes)}件。"
                   "**現販売価格は楽天から取得したものだけを使います**（未取得の商品は計算不可）。")
    return cache


def show_unmatched(unmatched):
    if unmatched:
        st.error(f"⚠️ NE商品マスタに存在しない行が {len(unmatched)} 件あります（計算から除外）")
        st.dataframe(pd.DataFrame({"未マッチ": unmatched}),
                     use_container_width=True, hide_index=True)
        st.warning("NE商品マスタが古い可能性があります。上の「NE商品マスタ」から最新版に差し替えてください。")


def exclude_not_on_rakuten(matched, cur_prices, in_df_price_col=None):
    """📡取得を試みても楽天から価格が取れなかった商品＝楽天に未登録の可能性が高いので
    価格変更の対象から除外する。除外した商品は別枠で一覧表示する。"""
    attempted = st.session_state.get("pricing_rk_attempted") or set()
    if not attempted:
        return matched
    target, excluded = [], []
    for r, info in matched:
        code = info["商品コード"].lower()
        has_csv_price = (in_df_price_col is not None
                         and calc.to_number(r[in_df_price_col]) is not None)
        if code in attempted and code not in cur_prices and not has_csv_price:
            excluded.append({"商品コード": info["商品コード"], "商品名": info.get("商品名", "")})
        else:
            target.append((r, info))
    if excluded:
        st.warning(f"🛑 楽天から現在価格を取得できなかった {len(excluded)}件 は"
                   "**楽天に登録されていない可能性が高いため、価格変更の対象から除外**しました"
                   "（CSVにも含まれません）。誤判定と思われる場合は📡「楽天から現在価格を取得」を"
                   "もう一度押すと再判定されます。")
        with st.expander(f"対象外にした商品を見る（{len(excluded)}件）", expanded=False):
            st.dataframe(pd.DataFrame(excluded), use_container_width=True, hide_index=True)
    return target


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


def build_output_files(result_df, include_unchanged, free_shipping=False):
    """結果表 → 出力CSV一式（bytes）。返り値: (files dict, モール件数, NE件数)
    free_shipping=True（直送品・送料込み価格）ならモールCSVに送料無料フラグ列を追加。"""
    # 対象行の判定はAPI直更新（lib/pricing/apply）と共通 ＝ CSVとAPIで中身が食い違わない
    ok, changed = apply.split_targets(result_df, include_unchanged)
    mall_rows = apply.mall_rows_of(changed)
    ne_rows = [{"商品コード": r["商品コード"], "NE売価": r["NE売価"], "NE原価": r["新下代"]}
               for _, r in ok.iterrows()]

    rak_records, rak_missing = ex.rakuten_rows(mall_rows, sku_table)
    yah_records, yah_diff = ex.yahoo_rows(mall_rows, sku_table)
    if rak_missing:
        st.warning(f"🔴 楽天CSVから {len(rak_missing)}件 を除外しました"
                   f"（楽天のSKU番号が未取得の枝番付き商品）: {', '.join(rak_missing[:10])}"
                   f"{' …' if len(rak_missing) > 10 else ''}　"
                   "→ 📡「楽天から現在価格を取得」を押すとSKU番号も自動取得されます。")
    if yah_diff:
        st.caption(f"🟡 Yahooは親コード単位のため、SKUで価格が割れた {len(yah_diff)}件 は最高値を採用: "
                   f"{', '.join(yah_diff[:10])}{' …' if len(yah_diff) > 10 else ''}")

    files = {
        "normal-item.csv": ex.rakuten_csv(rak_records, free_shipping=free_shipping),
        "yahoo_data.csv": ex.yahoo_csv(yah_records, free_shipping=free_shipping),
        "ne_price_update.csv": ex.ne_csv(ne_rows),
        "price_detail.csv": ex.detail_csv(result_df),
    }
    return files, len(mall_rows), len(ne_rows)


# ダウンロードボタンの表示名（ファイル名→ラベル）
_DL_LABELS = {
    "normal-item.csv": "🔴 楽天 normal-item.csv",
    "yahoo_data.csv": "🟡 Yahoo data.csv",
    "ne_price_update.csv": "🟢 NE商品マスタ更新CSV",
    "price_detail.csv": "📄 計算明細CSV",
}


def confirm_gate(files, key_prefix, tab_label, extra_files=None):
    """✅確定 → Driveの「価格改定履歴」へ版数付き保存（YYYYMMDD_連番_タブ名）→ DLボタン表示。

    複数人運用での誤操作対策: 確定するまでCSVは出さない。確定した内容は
    Driveに証跡として残り、確定後に内容を修正した場合は再確定を求める。
    extra_files: Driveには保存するがDLボタンは出さないファイル（入力CSVなどの証跡用）。
    返り値: 今の内容のハッシュ（確定後に内容が変わったかの判定にAPI直更新側でも使う）。
    """
    import hashlib
    cur_hash = hashlib.md5(b"".join(files.values())).hexdigest()

    conf_key = key_prefix + "_confirmed"
    conf = st.session_state.get(conf_key)
    label = "✅ 確定してDriveに保存（CSVを表示）" if conf is None else "✅ 再確定してDriveに保存し直す"
    if st.button(label, key=key_prefix + "_confirm", type="primary",
                 disabled=(conf is not None and conf["hash"] == cur_hash)):
        import datetime
        run_name, url, err = "", "", ""
        try:
            with st.spinner("Driveにバックアップ中…"):
                run_name, run_id = masters.save_run_to_drive(
                    {**files, **(extra_files or {})}, tab_label, product_folder)
            url = f"https://drive.google.com/drive/folders/{run_id}"
        except Exception as e:  # noqa: BLE001
            err = str(e)
        # DLファイル名のユニーク化用サフィックス（版数と一致させる。Drive失敗時は時刻）
        # ※ブラウザの重複リネーム「 (7)」はRMSのファイル名規則（半角英数と-_のみ）で
        #   弾かれるため、最初からユニークな名前で配布する
        if run_name:
            suffix = "_".join(run_name.split("_")[:2])          # 例: 20260717_001
        else:
            suffix = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        st.session_state[conf_key] = {"hash": cur_hash, "files": files,
                                      "run": run_name, "url": url, "err": err,
                                      "suffix": suffix}
        st.rerun()

    if conf is None:
        st.info("内容を確認したら「✅ 確定」を押してください。Driveにバックアップされた後、CSVがダウンロードできます。")
        return cur_hash

    if conf["err"]:
        st.error(f"⚠️ Driveへのバックアップに失敗しました（CSVは下からダウンロードできます）: {conf['err']}")
    else:
        st.success(f"確定済み: **{conf['run']}** としてDriveに保存しました。")
        if conf["url"]:
            st.link_button("📁 バックアップフォルダを開く", conf["url"])
    if conf["hash"] != cur_hash:
        st.warning("⚠️ 確定した後に内容が変わっています。下のCSVは**確定時点の内容**です。"
                   "最新の内容にするには「✅ 再確定」を押してください。")

    saved = conf["files"]
    suffix = conf.get("suffix", "")
    items = list(saved.items())
    for i in range(0, len(items), 4):
        chunk = items[i:i + 4]
        cols = st.columns(4)
        for col, (name, data) in zip(cols, chunk):
            stem, _, ext = name.rpartition(".")
            dl_name = f"{stem}_{suffix}.{ext}" if suffix else name
            col.download_button(_DL_LABELS.get(name, name), data, dl_name, "text/csv",
                                key=f"{key_prefix}_dl_{name}", use_container_width=True)
    return cur_hash


# ══ API直更新（CSVアップロードの代わり） ═══════════════════
# CSVは残したまま。APIが未設定・失敗した分は従来どおりCSVで反映できる（安全網）。

_API_LABELS = {"ne": "🟢 ネクストエンジン（売価・原価）",
               "rakuten": "🔴 楽天（販売価格・表示価格）",
               "yahoo": "🟡 Yahoo（販売価格）"}


def _api_available():
    """各システムのAPIが使える状態か。使えないものはチェックを外して選べなくする。"""
    return {"ne": ne_client.is_configured(),
            "rakuten": rakuten_price.is_configured(),
            "yahoo": yahoo_client.api_enabled()}


def api_apply_section(result_df, key_prefix, tab_label, include_unchanged,
                      stale=False, free_shipping=False):
    """✅確定後に出す「モールへ直接反映」。CSVアップロードの代わりにAPIで書き込む。

    確定していない（＝Driveに証跡が無い）内容は反映させない。確定後に画面の内容を
    変えた場合も、証跡と実際の反映がズレないよう再確定を求める。
    """
    conf = st.session_state.get(key_prefix + "_confirmed")
    if conf is None:
        return

    st.divider()
    st.markdown("#### 🚀 モールへ直接反映（CSVアップロード不要）")
    st.caption("確定した新価格を、各システムのAPIでそのまま書き込みます。"
               "CSVは今までどおり残してあるので、APIを使わない・失敗した分は"
               "CSVアップロードで反映できます。")

    avail = _api_available()
    systems, cols = set(), st.columns(3)
    for col, name in zip(cols, apply.ALL_SYSTEMS):
        on = col.checkbox(_API_LABELS[name], value=avail[name], disabled=not avail[name],
                          key=f"{key_prefix}_api_{name}")
        if not avail[name]:
            col.caption("⚠️ APIが未設定のため選べません（CSVで反映してください）")
        if on:
            systems.add(name)
    if not systems:
        st.info("反映先が選ばれていません。CSVだけで反映する場合はこのままでOKです。")
        return

    tasks, notes = apply.build_tasks(result_df, sku_table, include_unchanged, systems)
    n = apply.task_counts(tasks)
    c1, c2, c3 = st.columns(3)
    c1.metric("NE 更新", f"{n['ne']}件", help="売価・原価。価格が変わらない行も原価更新のため含みます")
    c2.metric("楽天 更新", f"{n['rakuten']}商品",
              help=f"SKU合計 {n['rakuten_sku']}件。商品管理番号ごとにPATCHします")
    c3.metric("Yahoo 更新", f"{n['yahoo']}件", help="親コード単位。更新後に全反映予約を1回呼びます")

    if free_shipping:
        st.warning("⚠️ 直送タブの**送料無料フラグはAPIでは設定できません**（価格のみ反映）。"
                   "送料設定は楽天・YahooのCSV（下のダウンロード）か管理画面で別途反映してください。")
    if notes["rakuten_missing"]:
        st.warning(f"🔴 楽天SKU番号が分からない {len(notes['rakuten_missing'])}件 はAPI対象外です: "
                   + "、".join(notes["rakuten_missing"][:10])
                   + (" …" if len(notes["rakuten_missing"]) > 10 else "")
                   + "　→ 📡「楽天から現在価格を取得」を押すとSKU番号も取得されます。")
    if notes["yahoo_diff"]:
        st.caption(f"🟡 Yahooは親コード単位のため、SKUで価格が割れた {len(notes['yahoo_diff'])}件 は最高値: "
                   + "、".join(notes["yahoo_diff"][:10]))
    if notes["ne_skipped"]:
        st.warning(f"🟢 売価か原価が空のため NE に送れない {len(notes['ne_skipped'])}件: "
                   + "、".join(notes["ne_skipped"][:10]))

    blockers = []
    if stale:
        blockers.append("確定した後に画面の内容が変わっています。"
                        "証跡と実際の反映がズレるので「✅ 再確定」を押してから実行してください。")
    if not any(n[k] for k in ("ne", "rakuten", "yahoo")):
        blockers.append("反映する対象が0件です。")
    for b in blockers:
        st.error("🛑 " + b)

    agree = st.checkbox("上記の内容で**本番データ（ネクストエンジン・楽天・Yahoo）を直接更新**する"
                        "ことを確認しました", key=f"{key_prefix}_api_agree")
    if st.button("🚀 反映を実行", type="primary", key=f"{key_prefix}_api_run",
                 disabled=not agree or bool(blockers)):
        _run_api(tasks, key_prefix, tab_label)
        st.rerun()

    _show_api_result(key_prefix, tab_label)


def _run_api(tasks, key_prefix, tab_label):
    """APIを実行し、結果・失敗分・証跡の保存先をセッションに残す（rerunしても消えないように）。"""
    total = ((1 if tasks.get("ne_price") else 0) + len(tasks.get("rakuten_price") or [])
             + (1 if tasks.get("yahoo_price") else 0))
    bar = st.progress(0.0, text="反映中…")
    done = {"n": 0}

    def on_step(message):
        done["n"] += 1
        bar.progress(min(done["n"] / max(total, 1), 1.0), text=message)

    try:
        results, failed = apply.execute(tasks, on_step=on_step)
    except Exception as e:  # noqa: BLE001（想定外でも画面を壊さず結果として見せる）
        results = [{"ステップ": "実行", "対象": "-", "状態": "失敗",
                    "メッセージ": f"実行中に想定外のエラー: {e}"}]
        failed = {k: v for k, v in tasks.items() if v}
    try:
        ne_usage.flush()
    except Exception:  # noqa: BLE001
        pass

    # Yahooが失敗したら反映待ちキュー（CSV）へ退避する＝APIが壊れていても取りこぼさない。
    # 退避できたら再実行キューからは外す（壊れたAPIを叩き続けないため）。
    yahoo_saved = 0
    if failed.get("yahoo_price"):
        try:
            yahoo_saved = yq.append_prices(
                [{"code": c, "price": p} for c, p in failed["yahoo_price"].items()],
                product_folder)
            failed.pop("yahoo_price")
            for r in results:
                if r.get("ステップ") == apply.STEP_YAHOO and r.get("状態") == "失敗":
                    r["状態"] = "CSV退避"
                    r["メッセージ"] = "APIが失敗したためYahoo反映待ちキューへ退避しました（元エラー: "\
                                   + str(r["メッセージ"]) + "）"
        except Exception as e:  # noqa: BLE001
            results.append({"ステップ": apply.STEP_YAHOO, "対象": "キュー退避", "状態": "失敗",
                            "メッセージ": f"CSVキューへの退避にも失敗: {e}"})

    bar.progress(1.0, text="証跡を保存中…")
    run_name, url, err = "", "", ""
    try:
        run_name, run_id = masters.save_run_to_drive(
            {"api_result.csv": ex.detail_csv(pd.DataFrame(results))},
            tab_label + "_API反映", product_folder)
        url = f"https://drive.google.com/drive/folders/{run_id}"
    except Exception as e:  # noqa: BLE001
        err = str(e)
    bar.empty()
    st.session_state[key_prefix + "_api_result"] = {
        "results": results, "failed": failed, "run": run_name, "url": url,
        "err": err, "yahoo_saved": yahoo_saved}


def _show_api_result(key_prefix, tab_label):
    """直近の反映結果（行×ステップ）と、失敗した分だけの再実行ボタン。"""
    state = st.session_state.get(key_prefix + "_api_result")
    if not state:
        return
    results, failed = state["results"], state["failed"]
    n_ok, n_ng, n_skip = apply.summarize(results)

    st.markdown("##### 反映結果")
    c1, c2, c3 = st.columns(3)
    c1.metric("成功", f"{n_ok}件")
    c2.metric("失敗", f"{n_ng}件")
    c3.metric("スキップ", f"{n_skip}件")
    st.dataframe(pd.DataFrame(results), use_container_width=True, hide_index=True)
    st.caption("表示は**直近の実行分**です（再実行するとこの表は入れ替わります）。"
               "各実行の結果はDriveに `api_result.csv` として1回ぶんずつ残ります。")

    if state["yahoo_saved"]:
        st.info(f"🟡 Yahooは反映待ちキューへ退避しました（キュー全体で {state['yahoo_saved']}件）。"
                "「📥 入荷登録」ページのYahooキューからCSVをまとめてアップしてください。")
    if state["err"]:
        st.warning(f"実行結果のDrive保存に失敗しました（反映自体は上の表のとおり）: {state['err']}")
    elif state["run"]:
        st.caption(f"実行結果を **{state['run']}** としてDriveに保存しました。")
        if state["url"]:
            st.link_button("📁 実行結果フォルダを開く", state["url"])

    if apply.has_auth_error(results):
        st.error("🔑 認証切れが含まれています。「📥 入荷登録」ページの🔐から"
                 "ネクストエンジン／Yahooを再認可してから、下の「失敗した分だけ再実行」を押してください。")

    if failed:
        n_failed = (len(failed.get("ne_price") or []) + len(failed.get("rakuten_price") or [])
                    + len(failed.get("yahoo_price") or {}))
        if st.button(f"🔁 失敗した分だけ再実行（{n_failed}件）", key=f"{key_prefix}_api_retry"):
            _run_api(failed, key_prefix, tab_label)
            st.rerun()
    elif n_ng == 0 and not state["yahoo_saved"]:
        st.success("✅ すべて反映しました。CSVのアップロードは不要です。")


def confirm_and_download(result_df, key_prefix, tab_label, include_unchanged,
                         input_file=None, free_shipping=False):
    """価格変更タブ用: 出力CSVを組み立てて確定ゲートへ。input_file=(名前, bytes)は証跡として一緒に保存。"""
    files, mall_n, ne_n = build_output_files(result_df, include_unchanged, free_shipping)
    st.caption(f"モール向け: {mall_n}件（価格変更あり{'＋変わらない行' if include_unchanged else 'のみ'}）"
               f" ／ NE向け: {ne_n}件（原価更新のため価格が変わらない行も含む）")
    extra = {f"input_{input_file[0]}": input_file[1]} if input_file else None
    cur_hash = confirm_gate(files, key_prefix, tab_label, extra_files=extra)
    conf = st.session_state.get(key_prefix + "_confirmed")
    api_apply_section(result_df, key_prefix, tab_label, include_unchanged,
                      stale=bool(conf and conf["hash"] != cur_hash),
                      free_shipping=free_shipping)


def input_file_of(uploaded, prefill=None, prefill_name="サイズ変更引き継ぎ.csv"):
    """バックアップ用の入力CSV（名前, bytes）。アップロードが無ければ引き継ぎ内容をCSV化。"""
    if uploaded is not None:
        return (uploaded.name, uploaded.getvalue())
    if prefill is not None:
        return (prefill_name, prefill.to_csv(index=False).encode("utf-8-sig"))
    return None


def result_section(rows, key_prefix, tab_label, input_file=None, free_shipping=False):
    """結果テーブル＋警告フィルタ＋確定・ダウンロード一式。"""
    df = pd.DataFrame(rows)
    # 警告は見つけやすいように商品名の直後に置く
    lead = ["商品コード", "商品名", "警告"]
    df = df[lead + [c for c in df.columns if c not in lead]]

    ng = df[df["警告"].astype(str) != ""]
    only_warn = False
    if len(ng):
        c1, c2 = st.columns([2, 3])
        c1.warning(f"⚠️ 警告あり {len(ng)}件")
        only_warn = c2.checkbox(f"⚠️ 警告のある行だけ表示（{len(ng)}件）",
                                key=f"{key_prefix}_only_warn")
    df_show = ng if only_warn else df
    editable_result(df_show, key_prefix)
    if only_warn:
        st.caption("※表示を絞っているだけで、集計・CSVには全行が含まれます。")

    # 集計・CSVは常に全行ベース（手修正はoverrides経由で全行に反映済み）
    up = df[df["新販売価格"].notna() & (df["新販売価格"] > df["現販売価格"])]
    down = df[df["新販売価格"].notna() & (df["新販売価格"] < df["現販売価格"])]
    keep = df[df["新販売価格"] == df["現販売価格"]]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("値上げ", f"{len(up)}件")
    c2.metric("値下げ", f"{len(down)}件")
    c3.metric("変わらず", f"{len(keep)}件")
    c4.metric("計算不可・要確認", f"{len(df) - len(up) - len(down) - len(keep)}件")
    include_unchanged = st.checkbox("価格が変わらない行もモールCSVに含める", value=False,
                                    key=f"{key_prefix}_inc_unchanged")
    confirm_and_download(df, key_prefix, tab_label, include_unchanged,
                         input_file=input_file, free_shipping=free_shipping)


# ══ タブ ════════════════════════════════════════════════════
# ※梱包サイズ変更タブは「📥 入荷登録」ページへ移設（判定ロジックはlib/pricingを共用）
tab1, tab2 = st.tabs(["📦 納品価格変更", "🚛 直送価格＆送料変更"])

# ── タブ1: 納品価格変更（本流） ─────────────────────────────
with tab1:
    st.markdown("##### 入力CSV: **JAN（またはNE商品コード）・新下代** ＋ 任意列（指定価格／値上げ率／項目1）")
    st.caption("アップした商品はすべて「目標利益率価格」と「値上げ率価格（現販売価格×新下代÷旧下代・送料は含めない）」の"
               "**高い方**に設定します（下代が値下げなら価格も下がり得ます。目標利益率は必ず確保）。"
               "目標利益率価格の計算では、3980円未満はお客様負担の送料（宅配880円/メール350円）を考慮します。"
               "旧下代はNE商品マスタの原価を使います。")

    up1 = st.file_uploader("価格変更の入力CSV", type=["csv"], key="t1_upload")
    in_df = None
    if up1 is not None:
        try:
            in_df = csv_import.read_csv_auto(up1.getvalue())
        except Exception as e:  # noqa: BLE001
            st.error(f"CSVの読込に失敗: {e}")

    if in_df is not None:
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
            jan_map, code_info = get_master_lookup()
            matched = []
            if jan_map is not None:
                matched, unmatched = pipeline.match_input(in_df, c_code, c_jan, jan_map, code_info)
                show_unmatched(unmatched)
            if matched:
                cur_prices = rakuten_price_controls(matched, "t1")
                matched = exclude_not_on_rakuten(matched, cur_prices)
            if matched:
                rows = pipeline.build_price_rows(
                    matched, c_cost, cost_table, params, mode="normal",
                    c_fixed=c_fixed, c_pct=c_pct, c_size=c_size,
                    overrides=st.session_state.get("t1_result_overrides"),
                    cur_prices=cur_prices)
                result_section(rows, "t1_result", "納品価格変更",
                               input_file=input_file_of(up1))

# ── タブ2: 直送価格＆送料変更 ───────────────────────────────
with tab2:
    st.markdown("##### 入力CSV: **JAN（またはNE商品コード）・新下代・送料**")
    st.caption("直送品は**送料込みの販売価格**を設定します（3980円未満でも送料込み）。"
               "送料は入力CSVの値を使い、資材は不要（送料・資材マスタは参照しません）。"
               "楽天・YahooのCSVには**送料無料フラグ**が付きます。価格ルールは納品価格変更と同じです。")
    up2 = st.file_uploader("直送価格変更の入力CSV", type=["csv"], key="t2_upload")
    if up2 is not None:
        try:
            in_df2 = csv_import.read_csv_auto(up2.getvalue())
        except Exception as e:  # noqa: BLE001
            st.error(f"CSVの読込に失敗: {e}")
            in_df2 = None
        if in_df2 is not None:
            c_jan = pipeline.pick_col(in_df2, "JANコード", "JAN", "jan")
            c_code = pipeline.pick_col(in_df2, "商品コード")
            c_cost = pipeline.pick_col(in_df2, "新下代", "下代", "仕入価格")
            c_ship = pipeline.pick_col(in_df2, "送料", "新送料")
            c_fixed = pipeline.pick_col(in_df2, "指定価格")
            c_pct = pipeline.pick_col(in_df2, "値上げ率")
            if not c_jan and not c_code:
                st.error(f"JAN列（または商品コード列）が見つかりません。実際の列: {list(in_df2.columns)}")
            elif not c_cost or not c_ship:
                st.error(f"新下代・送料の列が必要です。実際の列: {list(in_df2.columns)}")
            else:
                jan_map, code_info = get_master_lookup()
                matched = []
                if jan_map is not None:
                    matched, unmatched = pipeline.match_input(in_df2, c_code, c_jan, jan_map, code_info)
                    show_unmatched(unmatched)
                if matched:
                    cur_prices = rakuten_price_controls(matched, "t2")
                    matched = exclude_not_on_rakuten(matched, cur_prices)
                if matched:
                    rows = pipeline.build_price_rows(
                        matched, c_cost, {}, params, mode="direct",  # 送料・資材マスタは参照しない
                        c_fixed=c_fixed, c_pct=c_pct, c_ship=c_ship,
                        overrides=st.session_state.get("t2_result_overrides"),
                        cur_prices=cur_prices)
                    result_section(rows, "t2_result", "直送価格送料変更",
                                   input_file=input_file_of(up2), free_shipping=True)

