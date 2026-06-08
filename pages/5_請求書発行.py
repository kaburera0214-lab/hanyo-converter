# -*- coding: utf-8 -*-
"""
請求書発行ページ（Phase1）

このページは完全独立。既存ページ・共通ファイルには一切依存しない。
- session_state のキーはすべて "invoice_" 接頭辞で名前空間を分離
- import は遅延（関数内）にして、万一の不具合でもアプリ全体を巻き込まない

Phase1スコープ:
  1. クライアント選択（請求先ヘッダ情報を初期表示）
  2. 保管料入力（2期制：15日・末日の数量を入れて平均を自動計算）
  3. イレギュラー手入力（送料・作業料・値引き等を自由に追加）
  4. 請求書番号・各日付の自動生成（上書き可）
  5. MFクラウド取込用CSVのダウンロード（＋任意でDriveバックアップ）
"""
import datetime
import streamlit as st
import pandas as pd

st.set_page_config(page_title="請求書発行", layout="wide")
st.title("請求書発行")
st.caption("倉庫業務クライアント向けの請求書を作成し、MFクラウド取込用CSVを出力します。（Phase1）")

# --- 専用モジュール（遅延import） ---
from lib.invoice import (mf_export, invoice_number, store, notion_store,
                         csv_import, ne_calc, yamato_calc, drive_master)


# ============================================================
# 0. Notion初期化（マスタ・履歴の永続化）
#    エラー時はローカル既定値にフォールバックし、ページは必ず動作させる。
# ============================================================
def init_notion():
    """DBを冪等生成し db_ids を返す。session_stateにキャッシュ。
    キャッシュに不足DBがある場合（スキーマ追加後など）は作り直して自動修復する。"""
    cached = st.session_state.get("invoice_db_ids")
    if cached and all(k in cached for k in notion_store.DB_SCHEMAS):
        return cached
    with st.spinner("Notionデータベースを準備中…（初回・スキーマ更新時は数十秒かかります）"):
        db_ids = notion_store.ensure_databases()
        notion_store.seed_clients_if_empty(db_ids, store.DEFAULT_CLIENTS)
        notion_store.seed_area_map_if_empty(db_ids, store.DEFAULT_AREA_MAP)
        # 送料表の初期値はTeam-ECに投入（他クライアントは空から編集）
        notion_store.seed_shipping_table_if_empty(
            db_ids, "Team-EC", store.DEFAULT_SHIPPING_TABLE, store.SHIPPING_AREAS)
    st.session_state["invoice_db_ids"] = db_ids
    return db_ids


notion_ready = False
db_ids = None
if not st.secrets.get("INVOICE_NOTION_PARENT_PAGE_ID", ""):
    st.warning("Notion未設定のため、ローカル既定値で動作中です（編集内容や履歴は保存されません）。"
               "Secrets に INVOICE_NOTION_PARENT_PAGE_ID を設定すると永続化されます。")
else:
    try:
        db_ids = init_notion()
        notion_ready = True
    except Exception as e:
        st.error(f"Notion初期化に失敗しました（ローカル既定値で続行）: {e}")

col_reload, _ = st.columns([1, 5])
with col_reload:
    if st.button("🔄 マスタ再読込", key="invoice_reload"):
        for k in ("invoice_db_ids", "invoice_clients_cache",
                  "invoice_prod_df", "invoice_prod_meta"):
            st.session_state.pop(k, None)
        st.rerun()


# ============================================================
# 1. クライアント選択
# ============================================================
def get_clients():
    """Notionからクライアントを読む（キャッシュ）。失敗時はローカル既定値。"""
    if notion_ready:
        if "invoice_clients_cache" not in st.session_state:
            try:
                loaded = notion_store.load_clients(db_ids)
                st.session_state["invoice_clients_cache"] = loaded or store.load_clients()
            except Exception as e:
                st.error(f"クライアント読込に失敗（ローカル既定値で続行）: {e}")
                st.session_state["invoice_clients_cache"] = store.load_clients()
        return st.session_state["invoice_clients_cache"]
    return store.load_clients()


clients = get_clients()
client_names = list(clients.keys())

st.header("① クライアント・対象月")
col1, col2, col3 = st.columns([2, 1, 1])
with col1:
    client_name = st.selectbox("クライアント", client_names, key="invoice_client")
with col2:
    today = datetime.date.today()
    # 既定は先月（締めて請求するため）
    default_year = today.year if today.month > 1 else today.year - 1
    year = st.number_input("対象年", min_value=2020, max_value=2100,
                           value=today.year, step=1, key="invoice_year")
with col3:
    default_month = today.month - 1 if today.month > 1 else 12
    month = st.selectbox("対象月", list(range(1, 13)),
                         index=default_month - 1, key="invoice_month")

client = clients[client_name]
client_code = client.get("略号", "XX")

# --- 採番・日付の初期値 ---
auto_no = invoice_number.generate_invoice_number(int(year), int(month), client_code)
auto_dates = invoice_number.default_dates(int(year), int(month))

st.header("② 請求書ヘッダ情報")
hcol1, hcol2, hcol3 = st.columns(3)
with hcol1:
    inv_no = st.text_input("請求書番号", value=auto_no, key="invoice_no")
    issue_date = st.text_input("請求日", value=auto_dates["請求日"], key="invoice_issue")
