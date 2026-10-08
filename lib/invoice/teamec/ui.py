"""TeamEC新体系（Team-EC 2026年9月作業分〜）の画面。外部書込み・MF請求書発行は行わない。

上から順に確認する縦長の画面。各段は「確認済み」にすると1行の要約にたためる（タブは見落としやすいため）。
・出荷作業料・資材費・送料・出荷稼働日・FBA・ピース入庫：Eシス・B2・ヤマトの照合結果（teamec-billing-fetch）
・随時連絡・返品（着払い送料）・配送変更・新商品 初期設定：共有シートとEシスの棚番の記録から行を入れ、人が確認して直す
・依頼台帳：その月の依頼を「たたき台」で並べ、計上するものに数量・単価を入れてもらう
・保管・汎用作業：既存の保管カウント／イレギュラー作業ページのNotion記録（ここで再入力させない）
照合結果が読めないときは金額を出さない（0円扱いにしない）。最後の段でMF取込CSV・内訳・確定（発行履歴＋Drive）。
"""
from __future__ import annotations

import copy
import csv
import io
import json
import os
import zipfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
import streamlit as st

from . import rules as R

RESULT_FOLDER = "TeamEC実績"   # 請求書バックアップフォルダ配下。teamec-billing-fetch が追記保存する


def _records(frame):
    return frame.astype(object).where(pd.notna(frame), "").to_dict("records")


def review_bundle(result, inputs, ver):
    """自社で保持できる試算スナップショット。正式なMF CSVは含めない。"""
    def serialize(value):
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, tuple):
            return list(value)
        raise TypeError(type(value).__name__)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        payload = {"status": "試算・請求不可", "created_at": datetime.now().astimezone().isoformat(),
                   "inputs": inputs, "result": result, "rate_version": ver, "source": R.SOURCE}
        z.writestr("試算根拠.json", json.dumps(payload, ensure_ascii=False, indent=2, default=serialize))
        table = io.StringIO()
        writer = csv.DictWriter(table, fieldnames=["区分", "品名", "単価", "数量", "単位", "金額", "詳細"],
                                lineterminator="\r\n")
        writer.writeheader()
        writer.writerows(result["items"])
        z.writestr("試算明細_MF取込不可.csv", table.getvalue().encode("utf-8-sig"))
    return buf.getvalue()


def _load(label, loader):
    """Notionの記録を読む。読めなければNone（＝不明）。0件とは区別する。"""
    try:
        return loader()
    except Exception as exc:
        st.error(f"{label}を読めませんでした（0件扱いにはしません）: {exc}")
        return None


def _drive_result(target_ym):
    """Driveの「TeamEC実績」から対象月の最新の照合結果を読む。無ければ (None, 理由)。"""
    local = os.environ.get("TEAMEC_RESULTS_DIR")  # ローカル確認用（teamec-billing-fetch/data/results）
    if local:
        files = sorted(Path(local, target_ym).glob("*_照合結果.json"))
        if not files:
            return None, f"{local}\\{target_ym} に照合結果がありません"
        return json.loads(files[-1].read_text(encoding="utf-8")), f"ローカル {files[-1].name}"
    from lib.invoice import drive_master
    # teamec-billing-fetch が同じOAuthクライアントで作るフォルダ（マイドライブ直下）を名前で探す。
    # drive.file では請求書バックアップフォルダの配下に置けないため、親フォルダでは絞らない（2026-10-03）
    q = f"name = '{RESULT_FOLDER}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    found = drive_master._service().files().list(q=q, fields="files(id, createdTime)", orderBy="createdTime",
                                                pageSize=10).execute().get("files", [])
    if not found:
        return None, f"Driveに「{RESULT_FOLDER}」フォルダが見つかりません"
    sub = found[0]
    files = [f for f in drive_master.list_files(sub["id"], f"TeamEC実績_{target_ym}_") if f["name"].endswith(".json")]
    if not files:
        return None, f"Driveの「{RESULT_FOLDER}」に {target_ym} の照合結果がありません"
    latest = sorted(files, key=lambda f: f["name"])[-1]
    return json.loads(drive_master.download_bytes(latest["id"]).decode("utf-8")), f"Drive {latest['name']}"


def _yen(value) -> str:
    return f"{int(value):,}円"


def _num(value) -> str:
    return f"{float(value):,.10g}"


def _table(rows, cols=None, first=()):
    """折り返して全部見える表（横スクロール・省略表示なし）。列順は「名称 → 金額 → 理由」。左端の列が名称。

    cols：出す列と順番。first：列が決まっていない表で、先頭に寄せる列。
    """
    df = pd.DataFrame(rows)
    if cols:
        df = df[[c for c in cols if c in df.columns]]
    elif first:
        df = df[[c for c in first if c in df.columns] + [c for c in df.columns if c not in first]]
    for c in df.columns:   # 11.0000 のような表示にしない
        if pd.api.types.is_float_dtype(df[c]):
            df[c] = df[c].map(lambda v: "" if pd.isna(v) else _num(v))
    if len(df.columns) and len(df):
        st.table(df.set_index(df.columns[0]))


