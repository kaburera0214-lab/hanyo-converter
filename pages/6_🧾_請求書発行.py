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

from lib.auth import require_role
require_role("invoice")  # 認証ゲート（AUTH_ENABLED=false なら素通り）
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

st.header("【1】クライアント・対象月")
col1, col2, col3 = st.columns([2, 1, 1])
with col1:
    _def_idx = client_names.index("Team-EC") if "Team-EC" in client_names else 0
    client_name = st.selectbox("クライアント", client_names, index=_def_idx,
                               key="invoice_client")
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

# --- 新規クライアント追加 ---
with st.expander("➕ 新規クライアントを追加", expanded=False):
    if not notion_ready:
        st.info("Notion未設定のため追加できません。")
    else:
        st.caption("基本情報を登録します。単価・送料方式・保管種別は登録後に"
                   "「🛠 単価マスタ管理」等で設定してください。")
        nc1, nc2 = st.columns(2)
        nc_name = nc1.text_input("クライアント名（必須・一覧に表示）", key="newc_name")
        nc_ryaku = nc2.text_input("略号（請求書番号に使用・例 TE）", key="newc_ryaku")
        nc_corp = st.text_input("取引先名称（正式社名）", key="newc_corp")
        nz1, nz2 = st.columns(2)
        nc_zip = nz1.text_input("郵便番号", key="newc_zip")
        nc_pref = nz2.text_input("都道府県", key="newc_pref")
        nc_ad1 = st.text_input("住所1", key="newc_ad1")
        nc_ad2 = st.text_input("住所2", key="newc_ad2")
        nc_subj = st.text_input("件名", value="物流業務委託費", key="newc_subj")
        nc_furi = st.text_area("振込先", key="newc_furi", height=70)
        nc_biko = st.text_input("備考", key="newc_biko")
        if st.button("➕ このクライアントを追加", key="newc_save", type="primary"):
            try:
                notion_store.create_client(
                    db_ids, nc_name, nc_ryaku.strip(), {
                        "取引先名称": nc_corp, "取引先郵便番号": nc_zip,
                        "取引先都道府県": nc_pref, "取引先住所1": nc_ad1,
                        "取引先住所2": nc_ad2, "件名": nc_subj,
                        "振込先": nc_furi, "備考": nc_biko})
                st.session_state.pop("invoice_clients_cache", None)
                st.success(f"クライアント『{nc_name}』を追加しました。"
                           "上のクライアント選択で切り替え、各マスタを設定してください。")
            except Exception as e:
                st.error(f"追加に失敗しました: {e}")

# --- クライアント設定チェック（新規立ち上げ時の抜け漏れ防止） ---
if notion_ready:
    _miss = []
    _h = client.get("header", {})
    if not _h.get("取引先名称"):
        _miss.append("取引先名称")
    if not _h.get("振込先"):
        _miss.append("振込先")
    if not client.get("略号"):
        _miss.append("略号")
    if not client.get("保管料マスタ"):
        _miss.append("保管料マスタ（種別）")
    try:
        _pm_chk = notion_store.load_price_master(db_ids, client_name)
        _himoku = {r["費目"] for r in _pm_chk}
        _out = {str(r.get("出力品名", "")) for r in _pm_chk}
        for _need, _label in [("受注作業", "受注作業の単価"), ("出荷作業", "出荷作業の単価"),
                              ("資材", "資材の単価")]:
            if _need not in _himoku:
                _miss.append(_label)
        # 送料は単価マスタではなく送料方式で設定する（送料の費目チェックは不要）
        if not any("[汎用]作業料" in o for o in _out):
            _miss.append("[汎用]作業料の時給単価")
        _sm = notion_store.load_client_shipping_method(db_ids, client_name)
        if not _sm.get("送料方式"):
            _miss.append("送料方式")
    except Exception:
        pass
    if _miss:
        with st.expander(f"⚠️ {client_name} は未設定の項目があります（{len(_miss)}件）", expanded=True):
            st.warning("以下が未設定です。請求金額が0や欠落になる恐れがあります。"
                       "「🛠 単価マスタ管理」等で設定してください。")
            st.write("・" + "\n・".join(_miss))

# --- 採番・日付の初期値 ---
auto_no = invoice_number.generate_invoice_number(int(year), int(month), client_code)
auto_dates = invoice_number.default_dates(int(year), int(month))

st.header("【2】請求書ヘッダ情報")
st.caption("取引先情報はクライアントマスタから読み込みます。ここで編集して「マスタに保存」すると次回以降も反映されます。")
h = client["header"]
# クライアント別キー（クライアント切替で最新のマスタ値を読み込む）
_k = client_name
hcol1, hcol2, hcol3 = st.columns(3)
with hcol1:
    inv_no = st.text_input("請求書番号", value=auto_no, key=f"invoice_no_{_k}")
    issue_date = st.text_input("請求日", value=auto_dates["請求日"], key=f"invoice_issue_{_k}")