with hcol2:
    due_date = st.text_input("お支払期限", value=auto_dates["お支払期限"], key="invoice_due")
    sales_date = st.text_input("売上計上日", value=auto_dates["売上計上日"], key="invoice_sales")
with hcol3:
    subject = st.text_input("件名", value=client["header"].get("件名", ""), key="invoice_subject")
    staff = st.text_input("自社担当者氏名", value=client["header"].get("自社担当者氏名", ""),
                          key="invoice_staff")

with st.expander("取引先の詳細情報（住所・備考・振込先）", expanded=False):
    h = client["header"]
    ec1, ec2 = st.columns(2)
    with ec1:
        corp_name = st.text_input("取引先名称", value=h.get("取引先名称", ""), key="invoice_corp")
        zip_code = st.text_input("取引先郵便番号", value=h.get("取引先郵便番号", ""), key="invoice_zip")
        pref = st.text_input("取引先都道府県", value=h.get("取引先都道府県", ""), key="invoice_pref")
        addr1 = st.text_input("取引先住所1", value=h.get("取引先住所1", ""), key="invoice_addr1")
        addr2 = st.text_input("取引先住所2", value=h.get("取引先住所2", ""), key="invoice_addr2")
    with ec2:
        keisho = st.text_input("取引先敬称", value=h.get("取引先敬称", ""), key="invoice_keisho")
        biko = st.text_area("備考", value=h.get("備考", ""), key="invoice_biko", height=80)
        furikomi = st.text_area("振込先", value=h.get("振込先", ""), key="invoice_furikomi", height=80)