# 確認表の列順と幅（px）。名称 → 金額 → 計上 → 理由。1列は1つの意味だけ。幅の合計は横スクロールが出ない範囲（約950px）に収める
SHEET_COLUMNS = {
    "FBA": {"受注番号": 130, "金額": 90, "計上": 60, "発送日": 110, "注文ID": 110},
    "随時連絡": {"発生内容": 150, "金額": 90, "計上": 60, "発生日": 110, "受注番号": 210, "ステータス": 130, "送り状番号": 150},
    "返品": {"送り状番号": 200, "返品処理料": 90, "計上する送料": 105, "計上": 55, "送料の状態": 85, "ヤマト請求額": 90,
             "パピー記入欄": 85, "運送会社": 85, "送料負担": 70, "到着日": 95},
    "配送変更": {"注文ID": 110, "金額": 90, "計上": 60, "日付": 110, "変更前": 150, "変更後": 150, "伝票番号欄": 120},
    "新商品": {"表示用コード": 130, "金額": 90, "計上": 60, "商品名": 200, "棚番": 110, "確認日": 110, "根拠": 250},
}
MONEY_COLUMNS = ("金額", "返品処理料", "計上する送料", "ヤマト請求額")
ATTENTION = "background-color: #fff3b0; color: #1a1a1a"   # 入力・確認が要る行の色


def _confirm_table(key, name, rows, edit=(), attention=None):
    """確認表。どの表も同じ形：名称 → 金額 → 計上 → 理由。直せるのは「計上」と edit の列だけ。

    attention(row) が True の行は色をつけて、入力・確認を促す（色は直せない列に付く。Streamlit の仕様）。
    """
    widths = SHEET_COLUMNS[name]
    df = pd.DataFrame(rows)
    df["計上"] = df["計上"].map(lambda v: v is True or v == 1).astype(bool)
    for c in MONEY_COLUMNS:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c])
    config = {}
    for c, w in widths.items():
        if c == "計上":
            config[c] = st.column_config.CheckboxColumn(c, width=w)
        elif c in MONEY_COLUMNS:
            config[c] = st.column_config.NumberColumn(c, width=w, format="%d円", min_value=0, step=1)
        else:
            config[c] = st.column_config.Column(c, width=w)
    data = df
    if attention is not None:
        data = df.style.apply(lambda r: [ATTENTION if attention(r) else ""] * len(r), axis=1)
    out = st.data_editor(data, hide_index=True, key=key, width="stretch", column_order=list(widths),
                         disabled=[c for c in df.columns if c not in ("計上", *edit)], column_config=config)
    return _records(out)


def _section(key, title, summary):
    """縦に並ぶ1段。「確認済み」にすると要約1行にたたむ。戻り値 True のとき中身を描く。"""
    st.markdown(f"#### {title}")
    done = st.checkbox("確認済み（たたむ）", key=key)
    if done:
        st.caption("✅ " + summary)
    return not done