with hcol2:
    due_date = st.text_input("お支払期限", value=auto_dates["お支払期限"], key=f"invoice_due_{_k}")
    sales_date = st.text_input("売上計上日", value=auto_dates["売上計上日"], key=f"invoice_sales_{_k}")
with hcol3:
    subject = st.text_input("件名", value=h.get("件名", ""), key=f"invoice_subject_{_k}")
    staff = st.text_input("自社担当者氏名", value=h.get("自社担当者氏名", ""),
                          key=f"invoice_staff_{_k}")

with st.expander("取引先の詳細情報（住所・備考・振込先）＋マスタ保存", expanded=False):
    ec1, ec2 = st.columns(2)
    with ec1:
        ryaku_in = st.text_input("略号（請求書番号に使用）", value=client.get("略号", ""),
                                 key=f"invoice_ryaku_{_k}")
        corp_name = st.text_input("取引先名称", value=h.get("取引先名称", ""), key=f"invoice_corp_{_k}")
        zip_code = st.text_input("取引先郵便番号", value=h.get("取引先郵便番号", ""), key=f"invoice_zip_{_k}")
        pref = st.text_input("取引先都道府県", value=h.get("取引先都道府県", ""), key=f"invoice_pref_{_k}")
        addr1 = st.text_input("取引先住所1", value=h.get("取引先住所1", ""), key=f"invoice_addr1_{_k}")
        addr2 = st.text_input("取引先住所2", value=h.get("取引先住所2", ""), key=f"invoice_addr2_{_k}")
    with ec2:
        keisho = st.text_input("取引先敬称", value=h.get("取引先敬称", ""), key=f"invoice_keisho_{_k}")
        biko = st.text_area("備考", value=h.get("備考", ""), key=f"invoice_biko_{_k}", height=80)
        furikomi = st.text_area("振込先", value=h.get("振込先", ""), key=f"invoice_furikomi_{_k}", height=80)
    if st.button("💾 取引先情報をマスタに保存", key=f"invoice_header_save_{_k}",
                 disabled=not notion_ready):
        try:
            notion_store.save_client_header(
                db_ids, client_name, ryaku_in.strip(), {
                    "取引先名称": corp_name, "取引先郵便番号": zip_code,
                    "取引先都道府県": pref, "取引先住所1": addr1, "取引先住所2": addr2,
                    "件名": subject, "自社担当者氏名": staff,
                    "振込先": furikomi, "備考": biko})
            st.session_state.pop("invoice_clients_cache", None)
            st.success(f"{client_name} の取引先情報をマスタに保存しました。"
                       "「🔄 マスタ再読込」で請求書番号の略号等も反映されます。")
        except Exception as e:
            st.error(f"保存に失敗しました: {e}")


# ============================================================
# 3. 保管料（2期制）
# ============================================================
with st.expander("🛠 単価マスタ管理（クライアント別：送料・出荷作業・資材・受注作業・保管）", expanded=False):
    if not notion_ready:
        st.info("Notion未設定のため編集できません。Secretsに INVOICE_NOTION_PARENT_PAGE_ID を設定してください。")
    else:
        st.caption("費目ごとの単価を編集できます。その他は『課金区分』で 月額定額／単発 を指定でき、"
                   "月額定額は請求時に自動計上、単発はプロンプトで拾います。"
                   "期間限定は有効開始/終了（例 2026-10）で対象月の単価を切替えます。")
        try:
            pm_rows = notion_store.load_price_master(db_ids, client_name)
        except Exception as e:
            pm_rows = []
            st.error(f"単価マスタ読込に失敗: {e}")
        pm_df = pd.DataFrame(pm_rows, columns=[
            "費目", "種別", "単価", "出力品名", "マージン率", "加算額",
            "課金区分", "有効開始", "有効終了", "備考"])
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
                "課金区分": st.column_config.SelectboxColumn(
                    "課金区分", options=["通常", "月額定額", "単発"],
                    help="その他費目: 月額定額=自動計上 / 単発=プロンプトで確認"),
                "有効開始": st.column_config.TextColumn("有効開始(YYYY-MM)"),
                "有効終了": st.column_config.TextColumn("有効終了(YYYY-MM)"),
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

st.header("【3】保管料（2期制：15日・末日）")
st.caption("数量の入力は左メニュー「保管カウント」ページで行います。ここは保存済みカウントの自動計算結果（表示のみ）です。")

target_ym = f"{int(year)}-{int(month):02d}"