# ============================================================
# 3. 保管料（2期制）
# ============================================================
with st.expander("🛠 単価マスタ管理（クライアント別：送料・出荷作業・資材・受注作業・保管）", expanded=False):
    if not notion_ready:
        st.info("Notion未設定のため編集できません。Secretsに INVOICE_NOTION_PARENT_PAGE_ID を設定してください。")
    else:
        st.caption("費目ごとの単価を編集できます。出荷作業・資材は配送種別(nekop/60/80/100/120/140/160)ごと、"
                   "送料はマージン率(%)・加算額で設定します。保存するとNotionの単価マスタを更新します。")
        try:
            pm_rows = notion_store.load_price_master(db_ids, client_name)
        except Exception as e:
            pm_rows = []
            st.error(f"単価マスタ読込に失敗: {e}")
        pm_df = pd.DataFrame(pm_rows, columns=[
            "費目", "種別", "単価", "出力品名", "マージン率", "加算額", "備考"])
        pm_edited = st.data_editor(
            pm_df,
            num_rows="dynamic",
            use_container_width=True,
            key="invoice_pm_editor",
            column_config={
                "費目": st.column_config.SelectboxColumn(
                    "費目", options=["保管", "送料", "出荷作業", "資材", "受注作業", "その他"]),
                "種別": st.column_config.TextColumn("種別（配送種別など）"),
                "単価": st.column_config.NumberColumn("単価", step=0.01, format="%.2f"),
                "出力品名": st.column_config.TextColumn("出力品名（MF品目名）"),
                "マージン率": st.column_config.NumberColumn("マージン率(%)", step=1),
                "加算額": st.column_config.NumberColumn("加算額", step=1),
                "備考": st.column_config.TextColumn("備考"),
            },
        )
        if st.button("💾 単価マスタを保存", key="invoice_pm_save", type="primary"):
            try:
                n = notion_store.replace_price_master(
                    db_ids, client_name, pm_edited.to_dict("records"))
                st.session_state.pop("invoice_clients_cache", None)
                st.success(f"単価マスタを保存しました（{n}件）。")
            except Exception as e:
                st.error(f"保存に失敗しました: {e}")

        # --- 配送種別単価（出荷作業料・資材費）を見やすいグリッドで編集 ---
        st.markdown("---")
        st.markdown("##### 配送種別ごとの単価（出荷作業料・資材費）")
        st.caption("出荷作業料・資材費は配送種別(nekop/60/80/100/120/140/160)ごとの単価です。"
                   "ここで編集すると内部的に単価マスタへ保存されます。")
        # 既存の単価マスタ(費目=出荷作業/資材)を配送種別でピボット
        ship_map = {r["種別"]: r["単価"] for r in pm_rows if r["費目"] == "出荷作業"}
        mat_map = {r["種別"]: r["単価"] for r in pm_rows if r["費目"] == "資材"}
        # 未登録なら既定値（store）でプリフィル
        if not ship_map and not mat_map:
            for m in store.DEFAULT_CLIENTS.get(client_name, {}).get("単価マスタ", []):
                if m["費目"] == "出荷作業":
                    ship_map[m["種別"]] = m["単価"]
                elif m["費目"] == "資材":
                    mat_map[m["種別"]] = m["単価"]
        types = list(dict.fromkeys(
            store.DELIVERY_TYPES + list(ship_map.keys()) + list(mat_map.keys())))
        size_df = pd.DataFrame([
            {"配送種別": t, "出荷作業料": ship_map.get(t, 0), "資材費": mat_map.get(t, 0)}
            for t in types if t
        ])
        size_edited = st.data_editor(
            size_df, num_rows="dynamic", use_container_width=True,
            key="invoice_sizerate_editor",
            column_config={
                "配送種別": st.column_config.TextColumn("配送種別"),
                "出荷作業料": st.column_config.NumberColumn("出荷作業料(単価)", step=1),
                "資材費": st.column_config.NumberColumn("資材費(単価)", step=0.01),
            })

        def _save_size_rates(records):
            rows = []
            for r in records:
                t = str(r.get("配送種別", "")).strip()
                if not t:
                    continue
                rows.append({"費目": "出荷作業", "種別": t,
                             "単価": r.get("出荷作業料") or 0, "出力品名": "出荷作業料"})
                rows.append({"費目": "資材", "種別": t,
                             "単価": r.get("資材費") or 0, "出力品名": "資材費"})
            n = notion_store.replace_price_rows(
                db_ids, client_name, {"出荷作業", "資材"}, rows)
            st.session_state.pop("invoice_clients_cache", None)
            return n

        if st.button("💾 配送種別単価を保存", key="invoice_sizerate_save", type="primary"):
            try:
                n = _save_size_rates(size_edited.to_dict("records"))
                st.success(f"配送種別単価を保存しました（{n}件）。")
            except Exception as e:
                st.error(f"保存に失敗しました: {e}")

        # 配送種別単価のCSV取込（列: 配送種別,出荷作業料,資材費）
        sr_csv = st.file_uploader(
            "配送種別単価CSVを選択（列: 配送種別,出荷作業料,資材費）",
            type=["csv"], key="invoice_sr_csv")
        if sr_csv is not None:
            try:
                sr_imported = csv_import.parse_size_rate_csv(sr_csv.getvalue())
                st.caption(f"取込プレビュー（{len(sr_imported)}行）")
                st.dataframe(pd.DataFrame(sr_imported), use_container_width=True,
                             hide_index=True)
                if st.button("💾 このCSV内容で配送種別単価を上書き保存",
                             key="invoice_sr_csv_save"):
                    n = _save_size_rates(sr_imported)
                    st.success(f"CSVから配送種別単価を保存しました（{n}件）。再読込で反映されます。")
            except Exception as e:
                st.error(f"CSV取込に失敗しました: {e}")

        # --- 送料方式（クライアント別） ---
        st.markdown("---")
        st.markdown("##### 送料の請求方式")
        try:
            sm = notion_store.load_client_shipping_method(db_ids, client_name)
        except Exception as e:
            sm = {"送料方式": "実費マージン", "送料マージン率": 0, "送料加算額": 0}
            st.error(f"送料方式の読込に失敗: {e}")
        smcol1, smcol2, smcol3 = st.columns(3)
        method = smcol1.selectbox(
            "送料方式", ["送料表", "実費マージン"],
            index=0 if sm["送料方式"] == "送料表" else 1, key="invoice_ship_method")
        margin = smcol2.number_input(
            "マージン率(%)（実費方式時）", value=float(sm["送料マージン率"] or 0),
            step=1.0, key="invoice_ship_margin")
        addon = smcol3.number_input(
            "加算額（実費方式時・件あたり）", value=float(sm["送料加算額"] or 0),
            step=1.0, key="invoice_ship_addon")
        if st.button("💾 送料方式を保存", key="invoice_sm_save"):
            try:
                notion_store.save_client_shipping_method(
                    db_ids, client_name, method, margin, addon)
                st.success("送料方式を保存しました。")
            except Exception as e:
                st.error(f"保存に失敗しました: {e}")

        # --- 送料表（サイズ×地域マトリクス） ---
        st.markdown("---")
        st.markdown("##### 送料表（サイズ × 地域）")
        st.caption("「送料表」方式のときに使用します。行＝サイズ、列＝地域。"
                   "都道府県→地域の対応は下の地域マスタで管理します。")
        try:
            st_rows = notion_store.load_shipping_table(
                db_ids, client_name, store.SHIPPING_AREAS)
        except Exception as e:
            st_rows = []
            st.error(f"送料表の読込に失敗: {e}")
        st_cols = ["配送業者", "配送区分", "サイズ"] + store.SHIPPING_AREAS
        st_df = pd.DataFrame(st_rows, columns=st_cols)
        st_edited = st.data_editor(
            st_df, num_rows="dynamic", use_container_width=True,
            key="invoice_shiptable_editor")
        if st.button("💾 送料表を保存（表の内容）", key="invoice_st_save"):
            try:
                n = notion_store.replace_shipping_table(
                    db_ids, client_name, st_edited.to_dict("records"),
                    store.SHIPPING_AREAS)
                st.success(f"送料表を保存しました（{n}行）。")
            except Exception as e:
                st.error(f"保存に失敗しました: {e}")

        # 送料表のCSV一括取込
        st.markdown("**CSVで一括取込**")
        st.caption("形式：先頭列が 配送業者・配送区分・サイズ、以降に地域列（北海道〜沖縄）。"
                   "金額のカンマ区切り（1,460）や「60サイズ」表記も自動処理します。")
        st_csv = st.file_uploader("送料表CSVを選択", type=["csv"], key="invoice_st_csv")
        if st_csv is not None:
            try:
                imported = csv_import.parse_shipping_table_csv(
                    st_csv.getvalue(), store.SHIPPING_AREAS)
                imp_df = pd.DataFrame(imported, columns=st_cols)
                st.caption(f"取込プレビュー（{len(imp_df)}行）")
                st.dataframe(imp_df, use_container_width=True, hide_index=True)
                if st.button("💾 このCSV内容で送料表を上書き保存",
                             key="invoice_st_csv_save", type="primary"):
                    n = notion_store.replace_shipping_table(
                        db_ids, client_name, imported, store.SHIPPING_AREAS)
                    st.success(f"CSVから送料表を保存しました（{n}行）。再読込で反映されます。")
            except Exception as e:
                st.error(f"CSV取込に失敗しました: {e}")

        # --- 地域マスタ（都道府県→エリア） ---
        st.markdown("---")
        st.markdown("##### 地域マスタ（都道府県 → エリア）")
        st.caption("ヤマト運賃CSVの「扱店都道府県」を送料表の地域に変換するための対応表です（全クライアント共通）。")
        with st.expander("地域マスタを表示・編集", expanded=False):
            try:
                amap = notion_store.load_area_map(db_ids)
            except Exception as e:
                amap = {}
                st.error(f"地域マスタの読込に失敗: {e}")
            am_df = pd.DataFrame(
                [{"都道府県": k, "エリア": v} for k, v in amap.items()],
                columns=["都道府県", "エリア"])
            st.dataframe(am_df, use_container_width=True, hide_index=True)
            st.caption("※ 編集機能はPhase3で追加予定。現状は初期値（47都道府県→ヤマト地域）を使用します。")