def render(year, month, client, db_ids=None, notion_ready=False):
    from lib.invoice import notion_store

    year, month = int(year), int(month)
    ver = R.version(year, month)
    disp = R.dispatch(ver)
    target_ym = f"{year}-{month:02}"
    st.subheader(f"Team-EC 新体系（{ver['id']}）")
    st.caption("パピー用 · 2026年9月作業分から自動で新体系になります。8月以前は従来の計算です。"
               "上から順に確認し、済んだ段は「確認済み」でたためます。")
    st.info("出荷・FBA・随時連絡・返品・配送変更・新商品は、Eシス・B2・ヤマトと共有シートから自動で入ります。"
            "人が見るのは「4. 依頼ごとの作業」（単価の決まった作業の確認 → 依頼台帳の判別）、「5. 汎用作業」、「6. 控除」です。"
            "最後に「8」でMF取込CSVを出して確定します。")

    # ---- 1. 実績（Eシス・B2・ヤマトの照合結果）----
    result, source = None, ""
    uploaded = None
    try:
        result, source = _drive_result(target_ym)
    except Exception as exc:
        source = f"Driveを読めませんでした：{exc}"
    st.markdown("#### 1. 出荷実績（Eシス・B2・ヤマトの照合結果）")
    if result is None:
        st.warning(f"照合結果を読み込めていません（{source}）。金額は出しません。")
        uploaded = st.file_uploader("照合結果ファイル（*_照合結果.json）を手で読み込む", type=["json"], key=f"teamec_result_upload_{year}_{month}")
        if uploaded is not None:
            result, source = json.loads(uploaded.getvalue().decode("utf-8")), f"アップロード {uploaded.name}"
    if result is not None:
        try:
            R.check_result(result, year, month)
        except ValueError as exc:
            st.error(str(exc))
            result = None
    if result is not None:
        must = [i for i in result["要確認"] if i.get("重さ") != "参考"]
        cols = st.columns(4)
        cols[0].metric("出荷作業料", f"{result['出荷作業料']:,}円")
        cols[1].metric("資材費", f"{result['資材費']:,}円")
        cols[2].metric("送料（税別）", f"{result['送料合計']:,}円")
        cols[3].metric("要確認", f"{len(must)}件")
        st.caption(f"読込元：{source}／作成 {result.get('作成日時', '')}／注文{result.get('注文数', 0):,}件・"
                   f"{result.get('商品数', 0):,}個・{result.get('個口数', 0):,}個口")
        if must:
            st.error("確定前に確認が必要な項目があります。")
            _table(must, ["区分", "内容", "注文ID", "注文番号", "伝票番号", "表示用コード", "JANコード"])
        with st.expander("内訳（商品サイズ別PCS・資材・学習した項目1）"):
            st.write("出荷作業料（商品サイズ別PCS）", result["出荷作業料_サイズ別"])
            _table(result["資材費_サイズ別"], ["サイズ", "金額", "個口数", "単価"])
            irregular = result.get("棚番と項目1が違う商品", [])
            if irregular:
                st.caption("棚番と発送サイズ（項目1）が違う商品。1注文1個の出荷実績から学習した値で出荷作業料を計算しています。")
                _table(irregular, ["表示用コード", "項目1", "棚番", "根拠", "JANコード"])

    # 入力の下書きは「対象月×照合結果」ごと。照合結果を後から読み込んだら、稼働日などの初期値を作り直す
    draft_key = f"{target_ym}|{(result or {}).get('作成日時', 'none')}"
    drafts = st.session_state.setdefault("_teamec_drafts", {})
    if st.session_state.get("_teamec_active") != draft_key:
        st.session_state["_teamec_active"] = draft_key
        st.session_state["_teamec_generation"] = st.session_state.get("_teamec_generation", 0) + 1
        if drafts.get(draft_key):
            carried = drafts[draft_key]
        else:  # 初めて開く照合結果：シート由来の行を入れ直し、手で入れた行だけ引き継ぐ
            carried = {k: v for k, v in drafts.get(f"{target_ym}|none", {}).items() if k not in ("calendar", "ledger", "sheets")}
            carried["_fresh"] = True
        st.session_state["_teamec_form_base"] = copy.deepcopy(carried)
    base = st.session_state["_teamec_form_base"]   # 編集欄の初期値（切り替えるまで固定。変えると入力が消える）
    prev = drafts.get(draft_key, base)               # たたんだ段は直前の入力値を使う
    prefix = f"teamec_new_{year}_{month}_{st.session_state['_teamec_generation']}_"

    # ---- Notion（保管・汎用作業）----
    if notion_ready:
        counts = _load("保管カウント", lambda: notion_store.load_storage_counts(db_ids, R.CLIENT_NAME, target_ym))
        irregular_work = _load("イレギュラー作業", lambda: notion_store.load_irregular_work(db_ids, R.CLIENT_NAME, target_ym))
    else:
        st.error("Notion未接続のため、保管カウント・イレギュラー作業を読めません（0件扱いにはしません）。")
        counts = irregular_work = None

    # ---- 2. 出荷稼働日 ----
    default_days = set(result["出荷稼働日"]) if result else None
    cal_rows = copy.deepcopy(base.get("calendar"))
    if cal_rows is None:
        cal_rows = R.calendar_rows(year, month)
        if default_days is not None:
            for r in cal_rows:
                r["稼働"] = r["日付"] in default_days
    shown_rows = prev.get("calendar") or cal_rows
    active = [r["日付"] for r in shown_rows if r["稼働"]]
    summary = f"{len(active)}日（出荷指示作成料 {sum(r['定額回数'] for r in cal_rows if r['稼働'])}回）"
    if _section(prefix + "done_days", "2. 出荷稼働日", summary):
        st.caption(f"出荷指示作成料は1回{disp['price']:,}円。出荷した稼働日のみ、平日{disp['weekday']}回・"
                   f"土日祝{disp['holiday']}回で固定します。"
                   + ("Eシスで発送のあった日を選んでいます。" if default_days is not None else "照合結果が無いため平日を仮に選んでいます。"))
        cal = st.data_editor(pd.DataFrame(cal_rows), hide_index=True, disabled=["日付", "曜日", "祝日", "定額回数"],
                             column_config={"稼働": st.column_config.CheckboxColumn("出荷稼働")},
                             key=prefix + "calendar", width=430)
        shown_rows = _records(cal)
        active = [r["日付"] for r in shown_rows if r["稼働"]]

    # ---- 3. 保管 ----
    storage_sum, storage_err = None, ""
    if counts is not None:
        try:
            storage_sum = R.storage_summary(counts, year, month)
        except ValueError as exc:
            storage_err = str(exc)
    summary = ("Notionを読めていません" if counts is None else storage_err or
               f"保管料 {sum(r['金額'] for r in storage_sum):,}円（{len(counts)}行）")
    if _section(prefix + "done_storage", "3. 保管（保管カウントページの記録）", summary):
        if counts is not None:
            st.caption(f"{target_ym} のカウント状況（2期平均→保管料）。入力・修正は左メニュー「保管カウント」。"
                       "600円パレットは「保管料：当社指定ロケーション」。")
            if storage_err:
                st.error(storage_err)
            elif storage_sum:
                _table([{**r, "金額": _yen(r["金額"]), "単価": _yen(r["単価"])} for r in storage_sum],
                       ["種別", "金額", "単価", "平均", "第1期合計", "第2期合計"])
                st.caption(f"保管料 合計：{sum(r['金額'] for r in storage_sum):,}円")
            else:
                st.warning(f"{target_ym} の保管カウントが0行です。未入力でないか確認してください。")
            if counts:
                with st.expander(f"明細（{len(counts)}行）"):
                    _table(pd.DataFrame(counts).drop(columns=["id"], errors="ignore"), first=("種別", "数量", "期"))

    # ---- 4. 依頼ごとの作業（シートごとの確認表 → 依頼台帳）----
    choices = R.event_choices(ver)
    ledger_missing = R.ledger_state(result)
    links = ((result or {}).get("台帳") or {}).get("出典", {})
    columns = ["作業", "数量", "依頼ID", "実施日", "根拠・備考"]
    initial_events = [{**{c: r.get(c) for c in columns}, "作業": R.choice_label(r, ver)} for r in base.get("events", [])
                      if "（自動）" not in str(r.get("根拠・備考", "")) and not str(r.get("根拠・備考", "")).startswith(R.AUTO_MARKS)]
    new_items = (result or {}).get("新商品") or {}
    manual_rows = prev.get("events", initial_events)
    ledger_cols = ["依頼No", "品目", "数量", "単価", "メモ"]
    requests = R.ledger_requests(result)
    ledger_init = base.get("ledger") if base.get("ledger") is not None else R.ledger_rows(result)
    ledger_now = prev.get("ledger", ledger_init)
    sheets_init = base.get("sheets") if base.get("sheets") is not None else R.sheet_tables(result, ver)
    sheets_now = dict(prev.get("sheets") or sheets_init)
    cod_extra = prev.get("other", {}).get("cod", 0)
    new_notice = bool(prev.get("new_notice", False))

    def _sheet_lines(name):
        return R.event_lines(R.sheet_event_rows({name: sheets_now[name]}, target_ym, new_notice), year, month)

    def _request_total():
        """この段の金額（単価の決まった作業＋着払い送料＋依頼台帳）。入力が足りなければ ValueError。"""
        ev = R.event_lines(R.sheet_event_rows(sheets_now, target_ym, new_notice) + R.choice_rows(manual_rows, ver), year, month)
        led = R.ledger_lines(ledger_now, year, month)
        cod = R.return_freight(sheets_now["返品"])[0] + cod_extra
        return sum(x["金額"] for x in ev), cod, sum(x["金額"] for x in led)

    try:
        ev_sum, cod_sum, led_sum = _request_total()
        summary = (f"単価の決まった作業 {ev_sum:,}円・返品の着払い送料 {cod_sum:,}円・依頼台帳 {led_sum:,}円"
                   f"（計 {ev_sum + cod_sum + led_sum:,}円）")
    except ValueError as exc:
        summary = f"未確定：{exc}"
    if _section(prefix + "done_events", "4. 依頼ごとの作業（依頼された内容と金額）", summary):
        st.caption("確認する元ごとに、見出し → 確認表 → 小計の順です。どの表も「名称 → 金額 → 計上 → 理由」の並びで、"
                   "請求から外す行は「計上」のチェックを外します。色のついた行は、入力・確認が要る行です。")
        if ledger_missing:
            st.warning(f"共有シート（随時連絡・返品交換・配送変更依頼・依頼台帳）は自動で入っていません（{ledger_missing}）。"
                       "0件とは限りません。シートを見て「そのほかの作業」に入力してください。")

        st.markdown("##### FBA対応費 ｜ Eシス（受注番号 FBA00xx）")
        if sheets_init["FBA"]:
            sheets_now["FBA"] = _confirm_table(prefix + "sh_fba", "FBA", sheets_init["FBA"])
        elif result is None:
            st.caption("照合結果を読み込むと表示されます。")
        st.caption(f"小計：{sum(x['金額'] for x in _sheet_lines('FBA')):,}円（{len(sheets_init['FBA'])}件）")

        if not ledger_missing:
            st.markdown(f"##### 随時連絡 ｜ [シートを開く]({links.get('随時連絡', '')})")
            if sheets_init["随時連絡"]:
                sheets_now["随時連絡"] = _confirm_table(prefix + "sh_contacts", "随時連絡", sheets_init["随時連絡"])
            st.caption(f"小計：{sum(x['金額'] for x in _sheet_lines('随時連絡')):,}円"
                       f"（発生日が {target_ym} の行 {len(sheets_init['随時連絡'])}件）")

            st.markdown(f"##### 返品交換 ｜ [シートを開く]({links.get('返品交換', '')})")
            if sheets_init["返品"]:
                st.caption("送料の状態が「確定」＝送り状番号がヤマトの請求にあった金額（税別）。「仮」（色のついた行）＝ヤマトの請求に無く、"
                           "シートのパピー記入欄の金額を仮に入れています。運送会社の列の会社（佐川・日本郵便）の請求を見て、"
                           "「計上する送料」を直してください。")
                sheets_now["返品"] = _confirm_table(prefix + "sh_returns", "返品", sheets_init["返品"], edit=("計上する送料",),
                                                    attention=lambda r: r["送料の状態"] == "仮")
            cod_extra = st.number_input("返品交換シートに無い着払い送料（税抜合計・円）", min_value=0, value=cod_extra, step=1,
                                        key=prefix + "cod")
            try:
                fee_sum = sum(x["金額"] for x in _sheet_lines("返品"))
                freight_sum = R.return_freight(sheets_now["返品"])[0] + cod_extra
                st.caption(f"小計：{fee_sum + freight_sum:,}円（返品処理料 {fee_sum:,}円＋着払い送料 {freight_sum:,}円。"
                           f"到着日が {target_ym} の行 {len(sheets_init['返品'])}件）")
            except ValueError as exc:
                st.error(str(exc))

            st.markdown(f"##### 配送変更依頼 ｜ [シートを開く]({links.get('配送変更依頼', '')})")
            if sheets_init["配送変更"]:
                sheets_now["配送変更"] = _confirm_table(prefix + "sh_changes", "配送変更", sheets_init["配送変更"])
            st.caption(f"小計：{sum(x['金額'] for x in _sheet_lines('配送変更')):,}円"
                       f"（伝票番号がEシスの注文IDに当たる行 {len(sheets_init['配送変更'])}件）")

        st.markdown("##### 新商品 初期設定 ｜ Eシスの棚番")
        if new_items.get("状態") == "取得済":
            lines_new = _sheet_lines("新商品")
            unit_new = int(lines_new[0]["単価"]) if lines_new else int(R.price("新商品 初期設定", ver=ver))
            if sheets_init["新商品"]:
                sheets_now["新商品"] = [{k: v for k, v in r.items() if k != "金額"} for r in _confirm_table(
                    prefix + "sh_new", "新商品", [{**r, "金額": unit_new} for r in sheets_init["新商品"]])]
                new_notice = st.checkbox("14日前までに事前通知があった（5SKU以上・10SKU以上で単価が下がる）", value=new_notice,
                                         key=prefix + "new_notice")
            lines_new = _sheet_lines("新商品")
            st.caption(f"小計：{sum(x['金額'] for x in lines_new):,}円"
                       + (f"（{int(lines_new[0]['単価']):,}円×{float(lines_new[0]['数量']):g}SKU）" if lines_new else "")
                       + f"　{new_items.get('説明', '')}")
        else:
            st.warning("新商品 初期設定は自動で数えられていません（" + new_items.get("説明", "この照合結果は未対応") + "）。"
                       "ある場合は下の「そのほかの作業」に足してください。")

        st.markdown("##### そのほかの作業 ｜ 手で足すもの（車両受入・追加便など）")
        st.caption("作業は単価つきで選びます。事前通知（14日前まで）や時間帯外の到着で単価が変わる作業は、選択肢を分けています。"
                   "ピース入庫はEシス入庫履歴から自動計上するため、ここには入れないでください。")
        events_df = pd.DataFrame(initial_events, columns=columns) if initial_events else pd.DataFrame(
            {c: pd.Series(dtype="float64" if c == "数量" else "str") for c in columns})
        events_df["数量"] = pd.to_numeric(events_df["数量"].replace("", None))
        events = st.data_editor(
            events_df, num_rows="dynamic", hide_index=True, key=prefix + "events", width="stretch",
            column_config={
                "作業": st.column_config.SelectboxColumn("作業（単価）", options=list(choices), required=True, width=360),
                "数量": st.column_config.NumberColumn("数量", min_value=0, step=1, default=1, width=70),
                "依頼ID": st.column_config.TextColumn("依頼ID", help="注文ID・送り状番号・受注番号など、依頼を特定できるもの", width=150),
                "実施日": st.column_config.TextColumn("実施日", help="YYYY-MM-DD", width=110),
                "根拠・備考": st.column_config.TextColumn("根拠・備考", width=220),
            })
        manual_rows = _records(events)
        try:
            shown_ev = R.event_lines(R.choice_rows(manual_rows, ver), year, month)
            if shown_ev:
                st.caption(R.amount_text(shown_ev))
            st.caption(f"小計：{sum(x['金額'] for x in shown_ev):,}円")
        except ValueError as exc:
            st.error(str(exc))

        st.markdown(f"##### 依頼台帳 ｜ [シートを開く]({links.get('依頼台帳', '')})" if not ledger_missing
                    else "##### 依頼台帳")
        if not ledger_missing:
            st.markdown(f"**{target_ym} の依頼：{len(requests)}件**（出荷日。無ければ依頼日がこの月のもの）")
            for q in requests:
                note = "／".join(x for x in (q.get("対応状況"), q.get("作業費（台帳）"), q.get("パピー記入")) if x)
                st.markdown(f"- **No.{q['依頼No']}（{q['日付']}）** {q['内容']}" + (f"　〔台帳の記入：{note}〕" if note else ""))
        st.caption("下が請求のたたき台です。品目は文面からの候補なので、確認して、変更・行の追加・削除をしてください"
                   "（1つの依頼に複数の品目があれば行を足す）。汎用作業料は人時（0.25刻み・単価2,100円固定）、"
                   "実費（チャーター・資材など）は数量と単価（税抜）を入れます。**請求しない依頼は品目を空にする（または行を削除）。** "
                   "台帳で「通常内」「対応なし」の依頼は最初から品目を空にしています。")
        st.caption("同じ作業を「イレギュラー作業」ページ（次の段の汎用作業）にも入力済みなら二重になります。その場合はこちらの品目を空にしてください。")
        ledger_df = pd.DataFrame(ledger_init, columns=ledger_cols)
        for col in ("依頼No", "品目", "メモ"):
            ledger_df[col] = ledger_df[col].astype("object").where(ledger_df[col].notna(), None)
        ledger_df["数量"] = pd.to_numeric(ledger_df["数量"])
        ledger_df["単価"] = pd.to_numeric(ledger_df["単価"])
        edited = st.data_editor(
            ledger_df, hide_index=True, num_rows="dynamic", key=prefix + "ledger", width="stretch",
            column_config={
                "依頼No": st.column_config.TextColumn("依頼No", required=True, width=80),
                "品目": st.column_config.SelectboxColumn("品目", options=list(R.LEDGER_ITEMS), width=280),
                "数量": st.column_config.NumberColumn("数量", min_value=0.0, step=0.25, help="汎用作業料は人時", width=80),
                "単価": st.column_config.NumberColumn("単価", min_value=0, step=1, width=100,
                                                      help="実費のときだけ（税抜・円）。ほかは料金表の単価"),
                "メモ": st.column_config.TextColumn("メモ", width=360),
            })
        dates = {q["依頼No"]: q["日付"] for q in requests}
        ledger_now = [{**r, "日付": dates.get(str(r.get("依頼No") or ""), "")} for r in _records(edited)]
        try:
            shown_led = R.ledger_lines(ledger_now, year, month)
            if shown_led:
                st.caption(R.amount_text(shown_led))
            st.caption(f"小計：{sum(x['金額'] for x in shown_led):,}円")
        except ValueError as exc:
            st.error(str(exc))
        try:
            ev_sum, cod_sum, led_sum = _request_total()
            st.info(f"この段の合計：{ev_sum + cod_sum + led_sum:,}円（単価の決まった作業 {ev_sum:,}円・"
                    f"返品の着払い送料 {cod_sum:,}円・依頼台帳 {led_sum:,}円）")
        except ValueError:
            pass
        with st.expander("作業ごとの単価と適用条件（料金表）"):
            for name in R.EVENT_NAMES:
                row = R.ROWS[name]
                st.markdown(f"**{name}**：{row[2]}  \n{row[3]}")

    event_rows = R.sheet_event_rows(sheets_now, target_ym, new_notice) + R.choice_rows(manual_rows, ver)
    returns_now = sheets_now["返品"]

    # ---- 5. 汎用作業 ----
    labor_sum, labor_err = None, ""
    if irregular_work is not None:
        try:
            labor_sum = R.labor_lines(irregular_work, year, month)
        except ValueError as exc:
            labor_err = str(exc)
    labor_amount = sum(x["金額"] for x in labor_sum) if labor_sum else 0
    labor_hours = sum(x["数量"] for x in labor_sum) if labor_sum else 0
    summary = ("Notionを読めていません" if irregular_work is None else labor_err or
               f"{len(irregular_work)}件・{labor_hours:g}人時・{labor_amount:,}円")
    if _section(prefix + "done_labor", "5. 汎用作業（イレギュラー作業ページの記録）", summary):
        if irregular_work is not None:
            st.caption("15分単位の時間数×人数を合計し、汎用作業料（2,100円/人時）で計算します。"
                       "返品処理・FBAなど料金表に単価がある作業をここに入れると二重計上になります。")
            if labor_err:
                st.error(labor_err)
            unit = int(R.price("汎用作業料", ver=ver))
            _table([{"項目": "汎用作業料（合計）", "金額": _yen(labor_amount), "単価": _yen(unit),
                     "合計人時": _num(labor_hours), "件数": len(irregular_work)}])
            if irregular_work and not labor_err:
                detail = pd.DataFrame(irregular_work).drop(columns=["id", "対象年月"], errors="ignore")
                detail.insert(0, "金額", [_yen(R.yen(R.amount(r["時間数"]) * R.amount(r["人数"]) * unit)) for r in irregular_work])
                _table(detail, first=("作業項目", "金額", "時間数", "人数", "日付"))

    # ---- 6. 控除 ----
    keep = dict(prev.get("other", {}), cod=cod_extra)
    summary = f"応援控除 {keep.get('support', 0):,}円・保管費値引き {keep.get('discount', 0):,}円"
    if _section(prefix + "done_other", "6. 控除", summary):
        keep = {
            "cod": cod_extra,
            "support": st.number_input("応援控除（税抜・円）", min_value=0, value=keep.get("support", 0), step=1, key=prefix + "support"),
            "discount": st.number_input("保管費値引き（税抜・円）", min_value=0, value=keep.get("discount", 0), step=1, key=prefix + "discount"),
        }
        st.caption("控除は請求額から最後に差し引きます（管理費は控除前の第1層で計算）。")
    drafts[draft_key] = {"calendar": shown_rows, "events": manual_rows, "other": keep, "ledger": ledger_now,
                         "sheets": sheets_now, "new_notice": new_notice}

    # ---- 7. 試算 ----
    st.markdown("#### 7. 試算")
    calc = None
    try:
        if result is None:
            raise ValueError("出荷実績（照合結果）を読めていないため、金額を出しません")
        if counts is None or irregular_work is None:
            raise ValueError("保管カウント・イレギュラー作業を読めていないため、金額を出しません")
        if any(str(r.get("作業", "")).strip() == "入庫：ピース納品" for r in event_rows) and result.get("入庫_ピース数"):
            raise ValueError("ピース入庫はEシス入庫履歴から自動計上しています。依頼ごとの作業から外してください")
        lines = R.daily_lines(year, month, active)
        lines += R.result_lines(result, ver)
        lines += R.storage_lines(counts, year, month)
        lines += R.labor_lines(irregular_work, year, month)
        request_lines = R.event_lines(event_rows, year, month) + R.ledger_lines(ledger_now, year, month)
        lines += request_lines
        cod_auto, cod_detail = R.return_freight(returns_now)
        cod_total = cod_auto + keep.get("cod", 0)
        if cod_total:
            detail = "；".join(x for x in (cod_detail, f"ほか手入力 {keep['cod']:,}円" if keep.get("cod") else "") if x)
            lines.append(R.line("着払い送料", cod_total, 1, "実費", "式", detail))
        lines += R.extra_lines(year, month)
        calc = R.totals(R.merge_lines(lines), keep.get("support", 0), keep.get("discount", 0))
        metrics = st.columns(3)
        for col, label, field in zip(metrics, ("試算小計（税抜）", "試算消費税", "試算合計"), ("subtotal", "tax", "total")):
            col.metric(label, f"{calc[field]:,}円")
        st.caption(f"管理費の対象（第1層）：{calc['management_base']:,}円。控除前の第1層で計算しています。")
        shown = [{k: float(v) if isinstance(v, Decimal) else v for k, v in item.items()} for item in calc["items"]]
        _table([{**x, "単価": _num(x["単価"]), "数量": _num(x["数量"])} for x in shown],
               ["品名", "金額", "単価", "数量", "単位", "区分", "詳細"])
        if not counts:
            st.warning(f"{target_ym}の保管カウントが0行です。未入力でないか確認してください。")
    except ValueError as exc:
        st.error(str(exc))
    if calc is not None:
        bundle = review_bundle(calc, {"対象年月": target_ym, "出荷稼働日": active, "照合結果の読込元": source,
                                      "照合結果": result, "保管カウント": counts, "イレギュラー作業": irregular_work,
                                      "依頼ごとの作業": event_rows, "依頼台帳": ledger_now, "返品の送料": returns_now, "その他": keep}, ver)
        st.download_button("試算明細・入力根拠を保存（ZIP／MF取込不可）", bundle,
                           file_name=f"TeamEC新体系_試算_{year}{month:02}.zip", mime="application/zip",
                           key=prefix + "download")
    if calc is not None:
        _issue_section(calc, result, source, client, year, month, prefix, db_ids, notion_ready,
                       {"出荷稼働日": active, "保管カウント": counts, "イレギュラー作業": irregular_work,
                        "依頼ごとの作業": event_rows, "依頼台帳": ledger_now, "返品の送料": returns_now, "その他": keep,
                        "依頼の明細": [{k: (float(v) if isinstance(v, Decimal) else v) for k, v in x.items()}
                                       for x in request_lines]}, ver)
    with st.expander("料金の根拠"):
        st.markdown(f"[新料金単価表]({R.SOURCE['source_url']})（{R.SOURCE['checked_at']}取得）・版 {ver['id']}")
        st.write(disp["source"])
        for x in ver["extras"]:
            st.write(f"{x['品名']}：{x['source']}（対象 {'・'.join(x['months'])}）")
        st.write("請求先：" + client.get("header", {}).get("取引先名称", "Team-EC（マスタ未取得）"))