# 保管カウント明細を読み込み、種別ごとの2期平均×単価で集計（表示のみ）
storage_lines = {}   # 出力品名 -> 金額合計
storage_preview = []
if notion_ready:
    try:
        _counts = notion_store.load_storage_counts(db_ids, client_name, target_ym)
        _mp = {m["種別名"]: m["単価"] for m in client.get("保管料マスタ", [])}
        _mo = {m["種別名"]: m["出力品名"] for m in client.get("保管料マスタ", [])}
        storage_preview, storage_lines, _swarn = notion_store.aggregate_storage(
            _counts, _mp, _mo)
        for _w in _swarn:
            st.caption(f"（{_w}）")
    except Exception as e:
        st.caption(f"（保管カウントの読込はスキップしました: {e}）")

if storage_preview:
    st.dataframe(pd.DataFrame(storage_preview), use_container_width=True, hide_index=True)
    st.caption(f"保管料 合計: {sum(storage_lines.values()):,} 円")
else:
    st.info(f"{target_ym} の保管カウントがありません。「保管カウント」ページで入力してください。")


# ============================================================
# 4. データ取込・自動算出（NE）
# ============================================================
st.header("【4】データ取込・自動算出")
st.caption("①[NE]出荷確定で受注作業料を、②[NE]受注明細＋商品マスタで出荷作業料を、"
           "③[ヤマト]運賃情報参照で送料・資材費を自動算出し、下の【5】費目に反映します。")

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
ne_df_full = None            # ①結合（送料明細の受注/伝票引き当て用）
detail_souryo = None         # 送料フル明細
detail_shukka = None         # 出荷作業費フル明細
detail_shizai = None         # 資材費明細（配送種別別）
blocking_issues = []         # 価格に紐づかない等、確定をブロックする問題

st.markdown("##### ①[NE]出荷確定 → 受注作業料")
st.caption("出荷件数×受注作業単価で算出します。1000件単位で分割されている場合は複数選択OK。")
ne_ship_files = st.file_uploader(
    "①[NE]出荷確定 CSV（複数選択OK）",
    type=["csv"], key="invoice_ne_ship_m", accept_multiple_files=True)
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
        ne_df_full = ne_df
        c1, c2 = st.columns(2)
        c1.metric("出荷件数（ユニーク伝票番号）", f"{auto_ship_count:,} 件")
        c2.metric("受注作業 単価", f"{juchu_unit:,.0f} 円")
        auto_juchu_amount = round(auto_ship_count * juchu_unit)
        st.success(f"受注作業料 ＝ {auto_ship_count:,}件 × {juchu_unit:,.0f}円 "
                   f"＝ {auto_juchu_amount:,}円（【5】の受注作業料に反映）")
        with st.expander("発送方法別の内訳（参考）", expanded=False):
            md = summary["発送方法別"]
            st.dataframe(
                pd.DataFrame([{"発送方法": k, "件数": v} for k, v in md.items()]),
                use_container_width=True, hide_index=True)
    except Exception as e:
        st.error(f"NE出荷確定CSVの取込に失敗しました: {e}")

# --- ②受注明細＋商品マスタ → 出荷作業料（PCS×サイズ別単価） ---
st.markdown("##### ②[NE]受注明細一覧 → 出荷作業料")
st.caption("出荷作業料はPCS（商品点数）×サイズ別単価で算出します。"
           "②はクライアント別。商品マスタはNE共通で、Driveに保存して毎回のアップは不要です。")

drive_folder = st.secrets.get("INVOICE_GDRIVE_FOLDER_ID", "")
# 商品マスタのバックアップ先（汎用マスタ変換と同じフォルダ・命名 master_YYYYMMDD_NNN.csv）
product_folder = st.secrets.get(
    "PRODUCT_MASTER_FOLDER_ID", "1pQJgn7tYX0KF4x70WY6mlOiruZWPInd-")

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

# 2) Drive保存版（master.csvに項目1が無い場合のフォールバック・最新版を取得）
if prod_df is None and product_folder:
    try:
        f = drive_master.find_latest(product_folder, "master")
        if f:
            prod_df = ne_calc.load_product_master(drive_master.download_bytes(f["id"]))
            prod_meta = f"Drive保存版 {f['name']}（{len(prod_df):,}件）"
            st.session_state["invoice_prod_df"] = prod_df
            st.session_state["invoice_prod_meta"] = prod_meta
    except Exception as e:
        st.caption(f"（Driveの③読込をスキップ: {e}）")

