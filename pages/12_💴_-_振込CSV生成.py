# -*- coding: utf-8 -*-
"""
振込CSV生成（買掛）

ステータスが「確認済」の請求書のみを抽出し、取引先マスタの口座情報を使って
楽天銀行 総合振込インポートCSV(Shift-JIS)を生成・自動ダウンロードする。
口座未登録・除外フラグの取引先は対象外。生成内容は振込履歴に保存。
"""
import streamlit as st
import pandas as pd

st.set_page_config(page_title="振込CSV生成", layout="wide")

from lib.auth import require_role
require_role("payable")  # 認証ゲート（AUTH_ENABLED=false なら素通り）
st.title("💴 振込CSV生成（楽天銀行 総合振込）")
st.caption("『確認済』の請求書から楽天銀行インポート用CSVを作成します。")

from lib.payable import app_init, matching, rakuten_csv, mf_csv, notion_payable as N

try:
    db_ids = app_init.init_payable()
except Exception as e:
    st.error(f"初期化に失敗しました: {e}")
    st.stop()

c1, c2, c3 = st.columns([1, 1, 1])
target_ym = c1.text_input("対象月（例 2026-05）", value=st.session_state.get("payable_target_ym", ""),
                          key="csv_ym")
st.session_state["payable_target_ym"] = target_ym
exec_date = c2.text_input("振込実行日（MMDD 例 0430）", value=st.session_state.get("payable_exec", ""),
                          key="csv_exec")
st.session_state["payable_exec"] = exec_date
if c3.button("🔄 再読込", key="csv_reload"):
    st.session_state.pop("csv_invoices", None)

if "csv_invoices" not in st.session_state:
    st.session_state["csv_invoices"] = N.load_invoices(db_ids, target_ym=target_ym, status="確認済")
confirmed = st.session_state["csv_invoices"]

master_rows = N.load_master(db_ids)
look = matching.build_master_lookup(master_rows)

# 確認済請求書をマスタ口座に結合
records, skipped = [], []
for inv in confirmed:
    m = look["by_norm"].get(matching.normalize_name(inv["会社名"]))
    amount = int(inv["当月請求額"] or 0)
    if not m:
        skipped.append((inv["会社名"], "マスタ未登録"))
        continue
    if str(m.get("支払区分", "")).strip() == "カード払い":
        skipped.append((inv["会社名"], "カード払い（振込CSV対象外）"))
        continue
    if any(k in str(m.get("支払方法", "")) for k in ("口座振替", "現金")):
        skipped.append((inv["会社名"], f"{m.get('支払方法')}（振込CSV対象外）"))
        continue
    if str(m.get("除外フラグ", "")).strip() in ("✓", "○", "1"):
        skipped.append((inv["会社名"], "除外フラグ"))
        continue
    if not (m.get("銀行番号") and m.get("支店番号") and m.get("口座番号")):
        skipped.append((inv["会社名"], "口座情報が不足"))
        continue
    if amount <= 0:
        skipped.append((inv["会社名"], "金額が0以下"))
        continue
    records.append({
        "会社名": m["会社名"],
        "銀行番号": m["銀行番号"], "支店番号": m["支店番号"],
        "預金種目": m.get("預金種目", "普通"), "口座番号": m["口座番号"],
        "受取人口座名": m.get("受取人口座名", ""), "金額": amount,
    })

st.markdown(f"### 振込対象：{len(records)}件")
if records:
    st.dataframe(pd.DataFrame([{
        "会社名": r["会社名"], "銀行番号": str(r["銀行番号"]).zfill(4),
        "支店": str(r["支店番号"]).zfill(3), "種目": r["預金種目"],
        "口座番号": str(r["口座番号"]).zfill(7), "口座名": r["受取人口座名"],
        "金額": r["金額"],
    } for r in records]), use_container_width=True)
    st.markdown(f"**合計 {sum(r['金額'] for r in records):,} 円**")
else:
    st.info("『確認済』ステータスの振込対象がありません。『突合確認』でステータスを確認済にしてください。")

if skipped:
    with st.expander(f"⚠️ 対象外 {len(skipped)}件（要確認）", expanded=True):
        for name, reason in skipped:
            st.write(f"- {name}： {reason}")

