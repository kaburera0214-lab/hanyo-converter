"""TeamEC新体系（Team-EC 2026年9月作業分〜）の画面。外部書込み・MF請求書発行は行わない。

保管カウント・イレギュラー作業は既存ページのNotion記録を読む（ここで再入力させない）。
出荷実績（Eシス）の取込と正式出力が済むまでは試算専用。
"""
from __future__ import annotations

import copy
import csv
import io
import json
import zipfile
from datetime import datetime
from decimal import Decimal

import pandas as pd
import streamlit as st

from . import rules as R


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
        z.writestr("確認事項.txt", "これは試算です。MF取込用ではありません。\n"
                   "Eシス出荷実績の取込・送料との突合・根拠保全が済んでから正式請求へ進んでください。\n")
    return buf.getvalue()


def _load(label, loader):
    """Notionの記録を読む。読めなければNone（＝不明）。0件とは区別する。"""
    try:
        return loader()
    except Exception as exc:
        st.error(f"{label}を読めませんでした（0件扱いにはしません）: {exc}")
        return None


def render(year, month, client, db_ids=None, notion_ready=False):
    from lib.invoice import notion_store

    year, month = int(year), int(month)
    ver = R.version(year, month)
    disp = R.dispatch(ver)
    target_ym = f"{year}-{month:02}"
    drafts = st.session_state.setdefault("_teamec_drafts", {})
    if st.session_state.get("_teamec_active") != target_ym:
        st.session_state["_teamec_active"] = target_ym
        st.session_state["_teamec_generation"] = st.session_state.get("_teamec_generation", 0) + 1
        st.session_state["_teamec_form_base"] = copy.deepcopy(drafts.get(target_ym, {}))
    base = st.session_state["_teamec_form_base"]
    prefix = f"teamec_new_{year}_{month}_{st.session_state['_teamec_generation']}_"

    st.subheader(f"Team-EC 新体系（{ver['id']}）｜試算")
    st.caption("パピー用 · 2026年9月作業分から自動で新体系になります。8月以前は従来の計算です。")
    st.info("Eシス出荷実績の取込と正式出力が未接続のため、現在は試算です。MF取込CSVは発行しません。")

    if notion_ready:
        counts = _load("保管カウント", lambda: notion_store.load_storage_counts(db_ids, R.CLIENT_NAME, target_ym))
        irregular = _load("イレギュラー作業", lambda: notion_store.load_irregular_work(db_ids, R.CLIENT_NAME, target_ym))
    else:
        st.error("Notion未接続のため、保管カウント・イレギュラー作業を読めません（0件扱いにはしません）。")
        counts = irregular = None

    tabs = st.tabs(["① 稼働日", "② 保管・汎用作業", "③ 依頼ごとの作業", "④ 出荷・実費・控除", "⑤ 試算"])
    with tabs[0]:
        st.caption(f"出荷指示作成料は1回{disp['price']:,}円。出荷した稼働日のみ、平日{disp['weekday']}回・"
                   f"土日祝{disp['holiday']}回で固定します（出荷件数では増減しません）。")
        st.warning("いまは平日（祝日除く）を仮に選んでいます。出荷の無かった日を外し、出荷した土日祝を追加してください。"
                   "Eシス出荷実績の取込後は、出荷のあった日から自動で判定します。")
        cal = st.data_editor(pd.DataFrame(base.get("calendar", R.calendar_rows(year, month))), hide_index=True,
                             disabled=["日付", "曜日", "祝日", "定額回数"],
                             column_config={"稼働": st.column_config.CheckboxColumn("出荷稼働")},
                             key=prefix + "calendar", width="stretch")
        active = [r["日付"] for r in _records(cal) if r["稼働"]]
        st.caption(f"選択中：{len(active)}日。日次締め処理 {R.price('日次締め処理', ver=ver):,.0f}円/日、"
                   f"日次運用費 {R.price('日次運用費', ver=ver):,.0f}円/日。")
    with tabs[1]:
        st.markdown("**保管（保管カウントページの記録）**")
        if counts is not None:
            st.caption(f"{target_ym}：{len(counts)}行。入力・修正は左メニュー「保管カウント」で行います。")
            if counts:
                st.dataframe(pd.DataFrame(counts).drop(columns=["id"], errors="ignore"),
                             hide_index=True, width="stretch")
            names = "・".join(ver["storage"])
            st.caption(f"新体系で使える種別：{names}。600円パレットは「保管料：当社指定ロケーション」で記録します。")
        st.markdown("**汎用作業（イレギュラー作業ページの記録）**")
        if irregular is not None:
            st.caption(f"{target_ym}：{len(irregular)}件。15分単位の時間数×人数をそのまま合計します。"
                       "返品処理・FBAなど料金表に単価がある作業をここに入れると二重計上になります。")
            if irregular:
                st.dataframe(pd.DataFrame(irregular).drop(columns=["id", "対象年月"], errors="ignore"),
                             hide_index=True, width="stretch")
    with tabs[2]:
        st.caption("依頼IDごとに記録します（FBAはEシスの受注番号 FBA00xx）。数量は選んだ作業の単位です。")
        st.warning("依頼台帳・Eシスからの自動取込は未接続です。この入力欄は検証用です。")
        columns = ["実施日", "依頼ID", "作業", "数量", "通知日", "時間外再手配", "根拠・備考"]
        event_base = (pd.DataFrame(base["events"], columns=columns) if base.get("events") else
                      pd.DataFrame({c: pd.Series(dtype="bool" if c == "時間外再手配" else
                                                 "float64" if c == "数量" else "str") for c in columns}))
        event_base["数量"] = pd.to_numeric(event_base["数量"].replace("", None))
        event_base["時間外再手配"] = event_base["時間外再手配"].map(lambda v: v is True or v == 1).astype(bool)
        events = st.data_editor(
            event_base, num_rows="dynamic", hide_index=True, key=prefix + "events", width="stretch",
            column_config={
                "実施日": st.column_config.TextColumn("実施日", help="YYYY-MM-DD"),
                "作業": st.column_config.SelectboxColumn("作業", options=R.EVENT_NAMES, required=True, width="large"),
                "数量": st.column_config.NumberColumn("数量", min_value=0, step=1, default=1),
                "時間外再手配": st.column_config.CheckboxColumn("時間外再手配", default=False),
                "通知日": st.column_config.TextColumn("通知日", help="新商品・追加便の事前通知。YYYY-MM-DD"),
            })
        with st.expander("作業ごとの単価と適用条件（料金表）"):
            for name in R.EVENT_NAMES:
                row = R.ROWS[name]
                st.markdown(f"**{name}**：{row[2]}  \n{row[3]}")
    with tabs[3]:
        st.caption("Eシス出荷実績の取込までの検証用に、集計済みの税抜合計を入力します。")
        direct = {}
        for name in ("出荷作業料", "送料", "資材費", "着払い送料"):
            direct[name] = st.number_input(f"{name}（税抜合計・円）", min_value=0,
                                           value=base.get("direct", {}).get(name, 0), step=1, key=prefix + name)
        st.caption("出荷作業料は管理費5%の対象。送料・資材費・着払い送料は対象外です。")
        st.markdown("**控除（請求額から最後に差し引きます。管理費は変わりません）**")
        support = st.number_input("応援控除（税抜・円）", min_value=0, value=base.get("support", 0), step=1,
                                  key=prefix + "support")
        support_reason = st.text_input("応援控除の根拠", value=base.get("support_reason", ""),
                                       key=prefix + "support_reason", placeholder="対象日・担当者・作業など")
        discount = st.number_input("保管費値引き（税抜・円）", min_value=0, value=base.get("discount", 0), step=1,
                                   key=prefix + "discount")
        discount_reason = st.text_input("保管費値引きの根拠", value=base.get("discount_reason", ""),
                                        key=prefix + "discount_reason", placeholder="半分未満のパレットなど")
    drafts[target_ym] = {"calendar": _records(cal), "events": _records(events), "direct": direct,
                         "support": support, "support_reason": support_reason,
                         "discount": discount, "discount_reason": discount_reason}
    with tabs[4]:
        result = None
        try:
            if counts is None or irregular is None:
                raise ValueError("保管カウント・イレギュラー作業を読めていないため、金額を出しません")
            lines = R.daily_lines(year, month, active)
            lines += R.storage_lines(counts, year, month)
            lines += R.labor_lines(irregular, year, month)
            lines += R.event_lines(_records(events), year, month)
            lines += [R.line(name, value, 1, "第1層" if name == "出荷作業料" else "実費", "式",
                             "集計済合計を手入力（CSV突合未検証）") for name, value in direct.items() if value]
            lines += R.extra_lines(year, month)
            result = R.totals(lines, support, discount)
            for item in result["items"]:
                if item["品名"] == "応援控除":
                    item["詳細"] = support_reason or "根拠未入力"
                elif item["品名"] == "保管費値引き":
                    item["詳細"] = discount_reason or "根拠未入力"
            metrics = st.columns(3)
            for col, label, field in zip(metrics, ("試算小計（税抜）", "試算消費税", "試算合計"),
                                         ("subtotal", "tax", "total")):
                col.metric(label, f"{result[field]:,}円")
            st.caption(f"管理費の対象（第1層）：{result['management_base']:,}円。控除前の第1層で計算しています。")
            shown = [{k: float(v) if isinstance(v, Decimal) else v for k, v in item.items()} for item in result["items"]]
            st.dataframe(pd.DataFrame(shown), hide_index=True, width="stretch")
            if not counts:
                st.warning(f"{target_ym}の保管カウントが0行です。未入力でないか確認してください。")
        except ValueError as exc:
            st.error(str(exc))
        if result is not None:
            bundle = review_bundle(result, {"対象年月": target_ym, "出荷稼働日": active, "保管カウント": counts,
                                            "イレギュラー作業": irregular, "依頼ごとの作業": _records(events),
                                            "手入力合計": direct, "応援控除": support, "応援根拠": support_reason,
                                            "保管費値引き": discount, "保管値引き根拠": discount_reason,
                                            "実績CSV検証済": False}, ver)
            st.download_button("試算明細・入力根拠を保存（ZIP／MF取込不可）", bundle,
                               file_name=f"TeamEC新体系_試算_{year}{month:02}.zip", mime="application/zip",
                               key=prefix + "download")
        with st.expander("料金の根拠"):
            st.markdown(f"[新料金単価表]({R.SOURCE['source_url']})（{R.SOURCE['checked_at']}取得）・版 {ver['id']}")
            st.write(disp["source"])
            for x in ver["extras"]:
                st.write(f"{x['品名']}：{x['source']}（対象 {'・'.join(x['months'])}）")
            st.write("請求先：" + client.get("header", {}).get("取引先名称", "Team-EC（マスタ未取得）"))