with st.expander("商品マスタの管理（毎回アップ不要・Drive保存）",
                 expanded=(prod_df is None)):
    st.caption("商品マスタは汎用マスタ変換の master.csv と共有できます。"
               "master.csv に「項目1（サイズ）」列を含めて汎用側で更新すれば、"
               "請求書側も自動で最新になります（更新点が1つに）。"
               "項目1列が無い間は、下のDriveアップロード版を使います。")
    if product_folder:
        st.link_button(
            "📁 商品マスタのDriveフォルダを開く",
            f"https://drive.google.com/drive/folders/{product_folder}",
            use_container_width=True)
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
    if not product_folder:
        st.warning("商品マスタのDrive保存先（PRODUCT_MASTER_FOLDER_ID）が未設定のため、"
                   "今回のセッションのみ利用になります。")
    new_prod = st.file_uploader("商品マスタ(NEカスタム)をアップロード／更新",
                                type=["csv"], key="invoice_prod_upload")
    if new_prod is not None:
        data = new_prod.getvalue()
        pdf = None
        try:
            pdf = ne_calc.load_product_master(data)  # 検証（商品コード/項目1必須）
        except Exception as e:
            st.error(f"商品マスタCSVの読込に失敗: {e}")
        if pdf is not None:
            # まずマスタ更新を必ず反映（Drive保存の成否に関係なく）
            prod_df = pdf
            prod_meta = f"アップロード版（{len(pdf):,}件）"
            st.session_state["invoice_prod_df"] = pdf
            st.session_state["invoice_prod_meta"] = prod_meta
            st.success(f"商品マスタを更新しました（{len(pdf):,}件）。出荷作業料の算出に反映されます。")
            # Driveバックアップ（汎用と同じフォルダ・版数付き命名）はベストエフォート
            if product_folder:
                try:
                    _bn = drive_master.upload_versioned(data, "master", product_folder)
                    st.caption(f"Driveにバックアップしました（{_bn}）。")
                except Exception as e:
                    st.warning(f"⚠️ Driveバックアップに失敗しました（マスタ更新は反映済み）: {e}。"
                               "Driveが繰り返し失敗する場合は GOOGLE_REFRESH_TOKEN の再取得が必要かもしれません。")

order_files = st.file_uploader(
    "②[NE]受注明細一覧 CSV（複数選択OK）",
    type=["csv"], key="invoice_ne_order_m", accept_multiple_files=True)
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
            detail_shukka = ne_calc.build_picking_detail(
                order_df, prod_df, ship_rates, issue_date)
            unmatched = pres["未マッチ商品数"]
            if unmatched:
                blocking_issues.append(f"③商品マスタに無い商品が {unmatched} 件")
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
                       f"{sum(pres['サイズ別PCS'].values()):,}）（【5】の出荷作業料に反映）")
            with st.expander("サイズ別PCS（参考）", expanded=False):
                st.dataframe(
                    pd.DataFrame([{"サイズ": k, "PCS": v}
                                  for k, v in pres["サイズ別PCS"].items()]),
                    use_container_width=True, hide_index=True)
            if pres["未登録明細"]:
                blocking_issues.append(
                    f"出荷作業料の単価未登録サイズ {pres['単価未登録サイズ']}")
                st.error(
                    f"⛔ 出荷作業料を計上できなかった商品が {len(pres['未登録明細'])} 件あります"
                    f"（サイズ: {pres['単価未登録サイズ']}）。"
                    "③のサイズ設定または単価マスタ（出荷作業）を追加してください。")
                st.dataframe(
                    pd.DataFrame(pres["未登録明細"]),
                    use_container_width=True, hide_index=True)
        except Exception as e:
            st.error(f"出荷作業料の算出に失敗しました: {e}")

# --- ④ヤマト発行済データ（橋渡し用）。現状は不要なため非表示。
#     必要になったら SHOW_ISSUED = True にすると再表示される。 ---
SHOW_ISSUED = False
if SHOW_ISSUED:
    st.markdown("##### [ヤマト]発行済データCSV（ネコポスの紐付け精度向上）")
    st.caption("ネコポスはNEの発送番号と実際の送り状がズレるため、発行済データで橋渡しします。")
    issued_files = st.file_uploader(
        "[ヤマト]発行済データCSV（複数選択OK）",
        type=["csv"], key="invoice_ya_issued_m", accept_multiple_files=True)
    if issued_files:
        try:
            issued_df, _ierrs = csv_import.load_concat(issued_files, yamato_calc.load_issued)
            for _nm, _er in _ierrs:
                st.error(f"『{_nm}』の読込に失敗: {_er}")
            if issued_df is not None:
                st.caption(f"発行済データ {len(issued_df):,}件を読み込みました。")
        except Exception as e:
            st.error(f"発行済データの取込に失敗しました: {e}")