st.markdown("---")
disabled = not records or not exec_date.strip()
if st.button("📥 楽天CSVを生成してダウンロード", type="primary", disabled=disabled,
             key="csv_gen"):
    csv_bytes = rakuten_csv.build_csv_bytes(records, exec_date.strip())
    csv_name = f"楽天総合振込_{target_ym}_{exec_date.strip()}.csv"
    try:
        N.save_transfer_history(db_ids, 実行日=exec_date.strip(), 対象月=target_ym, records=records)
    except Exception as e:  # noqa: BLE001
        st.warning(f"振込履歴の保存に失敗（CSVは生成済）: {e}")
    st.session_state["csv_payload"] = (csv_name, csv_bytes)
    st.session_state["csv_autodl"] = True
    st.session_state["csv_done_ym"] = target_ym  # この対象月の振込CSVを作った印
    st.rerun()

if st.session_state.pop("csv_autodl", False):
    import base64 as _b64
    import streamlit.components.v1 as _components
    name, data = st.session_state["csv_payload"]
    b64 = _b64.b64encode(data).decode()
    _components.html(f"""
    <script>
    var a=document.createElement('a');
    a.href='data:text/csv;base64,{b64}'; a.download={name!r};
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    </script>
    """, height=0)
    st.success("楽天CSVのダウンロードを開始しました。")

if st.session_state.get("csv_payload"):
    name, data = st.session_state["csv_payload"]
    with st.expander("ダウンロードされない場合（手動DL）", expanded=False):
        st.download_button("⬇️ 楽天総合振込CSV", data=data, file_name=name,
                           mime="text/csv", key="csv_dl_manual")


# ============================================================
# MFクラウド会計 仕訳インポート用CSV
# 振込CSVを作った後の続きの作業のため、楽天CSV生成後にだけ表示する。
# ============================================================
st.markdown("---")
if st.session_state.get("csv_done_ym") != target_ym:
    st.caption("📗 MFクラウド会計用CSV（買掛未払）は、上の楽天CSVを生成すると続けて作成できます。")
    # 振込対象がない月（全件が口座振替など）でも詰まらないよう、逃げ道だけ用意する
    if not st.checkbox("楽天CSVを作らずに買掛未払CSVだけ作る", key="mf_kk_alone"):
        st.stop()

st.header("📗 MFクラウド会計用CSV")
st.markdown("### ① 買掛未払CSV（当月発生分の計上）")
st.caption("対象月の請求書を取引先ごとに合算し、借方（仕入高など）／貸方（買掛金・未払金）の"
           "仕訳CSVを作ります。勘定科目は取引先マスタの設定を使います"
           "（未設定の取引先は『取引先マスタ』ページで取り込み・登録してください）。")

# 取引日＝振込実行日の前月末日＝対象月の末日。取引No＝1万台＋MMDD（MFは9桁以内の数字）。
_default_date, _default_no = "", ""
try:
    _y, _m = int(target_ym.split("-")[0]), int(target_ym.split("-")[1])
    _d = mf_csv.month_end(_y, _m)
    _default_date, _default_no = _d.strftime("%Y/%m/%d"), mf_csv.torihiki_no(_d)
except (ValueError, IndexError):
    pass

mc1, mc2, mc3 = st.columns([1, 1, 2])
mf_date = mc1.text_input("取引日（YYYY/MM/DD）", value=_default_date, key="mf_kk_date",
                         help="振込実行日の前月末日（＝対象月の末日）")
mf_no = mc2.text_input("取引No", value=_default_no, key="mf_kk_no",
                       help="1万台＋前月末日のMMDD。例: 7/31 → 10731（MFは9桁以内の数字）")
mf_only_ok = mc3.checkbox("『確認済』の請求書だけを対象にする", value=False, key="mf_kk_onlyok",
                          help="既定は読取済・確認済の両方（口座振替分も計上に含めるため）")
mf_every = mc3.checkbox("全行に取引No・取引日を出力する", value=False, key="mf_kk_every",
                        help="既定は1行目のみ（運用中の経理シートと同じ形式）")

if not target_ym.strip():
    st.info("対象月を入力してください。")
