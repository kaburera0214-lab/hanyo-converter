"""新体系の確認用UI。外部書込み・MF請求書発行は行わない。

実績CSVの対応と未確定ポリシーの承認が済むまで試算専用。
本番のクライアント・価格マスタを追加・変更せず、別sessionキーを使う。
"""
from __future__ import annotations

import csv
import copy
import io
import json
import zipfile
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal

import pandas as pd
import streamlit as st

from . import rules as R


def _records(frame):
    return frame.astype(object).where(pd.notna(frame), "").to_dict("records")


def review_bundle(result, inputs):
    """自社で保持できる試算スナップショット。正式なMF CSVは含めない。"""
    def serialize(value):
        if isinstance(value, Decimal):
            return str(value)
        raise TypeError(type(value).__name__)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        payload = {"status": "試算・請求不可", "created_at": datetime.now().astimezone().isoformat(),
                   "inputs": inputs, "result": result, "source": R.SOURCE,
                   "dispatch_override": R.DISPATCH}
        z.writestr("試算根拠.json", json.dumps(payload, ensure_ascii=False, indent=2, default=serialize))
        table = io.StringIO()
        columns = ["区分", "品名", "単価", "数量", "単位", "金額", "詳細"]
        writer = csv.DictWriter(table, fieldnames=columns, lineterminator="\r\n")
        writer.writeheader()
        writer.writerows(result["items"])
        z.writestr("試算明細_MF取込不可.csv", table.getvalue().encode("utf-8-sig"))
        z.writestr("確認事項.txt", "これは試算です。MF取込用ではありません。\n"
                   "Eシス実績CSV、送料との突合、未確定条件、根拠保全を確認してから正式請求へ進んでください。\n"
                   "既存Team-ECと新体系で同じ月を二重に請求しないでください。\n")
    return buf.getvalue()