# --- ③ヤマト運賃（全件）→ 送料・資材費 ---
st.markdown("##### ③[ヤマト]運賃情報参照CSV（全クライアント混在のままでOK）")
st.caption("①でこのクライアント分だけ絞り込み、サイズ別に資材費・送料を算出します。"
           "先に①[NE]出荷確定を取り込んでください。")
ya_files = st.file_uploader(
    "③[ヤマト]運賃情報参照 CSV（複数選択OK）",
    type=["csv"], key="invoice_ya_freight_m", accept_multiple_files=True)
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
            bridge_note = "（①[NE]出荷確定＋発行済で紐付け）" if issued_df is not None else "（①[NE]出荷確定で紐付け）"
            st.info(f"ヤマト運賃 全{n_all:,}件中、このクライアント分 {n_match:,}件を抽出 {bridge_note}。")
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
            # フル明細（送料・資材費）
            detail_souryo = yamato_calc.build_freight_detail(
                matched, ne_df_full, shipping_method=sm2["送料方式"],
                shipping_table=ship_table, area_map=amap2,
                margin_rate=sm2["送料マージン率"], addon=sm2["送料加算額"],
                shime_date=issue_date)
            detail_shizai = pd.DataFrame(
                [{"日付": issue_date, "配送種別": t, "個数": c,
                  "資材単価": mat_rates.get(t, 0), "資材費": round(c * mat_rates.get(t, 0))}
                 for t, c in res["種別別件数"].items()],
                columns=["日付", "配送種別", "個数", "資材単価", "資材費"])
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
                if "未登録" in w or "引けなかった" in w:
                    blocking_issues.append(w)
                    st.error(f"⛔ {w}")
                else:
                    st.warning(w)
        except Exception as e:
            st.error(f"ヤマト運賃CSVの取込・算出に失敗しました: {e}")


# ============================================================
# 5. イレギュラー・その他費目（手入力＋自動算出の反映）
# ============================================================
st.header("【5】その他費目（送料・作業料・値引き等）")
st.caption("自動算出された値は反映済みです。未対応の費目（送料・出荷作業・資材）は当面手入力してください。")

# 受注作業料は自動算出があれば 単価=受注作業単価・数量=出荷件数 をプリフィル
if auto_juchu_amount is not None and auto_ship_count > 0:
    _juchu_row = {"品名": "受注作業料", "単価": juchu_unit, "数量": auto_ship_count}
else:
    _juchu_row = {"品名": "受注作業料", "単価": 0, "数量": 1}

def _auto_row(name, amount):
    """自動算出額があれば 単価=金額・数量=1 でプリフィル、無ければ0。"""
    return {"品名": name, "単価": amount if amount is not None else 0, "数量": 1}

# [汎用]作業料：イレギュラー作業（Notion）の月次合計人時 × 時給単価
_hanyo_unit = 0.0
_hanyo_hours = 0.0
if notion_ready:
    try:
        for r in notion_store.load_price_master(db_ids, client_name):
            if "[汎用]作業料" in str(r.get("出力品名", "")):
                _hanyo_unit = float(r["単価"] or 0)
                break
        _irr = notion_store.load_irregular_work(db_ids, client_name, target_ym)
        _hanyo_hours = sum(float(x["合計時間"] or 0) for x in _irr)
    except Exception as e:
        st.caption(f"（イレギュラー作業の読込をスキップ: {e}）")
if _hanyo_hours > 0:
    _hanyo_row = {"品名": "[汎用]作業料", "単価": _hanyo_unit, "数量": _hanyo_hours}
    st.caption(f"[汎用]作業料: {_hanyo_hours:g}人時 × {_hanyo_unit:,.0f}円 = "
               f"{round(_hanyo_hours * _hanyo_unit):,}円（イレギュラー作業ページの入力を反映）")
else:
    _hanyo_row = {"品名": "[汎用]作業料", "単価": 0, "数量": 1}

# その他費目：月額定額（自動計上）＋単発（プロンプトで確認）
def _ym_valid(start, end, ym):
    """有効開始/終了(YYYY-MM)が対象月ymを含むか。空欄は無制限。"""
    s = str(start or "").strip()
    e = str(end or "").strip()
    if s and ym < s:
        return False
    if e and ym > e:
        return False
    return True

_recurring = []   # 月額定額（自動計上）
_spot_master = []  # 単発候補
if notion_ready:
    try:
        for r in notion_store.load_price_master(db_ids, client_name):
            if r["費目"] != "その他":
                continue
            if "[汎用]作業料" in str(r.get("出力品名", "")):
                continue
            kubun = r.get("課金区分", "通常")
            name = r.get("出力品名") or r.get("種別") or "その他"
            if kubun == "月額定額":
                if _ym_valid(r.get("有効開始"), r.get("有効終了"), target_ym):
                    _recurring.append({"品名": name, "単価": float(r["単価"] or 0), "数量": 1})
            elif kubun == "単発":
                _spot_master.append({"品名": name, "単価": float(r["単価"] or 0)})
    except Exception:
        pass