else:
    if st.button("🔄 請求書を再読込（買掛未払）", key="mf_kk_reload"):
        st.session_state.pop("mf_kk_invoices", None)
    if "mf_kk_invoices" not in st.session_state or \
            st.session_state.get("mf_kk_ym") != target_ym:
        st.session_state["mf_kk_invoices"] = N.load_invoices(db_ids, target_ym=target_ym)
        st.session_state["mf_kk_ym"] = target_ym
    kk_invoices = st.session_state["mf_kk_invoices"]

    # 取引先ごとに当月請求額(税込)を合算。勘定科目はマスタから。
    agg, mf_skipped, mixed_tax = {}, [], []
    for inv in kk_invoices:
        if inv.get("突合状態") == "対象外":
            continue  # 内訳・重複として除外指定されたファイル
        if inv.get("ステータス") == "保留":
            mf_skipped.append((inv["会社名"], "保留（未確定）"))
            continue
        if mf_only_ok and inv.get("ステータス") != "確認済":
            mf_skipped.append((inv["会社名"], f"{inv.get('ステータス')}（確認済のみ指定）"))
            continue
        m = matching.lookup_master(look, inv["会社名"])
        if not m:
            mf_skipped.append((inv["会社名"], "マスタ未登録"))
            continue
        if not str(m.get("借方勘定科目", "")).strip():
            mf_skipped.append((m["会社名"], "マスタに勘定科目が未設定"))
            continue
        key = m["会社名"]
        rec = agg.setdefault(key, {
            "借方勘定科目": m.get("借方勘定科目", ""), "借方補助科目": m.get("借方補助科目", ""),
            "借方税区分": m.get("借方税区分", ""), "貸方勘定科目": m.get("貸方勘定科目", ""),
            "貸方補助科目": m.get("貸方補助科目", ""), "貸方税区分": m.get("貸方税区分", ""),
            "摘要": (m.get("摘要", "") or m["会社名"]), "金額": 0,
            "_順": m.get("MF並び順") if str(m.get("MF並び順", "")).strip() != "" else 99999,
            "_件数": 0,
        })
        rec["金額"] += int(inv.get("当月請求額") or 0)
        rec["_件数"] += 1
        if inv.get("軽減税率") and "10%" in str(m.get("借方税区分", "")):
            mixed_tax.append(m["会社名"])

    mf_records = [r for r in sorted(agg.values(), key=lambda r: (float(r["_順"]), r["摘要"]))
                  if r["金額"] != 0]
    zero = [k for k, v in agg.items() if v["金額"] == 0]

    st.markdown(f"#### 計上対象：{len(mf_records)}件")
    if mf_records:
        st.dataframe(pd.DataFrame([{
            "借方勘定科目": r["借方勘定科目"], "借方補助科目": r["借方補助科目"],
            "税区分": r["借方税区分"], "金額": r["金額"],
            "貸方勘定科目": r["貸方勘定科目"], "貸方補助科目": r["貸方補助科目"],
            "摘要": r["摘要"], "請求書": r["_件数"],
        } for r in mf_records]), use_container_width=True)
        st.markdown(f"**合計 {sum(r['金額'] for r in mf_records):,} 円**"
                    f"（取引No {mf_no or '—'} / 取引日 {mf_date or '—'}）")
    else:
        st.info("計上対象がありません。対象月の請求書と、マスタの勘定科目設定をご確認ください。")

    if mixed_tax:
        st.warning("🍱 軽減税率(8%)を含む請求書があります（税区分は10%で出力）。"
                   "MF側で税区分の分割が必要かご確認ください： " + "、".join(sorted(set(mixed_tax))))
    if zero:
        st.caption("※ 合計0円のため出力していない取引先： " + "、".join(zero))
    if mf_skipped:
        with st.expander(f"⚠️ 対象外 {len(mf_skipped)}件（要確認）", expanded=bool(
                [s for s in mf_skipped if "未設定" in s[1] or "未登録" in s[1]])):
            for name, reason in mf_skipped:
                st.write(f"- {name}： {reason}")

    _mf_disabled = not mf_records or not mf_date.strip() or not mf_no.strip()
    if not _mf_disabled:
        kk_bytes = mf_csv.build_kaikake_csv(mf_records, mf_no.strip(), mf_date.strip(),
                                            every_row=mf_every)
        st.download_button("📥 買掛未払CSVをダウンロード", data=kk_bytes,
                           file_name=f"買掛未払_{target_ym}.csv", mime="text/csv",
                           type="primary", key="mf_kk_dl")
        with st.expander("生成内容（先頭5行）", expanded=False):
            st.code("\n".join(kk_bytes.decode("utf-8").splitlines()[:6]), language="text")
    else:
        st.button("📥 買掛未払CSVをダウンロード", disabled=True, key="mf_kk_dl_off")