st.header("③ 保管料（2期制：15日・末日）")
st.caption("各種別について15日時点と末日時点の数量を入力すると、平均×単価で自動計算します。")

target_ym = f"{int(year)}-{int(month):02d}"

# 当月の保管内訳履歴があれば数量をプリフィルする
history_rows = []
if notion_ready:
    try:
        history_rows = notion_store.load_storage_history(db_ids, client_name, target_ym)
    except Exception as e:
        st.caption(f"（履歴の読込はスキップしました: {e}）")

if history_rows:
    st.caption(f"📌 {target_ym} の保管内訳履歴を読み込みました（編集して再保存できます）。")
    storage_default = pd.DataFrame([
        {"種別名": r["種別名"], "15日数量": r["15日数量"], "末日数量": r["末日数量"],
         "単価": r["単価"], "出力品名": r["出力品名"]}
        for r in history_rows
    ])
else:
    master = client.get("保管料マスタ", [])
    storage_default = pd.DataFrame([
        {"種別名": m["種別名"], "15日数量": 0, "末日数量": 0,
         "単価": m["単価"], "出力品名": m["出力品名"]}
        for m in master
    ])
if storage_default.empty:
    storage_default = pd.DataFrame(
        columns=["種別名", "15日数量", "末日数量", "単価", "出力品名"])

storage_edited = st.data_editor(
    storage_default,
    num_rows="dynamic",
    use_container_width=True,
    key="invoice_storage_editor",
    column_config={
        "種別名": st.column_config.TextColumn("種別名", width="medium"),
        "15日数量": st.column_config.NumberColumn("15日数量", min_value=0, step=1),
        "末日数量": st.column_config.NumberColumn("末日数量", min_value=0, step=1),
        "単価": st.column_config.NumberColumn("単価", min_value=0, step=10),
        "出力品名": st.column_config.TextColumn("出力品名（MF品目名）", width="medium"),
    },
)

# 平均・金額を計算し、出力品名ごとに集計
storage_lines = {}   # 出力品名 -> 金額合計
storage_preview = []
for _, row in storage_edited.iterrows():
    name = str(row.get("種別名", "")).strip()
    if not name:
        continue
    q15 = float(row.get("15日数量") or 0)
    qend = float(row.get("末日数量") or 0)
    price = float(row.get("単価") or 0)
    out_name = str(row.get("出力品名", "")).strip() or "保管料"
    avg = (q15 + qend) / 2
    amount = round(avg * price)
    storage_preview.append({
        "種別名": name, "15日数量": q15, "末日数量": qend, "平均数量": avg,
        "単価": price, "金額": amount, "出力品名": out_name})
    storage_lines[out_name] = storage_lines.get(out_name, 0) + amount

if storage_preview:
    st.dataframe(pd.DataFrame(storage_preview), use_container_width=True, hide_index=True)


# ============================================================
# 4. データ取込・自動算出（NE）
# ============================================================
st.header("④ データ取込・自動算出（NE）")
st.caption("①NE出荷確定で受注作業料を、⑤ヤマト運賃で送料・出荷作業料・資材費を自動算出し、下の⑤費目に反映します。")

# 受注作業の単価をマスタから取得
juchu_unit = 0.0
if notion_ready:
    try:
        for r in notion_store.load_price_master(db_ids, client_name):
            if r["費目"] == "受注作業":
                juchu_unit = float(r["単価"] or 0)
                break
    except Exception as e:
        st.caption(f"（受注作業単価の取得をスキップ: {e}）")

auto_ship_count = 0          # 出荷件数
auto_juchu_amount = None     # 受注作業料の自動算出額
auto_souryo = None           # 送料
auto_shukka = None           # 出荷作業料
auto_shizai = None           # 資材費
soufuda_set = set()          # ①の発送伝票番号（送り状）集合
ne_denpyo_set = set()        # ①のNE伝票番号集合（④橋渡し用）
issued_df = None             # ④発行済データ