# 単発作業の確認プロンプト
_spot_rows = []
if _spot_master:
    with st.expander("💡 今月、単発作業はありましたか？（あれば数量を入力）", expanded=True):
        st.caption("単発の作業（シール貼替・化粧箱入替など）。数量を入れた分だけ請求に計上されます。")
        for _i, _sp in enumerate(_spot_master):
            _q = st.number_input(
                f"{_sp['品名']}（@{_sp['単価']:,.0f}円）", min_value=0, step=1, value=0,
                key=f"invoice_spot_{client_name}_{_i}")
            if _q > 0:
                _spot_rows.append({"品名": _sp["品名"], "単価": _sp["単価"], "数量": _q})

if _recurring:
    st.caption("月額定額（自動計上）: "
               + "／".join(f"{r['品名']} {int(r['単価']):,}円" for r in _recurring))

other_default = pd.DataFrame(
    [_auto_row("送料", auto_souryo),
     _auto_row("出荷作業料", auto_shukka),
     _auto_row("資材費", auto_shizai),
     _juchu_row, _hanyo_row]
    + _recurring + _spot_rows
    + [{"品名": "その他", "単価": 0, "数量": 1},
       {"品名": "値引き", "単価": 0, "数量": 1}])
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
st.header("【6】請求内容の確認とCSV出力")

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

# --- 請求前サニティチェック（異常検知） ---
_amount = {it["品名"]: it["金額"] for it in items}
_checks = []
if auto_ship_count > 0:
    if not _amount.get("送料"):
        _checks.append("出荷があるのに**送料が0**です（③ヤマト運賃の取込漏れ？）。")
    if not _amount.get("出荷作業料"):
        _checks.append("出荷があるのに**出荷作業料が0**です（②受注明細・商品マスタの取込漏れ？）。")
    if not _amount.get("資材費"):
        _checks.append("出荷があるのに**資材費が0**です。")
if not storage_lines:
    _checks.append(f"**保管料が0**です（{target_ym}の保管カウント未入力？）。")
if total <= 0:
    _checks.append("**合計金額が0以下**です。費目を確認してください。")
for it in items:
    if it["品名"] not in ("値引き",) and it["金額"] < 0:
        _checks.append(f"**{it['品名']}がマイナス**です（{it['金額']:,}円）。意図通りか確認を。")
# 前月比
if notion_ready:
    try:
        _y, _m = int(target_ym[:4]), int(target_ym[5:7])
        _pm_ym = f"{_y - 1}-12" if _m == 1 else f"{_y}-{_m - 1:02d}"
        _prev = notion_store.load_issue_history(db_ids, client_name, _pm_ym)
        _prev = [r for r in _prev if r["区分"] == "請求"]
        if _prev:
            _pt = _prev[0]["合計金額"]
            if _pt and abs(total - _pt) / _pt > 0.5:
                _checks.append(
                    f"前月（{_pm_ym}）の合計 {int(_pt):,}円 と **{(total / _pt - 1) * 100:+.0f}%** 乖離。確認推奨。")
    except Exception:
        pass
if _checks:
    with st.expander(f"⚠️ 請求前チェック：気になる点が {len(_checks)}件", expanded=True):
        for _c in _checks:
            st.warning(_c)
else:
    st.success("✅ 請求前チェック：明らかな異常は見つかりませんでした。")

# CSV生成（取引先情報は【2】の入力＝マスタ由来の値を使用）
header = {
    "取引先名称": corp_name,
    "件名": subject,
    "請求日": issue_date,
    "お支払期限": due_date,
    "請求書番号": inv_no,
    "売上計上日": sales_date,
    "取引先敬称": keisho,
    "取引先郵便番号": zip_code,
    "取引先都道府県": pref,
    "取引先住所1": addr1,
    "取引先住所2": addr2,
    "自社担当者氏名": staff,
    "備考": biko,
    "振込先": furikomi,
}

from lib.invoice import excel_export

# --- MFクラウド取込用CSV ---
enc_label = st.radio("文字コード（MF CSV）", ["UTF-8(BOM付き)", "Shift-JIS(cp932)"],
                     horizontal=True, key="invoice_enc")
encoding = "cp932" if enc_label.startswith("Shift") else "utf-8-sig"
csv_bytes = mf_export.to_csv_bytes(header, items, encoding=encoding)
csv_name = f"MF請求書_{client_name}_{inv_no}.csv"

