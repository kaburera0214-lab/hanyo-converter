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
            st.dataframe(pd.DataFrame(must), hide_index=True, width="stretch")
        with st.expander("内訳（商品サイズ別PCS・資材・学習した項目1）"):
            st.write("出荷作業料（商品サイズ別PCS）", result["出荷作業料_サイズ別"])
            st.dataframe(pd.DataFrame(result["資材費_サイズ別"]), hide_index=True)
            irregular = result.get("棚番と項目1が違う商品", [])
            if irregular:
                st.caption("棚番と発送サイズ（項目1）が違う商品。1注文1個の出荷実績から学習した値で出荷作業料を計算しています。")
                st.dataframe(pd.DataFrame(irregular), hide_index=True)

    # 入力の下書きは「対象月×照合結果」ごと。照合結果を後から読み込んだら、稼働日などの初期値を作り直す
    draft_key = f"{target_ym}|{(result or {}).get('作成日時', 'none')}"
    drafts = st.session_state.setdefault("_teamec_drafts", {})
    if st.session_state.get("_teamec_active") != draft_key:
        st.session_state["_teamec_active"] = draft_key
        st.session_state["_teamec_generation"] = st.session_state.get("_teamec_generation", 0) + 1
        if drafts.get(draft_key):
            carried = drafts[draft_key]
        else:  # 初めて開く照合結果：シート由来の行を入れ直し、手で入れた行だけ引き継ぐ
            carried = {k: v for k, v in drafts.get(f"{target_ym}|none", {}).items() if k not in ("calendar", "ledger", "returns")}
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
                st.dataframe(pd.DataFrame(storage_sum), hide_index=True, width=640)
                st.caption(f"保管料 合計：{sum(r['金額'] for r in storage_sum):,}円")
            else:
                st.warning(f"{target_ym} の保管カウントが0行です。未入力でないか確認してください。")
            if counts:
                with st.expander(f"明細（{len(counts)}行）"):
                    st.dataframe(pd.DataFrame(counts).drop(columns=["id"], errors="ignore"), hide_index=True, width="stretch")

    # ---- 4. 依頼ごとの作業（単価の決まった作業 → 依頼台帳）----
    choices = R.event_choices(ver)
    fba_price = int(R.price("FBA対応費", ver=ver))
    fba_rows = [{"実施日": f["発送日"], "依頼ID": f["注文番号"], "作業": "FBA対応費", "数量": 1, "通知日": "",
                 "時間外再手配": False, "根拠・備考": f"Eシス注文ID {f['注文ID']}（自動）"}
                for f in (result or {}).get("FBA依頼", [])]
    ledger_missing = R.ledger_state(result)
    columns = ["実施日", "依頼ID", "作業", "数量", "根拠・備考"]
    initial_events = [r for r in base.get("events", []) if "（自動）" not in str(r.get("根拠・備考", ""))]
    if base.get("_fresh"):
        initial_events = R.auto_event_rows(result) + [r for r in initial_events
                                                      if not str(r.get("根拠・備考", "")).startswith(R.AUTO_MARKS)]
    initial_events = [{**{c: r.get(c) for c in columns}, "作業": R.choice_label(r, ver)} for r in initial_events]
    new_items = (result or {}).get("新商品") or {}
    manual_rows = prev.get("events", initial_events)
    ledger_cols = ["依頼No", "日付", "品目", "数量", "単価", "メモ"]
    requests = R.ledger_requests(result)
    ledger_init = base.get("ledger") if base.get("ledger") is not None else R.ledger_rows(result)
    ledger_now = prev.get("ledger", ledger_init)
    returns_init = base.get("returns") if base.get("returns") is not None else R.return_freight_rows(result)
    returns_now = prev.get("returns", returns_init)
    cod_extra = prev.get("other", {}).get("cod", 0)

    def _request_total():
        """この段の金額（単価の決まった作業＋着払い送料＋依頼台帳）。入力が足りなければ ValueError。"""
        ev = R.event_lines(fba_rows + R.choice_rows(manual_rows, ver), year, month)
        led = R.ledger_lines(ledger_now, year, month)
        cod = R.return_freight(returns_now)[0] + cod_extra
        return sum(x["金額"] for x in ev), cod, sum(x["金額"] for x in led)

    try:
        ev_sum, cod_sum, led_sum = _request_total()
        summary = (f"単価の決まった作業 {ev_sum:,}円・返品の着払い送料 {cod_sum:,}円・依頼台帳 {led_sum:,}円"
                   f"（計 {ev_sum + cod_sum + led_sum:,}円）")
    except ValueError as exc:
        summary = f"未確定：{exc}"
    if _section(prefix + "done_events", "4. 依頼ごとの作業（依頼された内容と金額）", summary):
        st.markdown("##### ① 単価が決まっている作業")
        st.markdown(f"**FBA（受注番号 FBA00xx）：{len(fba_rows)}件**（照合結果から自動）")
        if fba_rows:
            fba_df = pd.DataFrame(fba_rows)[["実施日", "依頼ID", "根拠・備考"]]
            fba_df["単価"], fba_df["金額"] = fba_price, fba_price
            st.dataframe(fba_df, hide_index=True, width=640)
            st.caption(f"FBA対応費 小計：{fba_price * len(fba_rows):,}円")
        elif result is None:
            st.caption("照合結果を読み込むと表示されます。")
        st.markdown("**随時連絡・返品・配送変更・新商品（自動で入れた行）＋ 手で足す作業**（車両受入・追加便など）")
        if ledger_missing:
            st.warning(f"随時連絡・返品・配送変更は自動で入っていません（{ledger_missing}）。0件とは限りません。シートを見て入力してください。")
        else:
            led = result["台帳"]
            st.caption(f"共有シート（取得 {led['取得日時']}）から：随時連絡 {len(led['随時連絡'])}件・返品 {len(led['返品'])}件・"
                       f"配送変更（伝票番号がEシスの注文ID） {len(led['配送変更'])}件。根拠が【シート】で始まる行です。違っていれば直す・消すができます。 "
                       + " ／ ".join(f"[{k}]({v})" for k, v in led["出典"].items() if k != "依頼台帳"))
        if new_items.get("状態") == "取得済":
            st.caption(f"新商品 初期設定：棚番が未設定→設定になった商品 {new_items['件数']}件（{new_items['説明']}）。")
            if new_items["明細"]:
                with st.expander("新商品（棚番が入った商品）"):
                    st.dataframe(pd.DataFrame(new_items["明細"]), hide_index=True, width="stretch")
        else:
            st.warning("新商品 初期設定は自動で数えられていません（" + new_items.get("説明", "この照合結果は未対応") + "）。")
        st.caption("作業は単価つきで選びます。事前通知（14日前まで）や時間帯外の到着で単価が変わる作業は、選択肢を分けています。"
                   "ピース入庫はEシス入庫履歴から自動計上するため、ここには入れないでください。")
        events_df = pd.DataFrame(initial_events, columns=columns) if initial_events else pd.DataFrame(
            {c: pd.Series(dtype="float64" if c == "数量" else "str") for c in columns})
        events_df["数量"] = pd.to_numeric(events_df["数量"].replace("", None))
        events = st.data_editor(
            events_df, num_rows="dynamic", hide_index=True, key=prefix + "events", width="stretch",
            column_config={
                "実施日": st.column_config.TextColumn("実施日", help="YYYY-MM-DD"),
                "依頼ID": st.column_config.TextColumn("依頼ID", help="注文ID・送り状番号・受注番号など、依頼を特定できるもの"),
                "作業": st.column_config.SelectboxColumn("作業（単価）", options=list(choices), required=True, width="large"),
                "数量": st.column_config.NumberColumn("数量", min_value=0, step=1, default=1),
            })
        manual_rows = _records(events)
        try:
            shown_ev = R.event_lines(R.choice_rows(manual_rows, ver), year, month)
            if shown_ev:
                st.dataframe(pd.DataFrame(R.amount_rows(shown_ev)), hide_index=True, width=760)
            st.caption(f"上の表の金額 小計：{sum(x['金額'] for x in shown_ev):,}円（FBAを除く）")
        except ValueError as exc:
            st.error(str(exc))

        st.markdown("**返品の着払い送料**（返品交換シート。返品処理料は上の表、送料はここ）")
        if ledger_missing:
            st.warning(f"返品の着払い送料は自動で入っていません（{ledger_missing}）。")
        elif returns_init:
            st.caption("送り状番号がヤマトの請求にあればその金額（税別）で確定。無いものは、追跡で運送会社を確かめ、"
                       "シートの「パピー記入欄」を仮の金額として入れています。**状態が「仮」の行は、その運送会社"
                       "（佐川・日本郵便）の請求を見て「計上する送料」を直してください。**")
            ret_df = pd.DataFrame(returns_init)
            ret_df["請求で確認した金額"] = pd.to_numeric(ret_df["請求で確認した金額"])
            ret_df["計上する送料"] = pd.to_numeric(ret_df["計上する送料"])
            edited_ret = st.data_editor(
                ret_df, hide_index=True, key=prefix + "returns", width="stretch",
                disabled=[c for c in ret_df.columns if c != "計上する送料"],
                column_config={"計上する送料": st.column_config.NumberColumn("計上する送料（税抜）", min_value=0, step=1)})
            returns_now = _records(edited_ret)
        else:
            st.caption(f"{target_ym} の返品は返品交換シートにありません。")
        cod_extra = st.number_input("上のほかの着払い送料（税抜合計・円）", min_value=0, value=cod_extra, step=1, key=prefix + "cod")

        st.markdown("##### ② 依頼台帳（内容を見て判別する依頼）")
        if ledger_missing:
            st.warning(f"依頼台帳を読めていません（{ledger_missing}）。0件とは限りません。台帳を見て下の表に行を足してください。")
        else:
            st.markdown(f"**{target_ym} の依頼：{len(requests)}件**（[依頼台帳]({result['台帳']['出典']['依頼台帳']})で、"
                        "出荷日（無ければ依頼日）がこの月のもの）")
            if requests:
                st.dataframe(pd.DataFrame(requests), hide_index=True, width="stretch",
                             column_config={"内容": st.column_config.TextColumn("内容", width="large")})
        st.markdown("**請求のたたき台**（依頼の文面から品目の候補を出しています。確認して、行の追加・削除・変更をしてください）")
        st.caption("1つの依頼に複数の品目があれば行を足します。汎用作業料は人時（0.25刻み・単価2,100円固定）、"
                   "実費（チャーター・資材など）は数量と単価（税抜）を入れます。請求しない候補は行ごと削除してください。"
                   "台帳の作業費が「通常内」・対応状況が「対応なし」の依頼は候補に出していません。")
        st.caption("同じ作業を「イレギュラー作業」ページ（次の段の汎用作業）にも入力済みなら二重になります。その場合はこちらの行を削除してください。")
        ledger_df = pd.DataFrame(ledger_init, columns=ledger_cols)
        for col in ("依頼No", "日付", "品目", "メモ"):
            ledger_df[col] = ledger_df[col].astype("object").where(ledger_df[col].notna(), None)
        ledger_df["数量"] = pd.to_numeric(ledger_df["数量"])
        ledger_df["単価"] = pd.to_numeric(ledger_df["単価"])
        edited = st.data_editor(
            ledger_df, hide_index=True, num_rows="dynamic", key=prefix + "ledger", width="stretch",
            column_config={
                "依頼No": st.column_config.TextColumn("依頼No", required=True),
                "日付": st.column_config.TextColumn("日付"),
                "品目": st.column_config.SelectboxColumn("品目", options=list(R.LEDGER_ITEMS), required=True, width="medium"),
                "数量": st.column_config.NumberColumn("数量", min_value=0.0, step=0.25, help="汎用作業料は人時"),
                "単価": st.column_config.NumberColumn("単価", min_value=0, step=1, help="実費のときだけ（税抜・円）。ほかは料金表の単価"),
                "メモ": st.column_config.TextColumn("メモ", width="large"),
            })
        ledger_now = _records(edited)
        try:
            shown_led = R.ledger_lines(ledger_now, year, month)
            if shown_led:
                st.dataframe(pd.DataFrame(R.amount_rows(shown_led)), hide_index=True, width=760)
            st.caption(f"依頼台帳の金額 小計：{sum(x['金額'] for x in shown_led):,}円")
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

    event_rows = fba_rows + R.choice_rows(manual_rows, ver)

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
            st.dataframe(pd.DataFrame([{"件数": len(irregular_work), "合計人時": float(labor_hours),
                                        "単価": int(R.price("汎用作業料", ver=ver)), "金額": labor_amount}]),
                         hide_index=True, width=420)
            if irregular_work:
                st.dataframe(pd.DataFrame(irregular_work).drop(columns=["id", "対象年月"], errors="ignore"),
                             hide_index=True, width="stretch")

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
                         "returns": returns_now}

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
        lines += R.event_lines(event_rows, year, month)
        lines += R.ledger_lines(ledger_now, year, month)
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
        st.dataframe(pd.DataFrame(shown), hide_index=True, width="stretch")
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
                        "依頼ごとの作業": event_rows, "依頼台帳": ledger_now, "返品の送料": returns_now, "その他": keep}, ver)
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
                    "詳細": str(it.get("詳細", ""))[:200], "金額": int(it["金額"])})
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
    pick = pd.DataFrame(result.get("出荷作業料_明細", []))
    sheets = [("出荷作業費", pick if len(pick) else None, "金額" if len(pick) else None),
              ("資材費", pd.DataFrame(result["資材費_サイズ別"]), "金額"),
              ("出荷稼働日", pd.DataFrame({"日付": inputs["出荷稼働日"]}), None),
              ("保管費", pd.DataFrame(inputs["保管カウント"] or []), None),
              ("汎用作業費", pd.DataFrame(inputs["イレギュラー作業"] or []).drop(columns=["id"], errors="ignore"), None),
              ("依頼ごとの作業", pd.DataFrame(inputs["依頼ごとの作業"]), None),
              ("依頼台帳", pd.DataFrame(inputs.get("依頼台帳") or []), None),
              ("返品の送料", pd.DataFrame(inputs.get("返品の送料") or []), None),
              ("入庫", pd.DataFrame(result.get("入庫", [])), None)]
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