ne_ship_files = st.file_uploader(
    "①NE出荷確定CSVを選択（1000件単位で分割されている場合は複数選択OK）",
    type=["csv"], key="invoice_ne_ship", accept_multiple_files=True)
if ne_ship_files:
    try:
        ne_df, _errs = csv_import.load_concat(ne_ship_files, ne_calc.load_shipment)
        for _nm, _er in _errs:
            st.error(f"『{_nm}』の読込に失敗: {_er}")
        if ne_df is None:
            raise ValueError("有効な①ファイルがありません。")
        st.caption(f"①を {len(ne_ship_files) - len(_errs)} ファイル結合（計 {len(ne_df):,} 行）")
        summary = ne_calc.summarize_shipment(ne_df)
        auto_ship_count = summary["出荷件数"]
        soufuda_set = ne_calc.get_soufuda_set(ne_df)
        ne_denpyo_set = ne_calc.get_ne_denpyo_set(ne_df)
        c1, c2 = st.columns(2)
        c1.metric("出荷件数（ユニーク伝票番号）", f"{auto_ship_count:,} 件")
        c2.metric("受注作業 単価", f"{juchu_unit:,.0f} 円")
        auto_juchu_amount = round(auto_ship_count * juchu_unit)
        st.success(f"受注作業料 ＝ {auto_ship_count:,}件 × {juchu_unit:,.0f}円 "
                   f"＝ {auto_juchu_amount:,}円（⑤の受注作業料に反映）")
        with st.expander("発送方法別の内訳（参考）", expanded=False):
            md = summary["発送方法別"]
            st.dataframe(
                pd.DataFrame([{"発送方法": k, "件数": v} for k, v in md.items()]),
                use_container_width=True, hide_index=True)
    except Exception as e:
        st.error(f"NE出荷確定CSVの取込に失敗しました: {e}")

# --- ②受注明細＋③商品マスタ → 出荷作業料（PCS×サイズ別単価） ---
st.markdown("##### ②受注明細一覧 ＋ ③商品マスタ → 出荷作業料")
st.caption("出荷作業料はPCS（商品点数）×サイズ別単価で算出します。"
           "②はクライアント別。③はNE共通の商品マスタで、Driveに保存して毎回のアップは不要です。")

drive_folder = st.secrets.get("INVOICE_GDRIVE_FOLDER_ID", "")

# ③商品マスタ：session_state → 共有master.csv → Drive の順で取得
prod_df = st.session_state.get("invoice_prod_df")
prod_meta = st.session_state.get("invoice_prod_meta", "")

# 1) 汎用マスタ変換と共有の master.csv（項目1列があれば優先利用）
if prod_df is None:
    try:
        import os
        mpath = os.path.join(os.path.dirname(__file__), "..", "master.csv")
        if os.path.exists(mpath):
            mdf = ne_calc._norm_columns(
                csv_import.read_csv_auto(open(mpath, "rb").read()))
            if "商品コード" in mdf.columns and "項目1" in mdf.columns:
                prod_df = mdf
                prod_meta = f"共有master.csv（汎用と共通・{len(mdf):,}件）"
                st.session_state["invoice_prod_df"] = prod_df
                st.session_state["invoice_prod_meta"] = prod_meta
    except Exception as e:
        st.caption(f"（共有master.csvの読込をスキップ: {e}）")

# 2) Drive保存版（master.csvに項目1が無い場合のフォールバック）
if prod_df is None and drive_folder:
    try:
        f = drive_master.find_file(drive_master.PRODUCT_MASTER_NAME, drive_folder)
        if f:
            prod_df = ne_calc.load_product_master(drive_master.download_bytes(f["id"]))
            prod_meta = f"Drive保存版（更新 {str(f.get('modifiedTime',''))[:10]}・{len(prod_df):,}件）"
            st.session_state["invoice_prod_df"] = prod_df
            st.session_state["invoice_prod_meta"] = prod_meta
    except Exception as e:
        st.caption(f"（Driveの③読込をスキップ: {e}）")