def render(year, month, client):
    year, month = int(year), int(month)
    month_key = f"{year}-{month:02}"
    drafts = st.session_state.setdefault("_teamec_drafts", {})
    if st.session_state.get("_teamec_active") != month_key:
        st.session_state["_teamec_active"] = month_key
        st.session_state["_teamec_generation"] = st.session_state.get("_teamec_generation", 0) + 1
        st.session_state["_teamec_form_base"] = copy.deepcopy(drafts.get(month_key, {}))
    base = st.session_state["_teamec_form_base"]
    prefix = f"teamec_new_{year}_{month}_{st.session_state['_teamec_generation']}_"
    st.subheader("TeamEC新体系｜試算・画面確認")
    st.caption("パピー用 · 2026年9月作業分から · 請求先は既存のTeam-ECと同じです。")
    st.info("現在は試算用です。実績CSVの連携と確認待ちの計算条件が残っているため、正式なMF取込CSVは発行しません。入力内容はこの画面のセッション内だけに保持されます。")
    if (year, month) < R.APPLIES_FROM:
        st.warning("2026年8月以前は、クライアントで既存の「Team-EC」を選んでください。")
        return
    tabs = st.tabs(["① 稼働日・保管", "② 入庫・個別作業", "③ 出荷・実費・控除", "④ 試算・確認事項"])
    with tabs[0]:
        st.markdown("**稼働日**")
        st.caption(f"出荷指示作成料は1回{R.DISPATCH['price']:,}円。稼働日は平日{R.DISPATCH['weekday']}回・土日祝{R.DISPATCH['holiday']}回で固定し、実際の出荷件数・CSV数では増減しません。")
        st.warning("稼働日は、まず平日（祝日除く）だけを仮入力しています。土日祝の稼働・臨時休業を確認してください。課金対象日の確定は確認待ちです。")
        cal = st.data_editor(pd.DataFrame(base.get("calendar", R.calendar_rows(year, month))), hide_index=True,
                             disabled=["日付", "曜日", "祝日", "定額回数"],
                             column_config={"稼働": st.column_config.CheckboxColumn("稼働")},
                             key=prefix + "calendar", width="stretch")
        active = [r["日付"] for r in _records(cal) if r["稼働"]]
        st.caption(f"現在の選択：{len(active)}稼働日。日次締め処理 {R.price('日次締め処理'):,.0f}円/日、日次運用費 {R.price('日次運用費'):,.0f}円/日。")
        st.markdown("**保管費｜15日・月末の平均**")
        storage = st.data_editor(pd.DataFrame(base.get("storage", [
            {"種別": name, "月額単価": int(R.price(name)), "15日": 0.0, "月末": 0.0}
            for name in R.STORAGE_NAMES])), hide_index=True,
            disabled=["種別", "月額単価"], key=prefix + "storage", width="stretch",
            column_config={c: st.column_config.NumberColumn(c, min_value=0.0, step=0.5)
                           for c in ("15日", "月末")})
        st.caption("600円パレットは「当社指定ロケーション」に入力します。同じ保管場所を通常パレットと重複計上しないでください。空き枠の予約機能は設けません。")
        st.caption(R.ROWS[R.STORAGE_NAMES[2]][3])
    with tabs[1]:
        st.markdown("**依頼単位の実績**")
        st.caption("実施日・依頼IDを付けて記録します。数量は箱数・点数・台数・SKU数など、選んだ作業の単位です。")
        st.warning("Eシス・依頼台帳からの自動取り込みは実データ確認待ちです。この入力欄は画面と計算の検証用です。")
        columns = ["実施日", "依頼ID", "作業", "数量", "通知日", "分/人", "人数", "時間外再手配", "根拠・備考"]
        event_base = (pd.DataFrame(base["events"], columns=columns) if base.get("events") else
                      pd.DataFrame({c: pd.Series(dtype="bool" if c == "時間外再手配" else
                            "float64" if c in ("数量", "分/人", "人数") else "str") for c in columns}))
        for numeric_column in ("数量", "分/人", "人数"):
            event_base[numeric_column] = pd.to_numeric(event_base[numeric_column].replace("", None))
        event_base["時間外再手配"] = event_base["時間外再手配"].map(lambda v: v is True or v == 1).astype(bool)
        events = st.data_editor(event_base,
            num_rows="dynamic", hide_index=True, key=prefix + "events", width="stretch",
            column_config={
                "実施日": st.column_config.TextColumn("実施日", help="YYYY-MM-DD"),
                "作業": st.column_config.SelectboxColumn("作業", options=R.EVENT_NAMES, required=True, width="large"),
                "数量": st.column_config.NumberColumn("数量", min_value=0, step=1, default=1),
                "人数": st.column_config.NumberColumn("人数", min_value=1, step=1, default=1),
                "分/人": st.column_config.NumberColumn("分/人", min_value=0, step=1, default=0),
                "時間外再手配": st.column_config.CheckboxColumn("時間外再手配", default=False),
                "通知日": st.column_config.TextColumn("通知日", help="新商品・追加便の事前通知。YYYY-MM-DD"),
            })
        st.caption("汎用作業は数量1・作業分数・人数を入力。FBAは1依頼につき数量1。追加便は通常便に含めず、3便超は事前協議です。")
        with st.expander("作業ごとの単価と適用条件"):
            for name in R.EVENT_NAMES:
                row = R.ROWS[name]
                st.markdown(f"**{name}**：{row[2]}  \n{row[3]}")
                st.caption(row[4] + ("／" + row[7] if len(row) > 7 else ""))
    with tabs[2]:
        st.markdown("**出荷作業料・実費**")
        st.caption("現時点は集計済みの税抜合計で試算します。Eシスの実CSVを確認後、出荷・入庫・送料を自動集計する欄に置き換えます。")
        direct = {}
        for name in ("出荷作業料", "送料", "資材費", "着払い送料"):
            direct[name] = st.number_input(f"{name}（税抜合計・円）", min_value=0, value=base.get("direct", {}).get(name, 0),
                                           step=1, key=prefix + name)
        st.caption("出荷作業料のみ管理費5%の対象。送料・資材費・着払い送料は対象外です。未取得の実績は0円確定とは扱いません。")
        st.markdown("**控除**")
        support = st.number_input("応援控除（税抜・円）", min_value=0, value=base.get("support", 0), step=1, key=prefix + "support")
        support_reason = st.text_input("応援控除の根拠", value=base.get("support_reason", ""), key=prefix + "support_reason", placeholder="対象日・担当者・作業など")
        discount = st.number_input("保管費値引き（税抜・円）", min_value=0, value=base.get("discount", 0), step=1, key=prefix + "discount")
        discount_reason = st.text_input("保管費値引きの根拠", value=base.get("discount_reason", ""), key=prefix + "discount_reason", placeholder="半分未満のパレットなど。割引率・計算方法は別途確認")
        st.caption("応援控除・保管費値引きは別の明細になります。旧事務手数料300円は新体系に加算しません。")
    with tabs[3]:
        st.markdown("**確認待ちの計算条件（切り替えて差額を確認できます）**")
        labor = st.radio("汎用作業の15分切り上げ", ["依頼ごとの合計人分", "各人ごと"], index=base.get("labor_index", 0), key=prefix + "round_labor", horizontal=True)
        st.caption("例：2人が各20分作業 → 合計人分なら45分＝1,575円、各人なら30分×2人＝2,100円。確定前の試算条件です。")
        management = st.radio("管理費の1円未満", ["四捨五入", "切り捨て"], index=base.get("management_index", 0), key=prefix + "round_management", horizontal=True)
        reduces = st.radio("応援控除と管理費", ["第1層から控除して5%を計算", "管理費は減らさず請求総額から控除"], index=base.get("support_index", 0), key=prefix + "support_base")
        policy = R.Policy(labor, management, reduces.startswith("第1層"))
        drafts[month_key] = {"calendar": _records(cal), "storage": _records(storage), "events": _records(events),
                             "direct": direct, "support": support, "support_reason": support_reason,
                             "discount": discount, "discount_reason": discount_reason,
                             "labor_index": int(labor == "各人ごと"), "management_index": int(management == "切り捨て"),
                             "support_index": int(not policy.support_reduces_base)}
        st.caption("明細金額と消費税は既存画面と同じ四捨五入で試算しています。正式出力までに端数処理も確定します。")
        result = None
        try:
            lines = R.daily_lines(year, month, active)
            lines += R.storage_lines(_records(storage))
            lines += R.event_lines(_records(events), policy, year, month)
            lines += [R.line(name, value, 1, "第1層" if name == "出荷作業料" else "実費", "式",
                             "集計済合計を手入力（CSV突合未検証）") for name, value in direct.items() if value]
            result = R.totals(lines, support, discount, policy)
            for item in result["items"]:
                if item["品名"] == "応援控除":
                    item["詳細"] = support_reason or "根拠未入力"
                elif item["品名"] == "保管費値引き":
                    item["詳細"] = discount_reason or "根拠未入力"
            metrics = st.columns(3)
            for col, label, field in zip(metrics, ("試算小計（税抜）", "試算消費税", "試算合計"), ("subtotal", "tax", "total")):
                col.metric(label, f"{result[field]:,}円")
            st.caption(f"管理費の対象額：{result['management_base']:,}円。保管・実費・第2層・第3層は含みません。")
            shown = [{k: float(v) if isinstance(v, Decimal) else v for k, v in item.items()} for item in result["items"]]
            st.dataframe(pd.DataFrame(shown), hide_index=True, width="stretch")
        except ValueError as exc:
            st.error(str(exc))
        st.markdown("**正式請求までの確認事項**")
        for text in (
            "Eシス実績CSVの列・出荷ID・明細単位を確認し、B2／送料と突合する（NE用CSVの流用はしない）。",
            "稼働日・出荷作業料・実費・保管数量・個別作業の月次実績を取得する。未取得を0件としない。",
            "丸め方・応援控除の管理費への反映・FBA依頼の識別方法を確定する。",
            "9月までの協力費20,000円を残すか確認する（現行料金表にないため、この試算には含めていません）。",
            "既存Team-ECとの二重請求を防ぐ履歴確認と、元CSV・適用単価のDrive保存を接続する。",
        ):
            st.write("・" + text)
        if result is not None:
            bundle = review_bundle(result, {"対象年": year, "対象月": month, "稼働日": active,
                         "保管": _records(storage), "作業": _records(events), "手入力合計": direct,
                         "応援控除": support, "応援根拠": support_reason,
                         "保管費値引き": discount, "保管値引き根拠": discount_reason,
                         "試算条件": asdict(policy), "実績CSV検証済": False})
            st.download_button("試算明細・入力根拠を保存（ZIP／MF取込不可）", bundle,
                               file_name=f"TeamEC新体系_試算_{year}{month:02}.zip", mime="application/zip",
                               key=prefix + "download")
        with st.expander("料金の根拠"):
            st.markdown(f"[新料金単価表]({R.SOURCE['source_url']})（{R.SOURCE['checked_at']}確認）")
            st.write(R.DISPATCH["source"])
            st.caption("参照料金表の40円/件より、本人回答の1,200円/回を優先。ピース入庫は参照料金表の40円です。")
            st.write("請求先：" + client.get("header", {}).get("取引先名称", "Team-EC（マスタ未取得）"))