# --- 内訳明細書（Excel）の組み立て ---
_stk_detail = None
_irr_detail = None
if notion_ready:
    try:
        _counts = notion_store.load_storage_counts(db_ids, client_name, target_ym)
        _mp = {m["種別名"]: m["単価"] for m in client.get("保管料マスタ", [])}
        _stk_detail = pd.DataFrame([
            {"カウント日": r["カウント日"], "種別": r["種別"], "エリア": r.get("エリア", ""),
             "ロケーション": r["ロケーション"], "数量": r["数量"],
             "単価": _mp.get(r["種別"], 0),
             "小計": round(float(r["数量"] or 0) * float(_mp.get(r["種別"], 0) or 0)),
             "備考": r["備考"]} for r in _counts],
            columns=["カウント日", "種別", "エリア", "ロケーション", "数量", "単価", "小計", "備考"])
        _irr = notion_store.load_irregular_work(db_ids, client_name, target_ym)
        _irr_detail = pd.DataFrame([
            {"日付": r["日付"], "時間数": r["時間数"], "人数": r["人数"],
             "合計時間": r["合計時間"], "作業項目": r["作業項目"],
             "作業詳細": r["作業詳細"], "備考": r["備考"]} for r in _irr],
            columns=["日付", "時間数", "人数", "合計時間", "作業項目", "作業詳細", "備考"])
    except Exception:
        pass

_auto_names = {"送料", "出荷作業料", "資材費", "受注作業料", "[汎用]作業料"}
_other_detail = pd.DataFrame(
    [{"品名": str(r.get("品名", "")).strip(), "単価": r.get("単価"), "数量": r.get("数量"),
      "金額": round(float(r.get("単価") or 0) * float(r.get("数量") or 0))}
     for _, r in other_edited.iterrows()
     if str(r.get("品名", "")).strip() and str(r.get("品名", "")).strip() not in _auto_names],
    columns=["品名", "単価", "数量", "金額"])

summary_rows = [{"費目": it["品名"], "金額": int(it["金額"])} for it in items]
detail_sheets = [
    ("保管費", _stk_detail, "小計"), ("送料", detail_souryo, "金額"),
    ("出荷作業費", detail_shukka, "計"), ("資材費", detail_shizai, "資材費"),
    ("汎用作業費", _irr_detail, None), ("その他", _other_detail, "金額"),
]
try:
    xlsx_bytes = excel_export.build_breakdown_excel(summary_rows, detail_sheets)
except Exception as e:
    xlsx_bytes = None
    st.error(f"内訳Excelの生成に失敗しました: {e}")
xlsx_name = f"内訳明細_{client_name}_{inv_no}.xlsx"

# --- アップロード元ファイル（①②③）を収集（Driveバックアップ用） ---
_src_files = []
for _label, _flist in (("①", ne_ship_files), ("②", order_files), ("③", ya_files)):
    for _f in (_flist or []):
        _src_files.append((f"{_label}_{_f.name}", _f.getvalue()))

drive_folder = st.secrets.get("INVOICE_GDRIVE_FOLDER_ID", "")
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

st.subheader("ダウンロード・確定")

# 作業用リンク（CSVアップロード元＝MF共通／内訳格納先＝クライアント別）
_mf_upload_url = st.secrets.get("MF_UPLOAD_URL", "https://invoice.moneyforward.com/billings")
_breakdown_link = client["header"].get("内訳格納リンク", "")
lk1, lk2 = st.columns(2)
with lk1:
    st.link_button("🔗 MF請求書のCSVアップロード元を開く", _mf_upload_url,
                   use_container_width=True)
with lk2:
    if _breakdown_link:
        st.link_button(f"🔗 内訳明細の格納先を開く（{client_name}）", _breakdown_link,
                       use_container_width=True)
    else:
        st.caption("内訳明細の格納先リンク未設定（下で設定）")
if drive_folder:
    st.link_button(
        "📁 請求確定のバックアップフォルダを開く",
        f"https://drive.google.com/drive/folders/{drive_folder}",
        use_container_width=True)
with st.expander("内訳明細の格納先リンク設定（クライアント別）", expanded=False):
    _b = st.text_input("内訳明細の格納先リンク（このクライアント用）",
                       value=_breakdown_link, key="invoice_link_bd")
    if st.button("💾 リンクを保存", key="invoice_link_save", disabled=not notion_ready):
        try:
            notion_store.save_client_links(db_ids, client_name, "", _b.strip())
            st.session_state.pop("invoice_clients_cache", None)
            st.success("リンクを保存しました。再読込で反映されます。")
        except Exception as e:
            st.error(f"保存に失敗しました: {e}")