with st.expander("③商品マスタの管理（毎回アップ不要・Drive保存）",
                 expanded=(prod_df is None)):
    st.caption("商品マスタは汎用マスタ変換の master.csv と共有できます。"
               "master.csv に「項目1（サイズ）」列を含めて汎用側で更新すれば、"
               "請求書側も自動で最新になります（更新点が1つに）。"
               "項目1列が無い間は、下のDriveアップロード版を使います。")
    if prod_df is not None:
        st.success(f"③商品マスタ利用中: {prod_meta}")
    else:
        st.info("③商品マスタが未保存です。初回のみ最新の③をアップロードしてください。")
    if st.button("🔄 商品マスタを再取得（最新を読み直す）", key="invoice_prod_reload"):
        for k in ("invoice_prod_df", "invoice_prod_meta"):
            st.session_state.pop(k, None)
        st.rerun()
    # 特定商品の項目1が今のマスタに反映されているか確認
    if prod_df is not None:
        chk = st.text_input("商品コードで項目1を確認", key="invoice_prod_check",
                            placeholder="例: TE4580060597956")
        if chk.strip():
            import unicodedata as _u
            key = _u.normalize("NFKC", chk).strip()
            hit = prod_df[prod_df["商品コード"].map(ne_calc._norm) == key]
            if hit.empty:
                st.error(f"この商品コードは現在の商品マスタに存在しません（③が最新でない可能性）。")
            else:
                val = ne_calc._norm(hit.iloc[0]["項目1"])
                if val in ("", "nan"):
                    st.warning("この商品の項目1（サイズ）は**空**です。③で項目1を設定して更新してください。")
                else:
                    st.success(f"この商品の項目1（サイズ）= 「{val}」（マスタに反映済み）")
    if not drive_folder:
        st.warning("Secretsに INVOICE_GDRIVE_FOLDER_ID が未設定のため、③をDrive保存できません"
                   "（今回のセッションのみ利用）。")
    new_prod = st.file_uploader("③NEカスタム(商品マスタ)をアップロード／更新",
                                type=["csv"], key="invoice_prod_upload")
    if new_prod is not None:
        try:
            data = new_prod.getvalue()
            pdf = ne_calc.load_product_master(data)  # 検証
            if drive_folder:
                drive_master.upload_or_replace(
                    data, drive_master.PRODUCT_MASTER_NAME, drive_folder)
                prod_meta = f"アップロード版（{len(pdf):,}件・Drive保存済）"
                st.success(f"③をDriveに保存しました（{len(pdf):,}件）。次回からアップ不要です。")
            else:
                prod_meta = f"アップロード版（{len(pdf):,}件・未保存）"
            prod_df = pdf
            st.session_state["invoice_prod_df"] = pdf
            st.session_state["invoice_prod_meta"] = prod_meta
        except Exception as e:
            st.error(f"③の取込に失敗: {e}")

order_files = st.file_uploader(
    "②受注明細一覧CSV（複数選択OK）",
    type=["csv"], key="invoice_ne_order", accept_multiple_files=True)
if order_files:
    if prod_df is None:
        st.error("③商品マスタがありません。上の『③商品マスタの管理』から最新の③をアップロードしてください。")
    elif not notion_ready:
        st.warning("Notion未設定のため出荷作業単価を参照できません。")
    else:
        try:
            order_df, _oerrs = csv_import.load_concat(order_files, ne_calc.load_order_detail)
            for _nm, _er in _oerrs:
                st.error(f"『{_nm}』の読込に失敗: {_er}")
            if order_df is None:
                raise ValueError("有効な②ファイルがありません。")
            ship_rates = {}
            for r in notion_store.load_price_master(db_ids, client_name):
                if r["費目"] == "出荷作業":
                    ship_rates[r["種別"]] = r["単価"]
            pres = ne_calc.compute_picking_charge(order_df, prod_df, ship_rates)
            unmatched = pres["未マッチ商品数"]
            if unmatched:
                st.error(
                    f"⚠️ ③商品マスタに無い商品が {unmatched} 件あります。"
                    "③が最新でない可能性が高いです。"
                    "『③商品マスタの管理』から**最新の③をアップロード**してください"
                    "（このままでは出荷作業料が過少になります）。")
                with st.expander("紐づかなかった商品コード（先頭20件）", expanded=True):
                    bad = order_df.loc[
                        ~order_df["商品コード"].map(ne_calc._norm).isin(
                            prod_df["商品コード"].map(ne_calc._norm)),
                        "商品コード"].astype(str).head(20).tolist()
                    st.write(bad)
            auto_shukka = pres["出荷作業料"]
            st.success(f"出荷作業料 ＝ {auto_shukka:,}円（PCS合計 "
                       f"{sum(pres['サイズ別PCS'].values()):,}）（⑤に反映）")
            with st.expander("サイズ別PCS（参考）", expanded=False):
                st.dataframe(
                    pd.DataFrame([{"サイズ": k, "PCS": v}
                                  for k, v in pres["サイズ別PCS"].items()]),
                    use_container_width=True, hide_index=True)
            if pres["未登録明細"]:
                st.warning(
                    f"出荷作業料を計上できなかった商品が {len(pres['未登録明細'])} 件あります"
                    f"（サイズ: {pres['単価未登録サイズ']}）。"
                    "下記の商品について、③のサイズ設定または単価マスタを確認してください。")
                st.dataframe(
                    pd.DataFrame(pres["未登録明細"]),
                    use_container_width=True, hide_index=True)
        except Exception as e:
            st.error(f"出荷作業料の算出に失敗しました: {e}")

# --- ④発行済データ（NE伝票番号⇔送り状の橋渡し。ネコポス精度に必須） ---
st.markdown("##### ④ヤマト発行済データCSV（推奨：ネコポスの紐付け精度向上）")
st.caption("ネコポスはNEの発送番号と実際の送り状がズレるため、④で正しく橋渡しします。"
           "未アップロードでも動きますが、ネコポスの件数が過少になることがあります。")
issued_files = st.file_uploader(
    "④ヤマト発行済データCSVを選択（複数選択OK）",
    type=["csv"], key="invoice_ya_issued", accept_multiple_files=True)