def _mf_items(calc):
    """試算の明細を MF 取込用の品目にする（単価・数量は整数か小数のまま。金額は明細の値）。"""
    out = []
    for it in calc["items"]:
        q = it["数量"]
        out.append({"品名": it["品名"], "単価": int(it["単価"]) if it["単価"] == int(it["単価"]) else float(it["単価"]),
                    "数量": int(q) if q == int(q) else float(q), "単位": it.get("単位", ""),
                    "詳細": R._plain(it.get("詳細", ""))[:200], "金額": int(it["金額"])})
    return out


def _issue_section(calc, result, source, client, year, month, prefix, db_ids, notion_ready, inputs, ver):
    """8. 請求書の発行：MF取込CSV・内訳Excelのダウンロードと、確定（発行履歴＋Driveバックアップ）。

    確定は既存の請求と同じ保存先に追記する。同じ月の発行履歴があるときは、二重請求の確認を挟む。
    保存に失敗したら「確定できていない」と表示する（成功に丸めない）。
    """
    from lib.invoice import drive_master, excel_export, invoice_number, mf_export, notion_store

    target_ym = f"{year}-{month:02}"
    st.markdown("#### 8. 請求書の発行（MF取込CSV）")
    h = client.get("header", {})
    auto_dates = invoice_number.default_dates(year, month)
    c1, c2, c3 = st.columns(3)
    inv_no = c1.text_input("請求書番号", value=invoice_number.generate_invoice_number(year, month, client.get("略号", "TE")),
                           key=prefix + "inv_no")
    issue_date = c1.text_input("請求日", value=auto_dates["請求日"], key=prefix + "issue_date")
    due_date = c2.text_input("お支払期限", value=auto_dates["お支払期限"], key=prefix + "due_date")
    sales_date = c2.text_input("売上計上日", value=auto_dates["売上計上日"], key=prefix + "sales_date")
    subject = c3.text_input("件名", value=h.get("件名", ""), key=prefix + "subject")
    staff = c3.text_input("自社担当者氏名", value=h.get("自社担当者氏名", ""), key=prefix + "staff")
    header = {"取引先名称": h.get("取引先名称", ""), "件名": subject, "請求日": issue_date, "お支払期限": due_date,
              "請求書番号": inv_no, "売上計上日": sales_date, "取引先敬称": h.get("取引先敬称", ""),
              "取引先郵便番号": h.get("取引先郵便番号", ""), "取引先都道府県": h.get("取引先都道府県", ""),
              "取引先住所1": h.get("取引先住所1", ""), "取引先住所2": h.get("取引先住所2", ""),
              "自社担当者氏名": staff, "備考": h.get("備考", ""), "振込先": h.get("振込先", "")}
    if not header["取引先名称"]:
        st.error("取引先名称がマスタから読めていません。発行できません。")
        return

    items = _mf_items(calc)
    subtotal, tax, total = mf_export.calc_totals(items)
    if (subtotal, tax, total) != (calc["subtotal"], calc["tax"], calc["total"]):
        st.error(f"MF出力の金額（{total:,}円）が画面の試算（{calc['total']:,}円）と一致しません。発行できません。")
        return

    blockers = []
    must = [i for i in result.get("要確認", []) if i.get("重さ") != "参考"]
    if must and not st.checkbox(f"照合結果の要確認 {len(must)}件を確認した", key=prefix + "ack_review"):
        blockers.append("照合結果の要確認が未確認です")
    prev = []
    if notion_ready:
        try:
            prev = [r for r in notion_store.load_issue_history(db_ids, R.CLIENT_NAME, target_ym) if r.get("区分") == "請求"]
        except Exception as exc:
            blockers.append(f"発行履歴を読めないため二重請求を確認できません（{exc}）")
    else:
        blockers.append("Notion未接続のため発行履歴を確認・保存できません")
    if prev:
        st.warning("この月は既に発行履歴があります：" + "、".join(f"{r.get('請求書番号')}（{int(r.get('合計金額') or 0):,}円）" for r in prev))
        if not st.checkbox("再発行する（二重請求にならないことを確認した）", key=prefix + "ack_reissue"):
            blockers.append("同じ月の発行履歴があります")

    enc = st.radio("文字コード（MF CSV）", ["UTF-8(BOM付き)", "Shift-JIS(cp932)"], horizontal=True, key=prefix + "enc")
    csv_bytes = mf_export.to_csv_bytes(header, items, encoding="cp932" if enc.startswith("Shift") else "utf-8-sig")
    csv_name = f"MF請求書_{R.CLIENT_NAME}_{inv_no}.csv"
    sheets = [(name, pd.DataFrame(rows), col) for name, rows, col in R.breakdown_sheets(
        calc["items"], result, inputs["出荷稼働日"], inputs["保管カウント"], inputs["イレギュラー作業"],
        inputs.get("依頼の明細") or [], inputs.get("返品の送料") or [], year, month)]
    xlsx_bytes = excel_export.build_breakdown_excel([{"費目": it["品名"], "金額": it["金額"]} for it in items], sheets)
    xlsx_name = f"内訳明細_{R.CLIENT_NAME}_{inv_no}.xlsx"
    evidence = review_bundle(calc, dict(inputs, 照合結果=result, 照合結果の読込元=source, 請求書ヘッダ=header), ver)

    d1, d2 = st.columns(2)
    d1.download_button("⬇ MF取込CSV", csv_bytes, file_name=csv_name, mime="text/csv", key=prefix + "dl_csv",
                       disabled=bool(blockers))
    d2.download_button("⬇ 内訳明細（Excel）", xlsx_bytes, file_name=xlsx_name, key=prefix + "dl_xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    for b in blockers:
        st.error("⛔ " + b)
    st.link_button("🔗 MF請求書のCSVアップロード元を開く",
                   st.secrets.get("MF_UPLOAD_URL", "https://invoice.moneyforward.com/billings"))
    if st.button("📦 請求を確定（発行履歴に保存＋Driveバックアップ）", type="primary", key=prefix + "confirm",
                 disabled=bool(blockers)):
        msgs, ok = [], True
        try:
            notion_store.save_issue_history(db_ids, invoice_no=inv_no, client_name=R.CLIENT_NAME, target_ym=target_ym,
                                            kind="請求", issue_date=issue_date, due_date=due_date,
                                            subtotal=subtotal, tax=tax, total=total, items=items)
            msgs.append(f"発行履歴を保存（{inv_no}）")
        except Exception as exc:
            ok = False
            msgs.append(f"発行履歴の保存に失敗：{exc}")
        folder = st.secrets.get("INVOICE_GDRIVE_FOLDER_ID", "")
        if folder:
            try:
                sub = drive_master.get_or_create_folder(
                    f"{inv_no}_{R.CLIENT_NAME}_{datetime.now():%Y%m%d_%H%M%S}", folder)
                drive_master.upload_bytes(csv_bytes, csv_name, sub, "text/csv")
                drive_master.upload_bytes(xlsx_bytes, xlsx_name, sub,
                                          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                drive_master.upload_bytes(evidence, f"根拠_{R.CLIENT_NAME}_{inv_no}.zip", sub, "application/zip")
                msgs.append("Driveへバックアップ（CSV・内訳・根拠ZIP）")
            except Exception as exc:
                ok = False
                msgs.append(f"Driveバックアップに失敗：{exc}")
        else:
            ok = False
            msgs.append("INVOICE_GDRIVE_FOLDER_ID 未設定のためバックアップできません")
        (st.success if ok else st.error)(("✅ 確定しました：" if ok else "⚠ 確定は完了していません：") + "／".join(msgs))