_missing = [n for n, d, _ in detail_sheets if d is None or len(d) == 0]
if _missing:
    st.caption(f"（内訳で明細が空のシート: {', '.join(_missing)}　※①②③を取り込むと埋まります）")

_cflash = st.session_state.pop("invoice_confirm_flash", None)
if _cflash:
    getattr(st, _cflash[0])(_cflash[1])

# 価格に紐づかないデータがある場合は確定をブロック
if blocking_issues:
    st.error("⛔ 価格に紐づかないデータがあるため確定できません。"
             "以下を解消してから確定してください：\n・" + "\n・".join(blocking_issues))

# 確定ボタン：1クリックで「CSV＋内訳のDL／請求履歴保存／Driveバックアップ」を実行
if st.button("📦 請求を確定（CSV・内訳をDL＋履歴保存＋Driveバックアップ）",
             key="invoice_confirm", type="primary", use_container_width=True,
             disabled=bool(blocking_issues)):
    msgs = []
    ok = True
    if notion_ready:
        try:
            notion_store.save_issue_history(
                db_ids, invoice_no=inv_no, client_name=client_name,
                target_ym=target_ym, kind="請求", issue_date=issue_date,
                due_date=due_date, subtotal=subtotal, tax=tax, total=total, items=items)
            msgs.append(f"請求履歴を保存（{inv_no}）")
        except Exception as e:
            ok = False
            msgs.append(f"履歴保存に失敗: {e}")
    if drive_folder:
        try:
            _stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            sub = drive_master.get_or_create_folder(
                f"{inv_no}_{client_name}_{_stamp}", drive_folder)
            drive_master.upload_bytes(csv_bytes, csv_name, sub, "text/csv")
            if xlsx_bytes:
                drive_master.upload_bytes(xlsx_bytes, xlsx_name, sub, _XLSX_MIME)
            for _n, _b2 in _src_files:
                drive_master.upload_bytes(_b2, _n, sub, "text/csv")
            msgs.append(f"Driveへ保存（{inv_no}_{client_name}_{_stamp}・{2 + len(_src_files)}件）")
        except Exception as e:
            ok = False
            msgs.append(f"Driveバックアップに失敗: {e}（Driveが繰り返し失敗する場合は "
                        "GOOGLE_REFRESH_TOKEN の再取得が必要かもしれません）")
    else:
        msgs.append("INVOICE_GDRIVE_FOLDER_ID 未設定のためDriveバックアップはスキップ")
    st.session_state["invoice_confirm_flash"] = (
        "success" if ok else "warning", "／".join(msgs))
    st.session_state["invoice_confirmed_no"] = inv_no
    st.session_state["invoice_autodl_pending"] = True  # ブラウザ自動DLを1回だけ起動
    st.rerun()

# 確定直後：ブラウザ側でCSVと内訳Excelを自動ダウンロード（ZIPなし・1クリック相当）
if st.session_state.pop("invoice_autodl_pending", False):
    import base64 as _b64
    import streamlit.components.v1 as _components
    _csv_b64 = _b64.b64encode(csv_bytes).decode()
    _xlsx_b64 = _b64.b64encode(xlsx_bytes or b"").decode()
    _html = f"""
    <script>
    function _dl(name, mime, b64){{
      var a=document.createElement('a');
      a.href='data:'+mime+';base64,'+b64; a.download=name;
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
    }}
    _dl({csv_name!r}, 'text/csv', {_csv_b64!r});
    setTimeout(function(){{ _dl({xlsx_name!r}, {_XLSX_MIME!r}, {_xlsx_b64!r}); }}, 700);
    </script>
    """
    _components.html(_html, height=0)
    st.success("CSVと内訳Excelのダウンロードを開始しました"
               "（ブラウザが『複数ファイルのDLを許可しますか？』と聞いたら許可してください）。")

# 自動DLが効かない環境向けの手動DL（フォールバック）
if st.session_state.get("invoice_confirmed_no") == inv_no:
    with st.expander("ダウンロードされない場合（手動DL）", expanded=False):
        d1, d2 = st.columns(2)
        with d1:
            st.download_button("⬇️ MF請求書CSV", data=csv_bytes, file_name=csv_name,
                               mime="text/csv", key="invoice_dl_csv",
                               use_container_width=True)
        with d2:
            st.download_button("⬇️ 内訳明細書（Excel）",
                               data=(xlsx_bytes or b""), file_name=xlsx_name,
                               mime=_XLSX_MIME, key="invoice_dl_xlsx",
                               use_container_width=True, disabled=(xlsx_bytes is None))
else:
    st.caption("※ 「請求を確定」を押すと、CSVと内訳ExcelのDL・請求履歴の保存・"
               "Driveバックアップ（MF CSV・内訳・①②③元ファイル）を一度に行います。")