if issued_files:
    try:
        issued_df, _ierrs = csv_import.load_concat(issued_files, yamato_calc.load_issued)
        for _nm, _er in _ierrs:
            st.error(f"『{_nm}』の読込に失敗: {_er}")
        if issued_df is not None:
            st.caption(f"④発行済データ {len(issued_df):,}件を読み込みました。")
    except Exception as e:
        st.error(f"④発行済データの取込に失敗しました: {e}")

# --- ⑤ヤマト運賃（全件）→ 送料・出荷作業料・資材費 ---
st.markdown("##### ⑤ヤマト運賃CSV（全クライアント混在のままでOK）")
st.caption("①（＋④）でこのクライアント分だけ絞り込み、サイズ別に出荷作業料・資材費・送料を算出します。"
           "先に①NE出荷確定を取り込んでください。")
ya_files = st.file_uploader(
    "⑤ヤマト運賃情報参照CSVを選択（複数選択OK）",
    type=["csv"], key="invoice_ya_freight", accept_multiple_files=True)
if ya_files:
    if not soufuda_set:
        st.warning("先に①NE出荷確定CSVを取り込んでください（送り状番号で絞り込みます）。")
    elif not notion_ready:
        st.warning("Notion未設定のため単価マスタを参照できません。")
    else:
        try:
            fr_df, _ferrs = csv_import.load_concat(ya_files, yamato_calc.load_freight)
            for _nm, _er in _ferrs:
                st.error(f"『{_nm}』の読込に失敗: {_er}")
            if fr_df is None:
                raise ValueError("有効な⑤ファイルがありません。")
            client_keys = yamato_calc.build_client_soufuda(
                soufuda_set, ne_denpyo_set, issued_df)
            matched, n_match, n_all = yamato_calc.filter_by_soufuda(fr_df, client_keys)
            bridge_note = "（①＋④で紐付け）" if issued_df is not None else "（①のみ。④未取込）"
            st.info(f"ヤマト運賃 全{n_all:,}件中、このクライアント分 {n_match:,}件を抽出 {bridge_note}。")
            if issued_df is None:
                st.warning("④発行済データが未取込のため、ネコポスの件数が過少になる可能性があります。")
            # マスタ取得
            pm = notion_store.load_price_master(db_ids, client_name)
            ship_rates = {r["種別"]: r["単価"] for r in pm if r["費目"] == "出荷作業"}
            mat_rates = {r["種別"]: r["単価"] for r in pm if r["費目"] == "資材"}
            sm2 = notion_store.load_client_shipping_method(db_ids, client_name)
            ship_table = notion_store.load_shipping_table(
                db_ids, client_name, store.SHIPPING_AREAS)
            amap2 = notion_store.load_area_map(db_ids)
            res = yamato_calc.compute_charges(
                matched, ship_rates=ship_rates, material_rates=mat_rates,
                shipping_method=sm2["送料方式"], shipping_table=ship_table,
                area_map=amap2, margin_rate=sm2["送料マージン率"],
                addon=sm2["送料加算額"])
            auto_souryo = res["送料"]
            auto_shizai = res["資材費"]
            m1, m3 = st.columns(2)
            m1.metric("送料", f"{auto_souryo:,} 円")
            m3.metric("資材費", f"{auto_shizai:,} 円")
            st.caption(f"送料方式: {sm2['送料方式']}")
            with st.expander("配送種別別の件数（参考）", expanded=False):
                st.dataframe(
                    pd.DataFrame([{"配送種別": k, "件数": v}
                                  for k, v in res["種別別件数"].items()]),
                    use_container_width=True, hide_index=True)
            for w in res["警告"]:
                st.warning(w)
        except Exception as e:
            st.error(f"ヤマト運賃CSVの取込・算出に失敗しました: {e}")


# ============================================================
# 5. イレギュラー・その他費目（手入力＋自動算出の反映）
# ============================================================
st.header("⑤ その他費目（送料・作業料・値引き等）")
st.caption("自動算出された値は反映済みです。未対応の費目（送料・出荷作業・資材）は当面手入力してください。")

# 受注作業料は自動算出があれば 単価=受注作業単価・数量=出荷件数 をプリフィル
if auto_juchu_amount is not None and auto_ship_count > 0:
    _juchu_row = {"品名": "受注作業料", "単価": juchu_unit, "数量": auto_ship_count}
else:
    _juchu_row = {"品名": "受注作業料", "単価": 0, "数量": 1}

def _auto_row(name, amount):
    """自動算出額があれば 単価=金額・数量=1 でプリフィル、無ければ0。"""
    return {"品名": name, "単価": amount if amount is not None else 0, "数量": 1}

other_default = pd.DataFrame([
    _auto_row("送料", auto_souryo),
    _auto_row("出荷作業料", auto_shukka),
    _auto_row("資材費", auto_shizai),
    _juchu_row,
    {"品名": "[汎用]作業料", "単価": 0, "数量": 1},
    {"品名": "その他", "単価": 0, "数量": 1},
    {"品名": "値引き", "単価": 0, "数量": 1},
])
other_edited = st.data_editor(
    other_default,
    num_rows="dynamic",
    use_container_width=True,
    key="invoice_other_editor",
    column_config={
        "品名": st.column_config.TextColumn("品名", width="medium"),
        "単価": st.column_config.NumberColumn("単価（マイナス可）", step=10),
        "数量": st.column_config.NumberColumn("数量", step=0.25),
    },
)


# ============================================================
# 5. 品目を組み立ててプレビュー＆CSV出力
# ============================================================
st.header("⑥ 請求内容の確認とCSV出力")

items = []
# 保管料（出力品名ごとに1行、数量1・単価=合計金額）
for out_name, amount in storage_lines.items():
    if amount != 0:
        items.append({"品名": out_name, "単価": amount, "数量": 1, "金額": amount})
# その他費目
for _, row in other_edited.iterrows():
    name = str(row.get("品名", "")).strip()
    if not name:
        continue
    price = float(row.get("単価") or 0)
    qty = float(row.get("数量") or 0)
    amount = round(price * qty)
    if price == 0 and qty == 0:
        continue
    items.append({"品名": name, "単価": price, "数量": qty, "金額": amount})

if not items:
    st.info("品目がありません。保管料またはその他費目を入力してください。")
    st.stop()

subtotal, tax, total = mf_export.calc_totals(items)

# プレビュー（表示専用：HTML白背景で見やすく）
prev_df = pd.DataFrame([
    {"品名": it["品名"], "単価": f"{int(it['単価']):,}",
     "数量": it["数量"], "金額": f"{int(it['金額']):,}"}
    for it in items
])
st.dataframe(prev_df, use_container_width=True, hide_index=True)

mcol1, mcol2, mcol3 = st.columns(3)
mcol1.metric("小計", f"{subtotal:,} 円")
mcol2.metric("消費税(10%)", f"{tax:,} 円")
mcol3.metric("合計金額", f"{total:,} 円")

# CSV生成
header = {
    "取引先名称": st.session_state.get("invoice_corp", client["header"].get("取引先名称", "")),
    "件名": subject,
    "請求日": issue_date,
    "お支払期限": due_date,
    "請求書番号": inv_no,
    "売上計上日": sales_date,
    "取引先敬称": st.session_state.get("invoice_keisho", ""),
    "取引先郵便番号": st.session_state.get("invoice_zip", ""),
    "取引先都道府県": st.session_state.get("invoice_pref", ""),
    "取引先住所1": st.session_state.get("invoice_addr1", ""),
    "取引先住所2": st.session_state.get("invoice_addr2", ""),
    "自社担当者氏名": staff,
    "備考": st.session_state.get("invoice_biko", ""),
    "振込先": st.session_state.get("invoice_furikomi", ""),
}

st.subheader("CSVダウンロード")
enc_label = st.radio("文字コード", ["UTF-8(BOM付き)", "Shift-JIS(cp932)"],
                     horizontal=True, key="invoice_enc")
encoding = "cp932" if enc_label.startswith("Shift") else "utf-8-sig"
csv_bytes = mf_export.to_csv_bytes(header, items, encoding=encoding)
filename = f"MF請求書_{client_name}_{inv_no}.csv"

st.download_button(
    "MFクラウド取込用CSVをダウンロード",
    data=csv_bytes,
    file_name=filename,
    mime="text/csv",
    key="invoice_download",
)

# 任意：Driveバックアップ
with st.expander("Googleドライブにバックアップ保存（任意）", expanded=False):
    folder_id = st.text_input(
        "保存先フォルダID（請求書専用フォルダを指定）",
        value=st.secrets.get("INVOICE_GDRIVE_FOLDER_ID", ""),
        key="invoice_drive_folder",
        help="Secretsに INVOICE_GDRIVE_FOLDER_ID を設定すると自動入力されます。",
    )
    if st.button("Driveへバックアップ", key="invoice_drive_btn"):
        if not folder_id:
            st.error("保存先フォルダIDを入力してください。")
        else:
            try:
                fid = store.backup_to_drive(csv_bytes, filename, folder_id)
                st.success(f"Driveへ保存しました（ファイルID: {fid}）")
            except Exception as e:
                st.error(f"Drive保存に失敗しました: {e}")


# ============================================================
# 6. Notionへ履歴保存（請求書・見積のスナップショット＋保管内訳）
# ============================================================
st.header("⑦ 履歴保存（Notion）")
if not notion_ready:
    st.info("Notion未設定のため履歴保存は無効です。Secretsに INVOICE_NOTION_PARENT_PAGE_ID を設定すると有効になります。")
else:
    st.caption("発行時点の内容をそのまま記録します。後でマスタ単価を変えても、この履歴は当時の内容のまま残ります。")
    scol1, scol2 = st.columns(2)

    def _do_save(kind):
        try:
            notion_store.save_issue_history(
                db_ids, invoice_no=inv_no, client_name=client_name,
                target_ym=target_ym, kind=kind, issue_date=issue_date,
                due_date=due_date, subtotal=subtotal, tax=tax, total=total,
                items=items)
            if storage_preview:
                notion_store.save_storage_history(
                    db_ids, client_name=client_name, target_ym=target_ym,
                    storage_rows=storage_preview)
            st.success(f"{kind}として履歴に保存しました（{inv_no} / {target_ym}）。")
        except Exception as e:
            st.error(f"履歴保存に失敗しました: {e}")

    with scol1:
        if st.button("💾 請求書として履歴に保存", key="invoice_save_bill", type="primary"):
            _do_save("請求")
    with scol2:
        if st.button("📝 見積として履歴に保存", key="invoice_save_quote"):
            _do_save("見積")
