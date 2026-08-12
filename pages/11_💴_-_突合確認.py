# -*- coding: utf-8 -*-
"""
突合確認（買掛）

ネクストエンジンの発注データCSVをアップロードし、対象月(作成日=発注日 1〜末日)で
仕入先cd単位に合算。読取済の請求書と「会社名＋金額（許容誤差）」で突合し、
一致は緑・不一致は赤で表示。人が確認して「確認済→突合OK」に進める。
"""
import streamlit as st
import pandas as pd

st.set_page_config(page_title="突合確認", layout="wide")

from lib.auth import require_role
require_role("payable")  # 認証ゲート（AUTH_ENABLED=false なら素通り）
st.title("💴 突合確認（発注データとの照合）")
st.caption("ネクストエンジン発注データと請求書を会社名＋金額で突合します。")

from lib.payable import (app_init, matching, extract, business_day as BD,
                         notion_payable as N)

try:
    db_ids = app_init.init_payable()
except Exception as e:
    st.error(f"初期化に失敗しました: {e}")
    st.stop()

c1, c2, c3 = st.columns([1, 1, 1])
target_ym = c1.text_input("対象月（例 2026-05）",
                          value=(st.session_state.get("payable_target_ym")
                                 or BD.default_target_ym()),
                          key="match_ym", help="既定は前月（作業月の1つ前）です。")
st.session_state["payable_target_ym"] = target_ym
tol = c2.number_input("許容誤差（円）", min_value=0, value=10, step=1, key="match_tol",
                      help="請求額とNE合算額の差がこの範囲内なら『一致』とみなします"
                           "（税抜の端数ズレを吸収するため既定10円）。")
if c3.button("🔄 請求書を再読込", key="match_reload"):
    st.session_state.pop("match_invoices", None)
    st.session_state.pop("match_prev_ym", None)  # 前月の不一致も取り直す

try:
    y, m = (int(target_ym.split("-")[0]), int(target_ym.split("-")[1])) if "-" in target_ym else (None, None)
except (ValueError, IndexError):
    y, m = None, None

st.markdown("### 1. ネクストエンジン発注データCSV")
ne_file = st.file_uploader("発注データCSV（Shift-JIS/UTF-8）", type=["csv"], key="match_ne")
if ne_file is not None:
    st.session_state["match_ne_bytes"] = ne_file.getvalue()

ne_bytes = st.session_state.get("match_ne_bytes")
if not ne_bytes:
    st.info("発注データCSVをアップロードしてください。")
    st.stop()
if not (y and m):
    st.warning("対象月を YYYY-MM 形式で入力してください。")
    st.stop()

ne_rows = matching.read_ne_rows(ne_bytes)
ne_agg = matching.aggregate_ne(ne_rows, y, m)
st.success(f"発注データ {len(ne_rows)}行 / 対象月 {target_ym} の仕入先 {len(ne_agg)}件を合算しました。")

# 請求書(読取済以降)を取得
if "match_invoices" not in st.session_state:
    st.session_state["match_invoices"] = N.load_invoices(db_ids, target_ym=target_ym)
invoices = st.session_state["match_invoices"]
if not invoices:
    st.warning(f"対象月 {target_ym} の請求書がありません。先に『請求書取込』で登録してください。")
    st.stop()

master_rows = N.load_master(db_ids)
look = matching.build_master_lookup(master_rows)


def _drop_master_cache():
    """マスタを更新したら、取込ページ側のキャッシュも作り直させる。"""
    st.session_state["payable_master_nonce"] = \
        st.session_state.get("payable_master_nonce", 0) + 1


def _master_of(company):
    return look["by_norm"].get(matching.normalize_name(company))


def _ne_owner(v):
    """NE仕入先1件に対応するマスタ行を返す(仕入先cd優先→名称キー)。"""
    cd = (v.get("仕入先cd", "") or "").strip()
    if cd and cd in look["by_cd"]:
        return look["by_cd"][cd]
    for k in (v.get("名候補") or matching.name_keys(v.get("仕入先名", ""))):
        m = look["by_norm"].get(k)
        if m:
            return m
    return None


# ── NE仕入先とマスタの紐付け状況 ─────────────────────────────
# 突合は「請求書 → マスタ会社名 → NE仕入先」の順に辿る。マスタのNE仕入先cdが
# 空でも名称で拾えるようにしてあるが、名称が全く違う取引先はここで手当てする。
_unlinked = [(k, v) for k, v in ne_agg.items() if _ne_owner(v) is None]
with st.expander(f"🔗 NE仕入先とマスタの紐付け（未紐付け {len(_unlinked)} / 全{len(ne_agg)}社）",
                 expanded=bool(_unlinked)):
    st.caption("マスタに結びつかないNE仕入先の一覧です。ここで取引先を選んで紐づけると、"
               "マスタの『NE仕入先cd』に保存され、次回以降は確実に突合されます。")
    if not _unlinked:
        st.success("対象月のNE仕入先はすべて取引先マスタに紐付いています。")
    for k, v in _unlinked:
        cd = (v.get("仕入先cd", "") or "").strip()
        cA, cB = st.columns([3, 2])
        cA.markdown(f"**{v.get('仕入先名','')}**　`{cd or '(cdなし)'}`　"
                    f"発注 {int(v.get('合算額', 0)):,}円＋送料 {int(v.get('送料', 0)):,}円 "
                    f"／ {v.get('件数', 0)}件")
        try:
            cands = matching.find_candidates(v.get("仕入先名", ""), list(master_rows))
        except Exception:  # noqa: BLE001
            cands = []
        opts = ["（紐づけない）"] + cands + [m.get("会社名", "") for m in master_rows
                                        if m.get("会社名") and m.get("会社名") not in cands]
        pick = cB.selectbox("紐づける取引先", opts, key=f"link_sel_{k}",
                            label_visibility="collapsed")
        if cB.button("🔗 このマスタに紐づける", key=f"link_btn_{k}",
                     disabled=(pick == "（紐づけない）" or not cd), use_container_width=True):
            try:
                N.set_ne_cd_by_company(db_ids, pick, cd, overwrite=True)
                _drop_master_cache()
                st.success(f"『{pick}』に仕入先cd {cd} を登録しました。突合を再実行してください。")
                st.rerun()
            except Exception as e:  # noqa: BLE001
                st.error(f"紐付けに失敗しました: {e}")
        if not cd:
            cB.caption("※ NE側に仕入先cdが無いため、名称一致でのみ突合されます。")

def _parse_extax_breakdown(s):
    """税内訳 '10%:税抜68489/税6849, 8%:…' から税抜合計を復元。無ければNone。"""
    import re
    import unicodedata
    if not s:
        return None
    vals = re.findall(r"税抜\s*(-?[\d,]+)", unicodedata.normalize("NFKC", str(s)))
    if not vals:
        return None
    try:
        return sum(int(v.replace(",", "")) for v in vals)
    except ValueError:
        return None


def _extax_info(inv):
    """突合に使う税抜額と、その出所('税抜'/'内訳'/'逆算')を返す。
    税込しか読めなかった場合は税率で逆算して税抜換算する(突合画面のみの計算)。"""
    v = inv.get("当月税抜額")
    if v:
        return v, "税抜"
    bd = _parse_extax_breakdown(inv.get("税内訳", ""))
    if bd is not None:
        return bd, "内訳"
    inc = inv.get("当月請求額") or 0
    rate = 1.08 if inv.get("軽減税率") else 1.10
    return round(inc / rate), "逆算"


def _extax(inv):
    return _extax_info(inv)[0]


st.markdown("### 2. 突合結果")
st.caption("NE発注データは税抜のため、突合は『当月税抜額』で行います（振込CSVは税込で作成）。"
           "税抜額が未読取のものは税内訳から復元し、それも無ければ税込額から逆算（『（逆算）』表記）。")
if st.button("🔁 突合を実行/再計算", type="primary", key="match_run"):
    # 取込ページでの会社名修正・金額修正を確実に反映するため、最新をNotionから再取得
    st.session_state["match_invoices"] = N.load_invoices(db_ids, target_ym=target_ym)
    invoices = st.session_state["match_invoices"]
    # ステータスのプルダウン既定値を再適用するため、既存のウィジェット状態をリセット
    for _k in [k for k in list(st.session_state.keys()) if str(k).startswith("match_st_")]:
        del st.session_state[_k]
    linked = []  # 名称一致で判明した仕入先cdをマスタへ保存(次回から確実に紐づく)
    auto_confirmed = []  # 請求 < 発注 で自動的に確認済にしたもの
    for inv in invoices:
        if inv.get("突合状態") in ("対象外", "口座振替"):
            continue  # 突合しない指定・口座振替はスキップ(保持はする)
        r = matching.match_invoice(inv["会社名"], _extax(inv), look, ne_agg, tolerance=tol)
        denpyo = ",".join(str(d) for d in r.get("NE伝票", []))
        # 名称で拾えた＝マスタのNE仕入先cdが空。判明したcdをマスタに登録しておく。
        if r.get("紐付け方法") == "名称" and r.get("NE仕入先cd"):
            mrow = _master_of(inv["会社名"])
            if mrow and not str(mrow.get("NE仕入先cd", "") or "").strip():
                try:
                    if N.set_ne_cd_by_company(db_ids, mrow.get("会社名", ""), r["NE仕入先cd"]):
                        linked.append(f"{mrow.get('会社名','')}→{r['NE仕入先cd']}")
                except Exception:  # noqa: BLE001
                    pass
        # 請求が発注より少ない(差額マイナス)＝払い過ぎにならないので自動で確認済にする
        auto_ok = (r["状態"] == "金額不一致" and (r.get("差額") or 0) < 0
                   and inv.get("ステータス") == "読取済")
        try:
            N.update_invoice_fields(
                db_ids, inv["id"],
                突合状態=r["状態"],
                NE合算額=r["NE合算額"], NE送料=r.get("NE送料", 0),
                差額=r["差額"], NE発注番号=denpyo,
                **({"ステータス": "確認済"} if auto_ok else {}),
            )
            if auto_ok:
                inv["ステータス"] = "確認済"
                auto_confirmed.append(f"{inv['会社名']}（{r['差額']:+,}円）")
            inv["突合状態"] = r["状態"]
            inv["NE合算額"] = r["NE合算額"]
            inv["NE送料"] = r.get("NE送料", 0)
            inv["NE合計"] = r.get("NE合計")
            inv["差額"] = r["差額"]
            inv["NE発注番号"] = denpyo
        except Exception as e:  # noqa: BLE001
            st.error(f"{inv['会社名']} の更新に失敗: {e}")
    if linked:
        _drop_master_cache()
        st.info(f"🔗 {len(linked)}社のNE仕入先cdをマスタに自動登録しました："
                + "、".join(linked))
    if auto_confirmed:
        st.info(f"✅ 請求額が発注額より少ないため、{len(auto_confirmed)}社を自動で『確認済』に"
                "しました（払い過ぎにならないため）： " + "、".join(auto_confirmed))
    st.toast("突合を更新しました")


# 数値整形: 小数があれば表示、なければ整数(カンマ区切り)
def yen(v):
    if v is None or v == "":
        return ""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{int(f):,}" if f == int(f) else f"{f:,.2f}"


def _row_color(stt):
    return {
        "一致": "background-color:#e6f4ea",
        "金額不一致": "background-color:#fde8e8",
        "発注なし": "background-color:#fff4e5",
        "マスタ未登録": "background-color:#fffbe6",
    }.get(stt, "")


def _ne_total(i):
    if i.get("NE合計") is not None:
        return i.get("NE合計")
    if i.get("NE合算額") is not None:
        return (i.get("NE合算額") or 0) + (i.get("NE送料") or 0)
    return None


def _kamoku(inv):
    mm = look["by_norm"].get(matching.normalize_name(inv["会社名"]))
    return str(mm.get("科目", "")) if mm else ""


def _ne_link_label(inv):
    """この請求書がどのNE仕入先と結びついているかを表示用に返す。"""
    m = _master_of(inv["会社名"])
    cd = str((m or {}).get("NE仕入先cd", "") or "").strip()
    if cd and cd in ne_agg:
        return f"{ne_agg[cd].get('仕入先名','')}（{cd}）"
    if cd:
        return f"cd {cd}（対象月の発注なし）"
    for k in matching.company_keys(m, extra=[inv["会社名"]]):
        for v in ne_agg.values():
            if k in (v.get("名候補") or set()):
                return f"{v.get('仕入先名','')}（{v.get('仕入先cd','') or '名称一致'}）"
    return "未紐付け"


def _shiire_no_order(inv):
    """科目が仕入なのに発注なし(=締め跨ぎ等の要調査)。"""
    return inv.get("突合状態") == "発注なし" and _kamoku(inv) == "仕入"

df = pd.DataFrame([{
    "会社名": i["会社名"],
    "当月税抜(突合)": yen(_extax(i)) + ("（逆算）" if _extax_info(i)[1] == "逆算" else ""),
    "NE発注額": yen(i.get("NE合算額")), "送料": yen(i.get("NE送料")),
    "NE合計": yen(_ne_total(i)), "差額": yen(i.get("差額")),
    "突合状態": (i.get("突合状態", "未突合")
              + ("（仕入・締め跨ぎ?）" if _shiire_no_order(i) else "")),
    "当月税込": yen(i["当月請求額"]),
    "ステータス": i["ステータス"], "口座相違": "⚠️" if i.get("口座相違フラグ") else "",
} for i in invoices])

# 行の色: 金額不一致、または『仕入なのに発注なし』は赤
_colors = []
for i in invoices:
    stt = i.get("突合状態", "未突合")
    if stt == "金額不一致" or _shiire_no_order(i):
        _colors.append("background-color:#fde8e8")
    else:
        _colors.append(_row_color(stt))
styled = df.style.apply(lambda s: _colors, subset=["突合状態"])
st.dataframe(styled, use_container_width=True)

n_ok = sum(1 for i in invoices if i.get("突合状態") == "一致")
n_err = sum(1 for i in invoices if i.get("突合状態") in ("金額不一致", "発注なし"))
st.caption(f"一致 {n_ok}件 / 要確認 {n_err}件 / 全{len(invoices)}件")
if any(_shiire_no_order(i) for i in invoices):
    st.warning("🔴 科目『仕入』なのに発注が見つからない取引先があります。"
               "締め日の跨ぎ（月初/末日でのズレ）の可能性があるため、NEの発注日や前後月をご確認ください。")
_fallback = [i["会社名"] for i in invoices
             if i.get("突合状態") not in ("対象外", "口座振替") and _extax_info(i)[1] == "逆算"]
if _fallback:
    st.info("💴 税抜額が読み取れなかったため、税込額から逆算して突合している請求書があります"
            "（端数で±数円ズレる場合は許容誤差を設定してください）： "
            + "、".join(_fallback))

# 同一会社名の重複検知(突合対象外=対象外は除外)
from collections import defaultdict
_groups = defaultdict(list)
for inv in invoices:
    if inv.get("突合状態") in ("対象外", "口座振替"):
        continue
    _groups[matching.normalize_name(inv["会社名"])].append(inv)
_dups = {k: v for k, v in _groups.items() if len(v) > 1}
if _dups:
    st.markdown("### ⚠️ 同一会社名の重複")
    st.caption(f"{len(_dups)}社で会社名が重複しています。残すレコードを選び、それ以外を削除できます。")
    for norm, recs in _dups.items():
        with st.container(border=True):
            st.markdown(f"**{recs[0]['会社名']}**（{len(recs)}件）")
            labels = [f"{yen(r['当月請求額'])}円 / {r.get('ファイルリンク','') or '(ファイル名なし)'} "
                      f"/ {r['ステータス']} / {r.get('突合状態','')}" for r in recs]
            keep = st.radio("残すレコード", options=list(range(len(recs))),
                            format_func=lambda j, _l=labels: _l[j], key=f"dupkeep_{norm}")
            with st.popover("🗑️ 選択以外を削除"):
                st.warning("⚠️ 必ず請求書（PDF）の内容を確認してから実行してください。"
                           "削除は取り消せません。")
                ok = st.checkbox("請求書を確認しました", key=f"dupok_{norm}")
                if st.button("選択以外を削除する", type="primary", disabled=not ok,
                             key=f"dupbtn_{norm}"):
                    for j, r in enumerate(recs):
                        if j != keep:
                            N.delete_invoice(db_ids, r["id"])
                    st.session_state.pop("match_invoices", None)
                    st.rerun()

st.markdown("### 3. ステータス更新")
st.caption("各請求書を確認し、問題なければ『確認済』にしてください"
           "（確認済＝振込CSVに出してよい最終承認）。保留・対象外はここには出ません。")

# プレビュー用に請求書ファイルを読み込む(任意)。ファイル名一致で各行に表示。
with st.expander("📄 プレビュー用に請求書ファイルを読み込む（任意・ZIP可）", expanded=False):
    prev_files = st.file_uploader("PDF / 画像 / ZIP", type=["pdf", "png", "jpg", "jpeg", "webp", "zip"],
                                  accept_multiple_files=True, key="match_prevup")
    if prev_files:
        st.session_state["match_filebytes"] = {
            n: b for n, b in extract.iter_files_from_uploads(prev_files)}
        st.caption(f"{len(st.session_state['match_filebytes'])}件を読み込みました。")
fbmap = st.session_state.get("match_filebytes", {})


# ── 前月の金額不一致（月またぎのズレを見つけやすくする） ──────────
def _prev_ym(ym):
    try:
        y, m = int(ym.split("-")[0]), int(ym.split("-")[1])
    except (ValueError, IndexError):
        return ""
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


_pym = _prev_ym(target_ym)
if _pym and st.session_state.get("match_prev_ym") != _pym:
    try:
        _prev_rows = N.load_invoices(db_ids, target_ym=_pym)
    except Exception:  # noqa: BLE001
        _prev_rows = []
    st.session_state["match_prev_ym"] = _pym
    st.session_state["match_prev_diff"] = {
        matching.normalize_name(r["会社名"]): r for r in _prev_rows
        if r.get("突合状態") == "金額不一致"}
prev_diff = st.session_state.get("match_prev_diff", {})


def _prev_note(inv):
    """前月に金額不一致だった取引先は、その差額を見出しに出す。"""
    p = prev_diff.get(matching.normalize_name(inv["会社名"]))
    if not p:
        return ""
    return f"　|　🔁前月不一致 {yen(p.get('差額'))}円"


NE_URL = "https://main.next-engine.com/userg5210?dnum={}"
_ICON = {"一致": "✅", "金額不一致": "⚠️", "発注なし": "🟠", "マスタ未登録": "🟡", "未突合": "⬜"}
_STAT = ["読取済", "確認済"]  # 簡素化: 読取済→確認済の2段(確認済=振込CSV対象)

# 対象外・保留はこのページには出さない(取込ページで扱う)
visible = [i for i in invoices
           if i.get("突合状態") not in ("対象外", "口座振替") and i.get("ステータス") != "保留"]
if not visible:
    st.info("表示対象の請求書がありません（保留・対象外を除く）。")
for inv in visible:
    stt = inv.get("突合状態", "未突合")
    icon = "🔴" if _shiire_no_order(inv) else _ICON.get(stt, "")
    diff = inv.get("差額")
    head = (f"{icon} {inv['会社名']}　|　突合 {stt}"
            f"（差額 {yen(diff)}円）　|　ステータス: {inv['ステータス']}"
            f"{_prev_note(inv)}")
    # 確認済＝作業が終わった行なので閉じておく（残りの作業に集中できるように）
    _open = (stt != "一致") and inv["ステータス"] != "確認済"
    with st.expander(head, expanded=_open):
        if _shiire_no_order(inv):
            st.error("🔴 科目『仕入』なのに発注なし。締め日の跨ぎ（月初/末日のズレ）の可能性。"
                     "NEの発注日・前後月をご確認ください。")
        # 発注なし＝NE仕入先と結びついていない可能性。似た仕入先を候補提示して紐付け。
        if stt == "発注なし":
            _mrow = _master_of(inv["会社名"])
            try:
                _nec = matching.find_ne_candidates(inv["会社名"], ne_agg, _mrow)
            except Exception:  # noqa: BLE001
                _nec = []
            if _nec:
                st.warning("NE発注データに、名前の似た仕入先があります。"
                           "同じ取引先ならここで紐づけると突合されます。")
                _lab = {f"{nm}（{c}）／ {int(ne_agg.get(c, {}).get('合算額', 0)):,}円": c
                        for c, nm, _s in _nec if c}
                if _lab and _mrow:
                    lc1, lc2 = st.columns([3, 1])
                    sel = lc1.selectbox("NE仕入先の候補", ["（紐づけない）"] + list(_lab),
                                        key=f"nolink_sel_{inv['id']}")
                    if lc2.button("🔗 紐づける", key=f"nolink_btn_{inv['id']}",
                                  disabled=(sel == "（紐づけない）"), use_container_width=True):
                        try:
                            N.set_ne_cd_by_company(db_ids, _mrow.get("会社名", ""),
                                                   _lab[sel], overwrite=True)
                            _drop_master_cache()
                            st.success(f"『{_mrow.get('会社名','')}』に仕入先cd {_lab[sel]} を"
                                       "登録しました。『突合を実行/再計算』を押してください。")
                            st.rerun()
                        except Exception as e:  # noqa: BLE001
                            st.error(f"紐付けに失敗しました: {e}")
                elif not _mrow:
                    st.caption("※ この会社名がマスタに見つからないため紐付けできません。"
                               "先に会社名の修正（取込ページ）またはマスタ登録をしてください。")
        if fbmap.get(inv.get("ファイルリンク", "")):
            if st.toggle("📄 プレビュー表示", key=f"match_prev_{inv['id']}"):
                for _img in extract.render_preview_images(
                        fbmap[inv["ファイルリンク"]], inv["ファイルリンク"]):
                    st.image(_img, use_container_width=True)
        cL, cR = st.columns(2)
        # 左: 金額・突合
        cL.markdown(
            f"**金額**\n\n"
            f"- 当月税抜(突合用): **{yen(_extax(inv))}** 円\n"
            f"- 当月請求(税込・振込用): **{yen(inv.get('当月請求額'))}** 円\n"
            f"- 今回請求(繰越込): {yen(inv.get('今回請求額'))} 円 / 前月繰越: {yen(inv.get('前月繰越額'))} 円\n"
            f"- 税内訳: {inv.get('税内訳','') or '—'}"
            f"{'　🍱軽減税率' if inv.get('軽減税率') else ''}\n\n"
            f"**突合**\n\n"
            f"- NE発注額(税抜): {yen(inv.get('NE合算額'))} ＋ 送料: {yen(inv.get('NE送料'))} "
            f"= NE合計: **{yen(_ne_total(inv))}** 円\n"
            f"- 差額: **{yen(diff)}** 円\n"
            f"- 紐付けNE仕入先: {_ne_link_label(inv)}")
        denpyo = [d for d in str(inv.get("NE発注番号", "")).split(",") if d.strip()]
        if denpyo:
            links = "　".join(
                f'<a href="{NE_URL.format(d.strip())}" target="_blank" rel="noopener">📄{d.strip()}</a>'
                for d in denpyo)
            cL.markdown(f"NE発注書: {links}", unsafe_allow_html=True)
        # 右: 口座・取引先・期日・ファイル
        cR.markdown(
            f"**振込先（請求書から抽出）**\n\n"
            f"- {inv.get('抽出_銀行','') or '—'} {inv.get('抽出_支店','')} "
            f"{inv.get('抽出_預金種目','')} {inv.get('抽出_口座番号','')}\n"
            f"- 名義: {inv.get('抽出_口座名義','') or '—'}\n"
            f"{'- ⚠️ **口座変更の可能性あり**' if inv.get('口座相違フラグ') else ''}\n\n"
            f"**その他**\n\n"
            f"- 請求日: {inv.get('請求日','') or '—'} / 支払期日: {inv.get('支払期日','') or '—'}\n"
            f"- カテゴリ: {inv.get('カテゴリ','') or '—'}\n"
            f"- ファイル: {inv.get('ファイルリンク','') or '—'}\n"
            f"- AIメモ: {inv.get('抽出メモ','') or '—'}")
        # 操作
        oc1, oc2, oc3 = st.columns([2, 1, 1])
        # 既定値: 一致(緑・アコーディオンが閉じるもの)は『確認済』、要確認(開くもの)は現状のまま。
        # selectboxはセッションに値が残るとindexが効かないため、明示的に初期化する。
        # 一致、または請求が発注より少ない(差額マイナス)なら既定を『確認済』にする
        _auto = (stt == "一致") or (stt == "金額不一致" and (diff or 0) < 0)
        default_stat = ("確認済" if (_auto and inv["ステータス"] == "読取済")
                        else inv["ステータス"])
        skey = f"match_st_{inv['id']}"
        if skey not in st.session_state:
            st.session_state[skey] = default_stat if default_stat in _STAT else _STAT[0]
        new_status = oc1.selectbox("ステータス", _STAT, key=skey)
        if oc2.button("更新", key=f"match_upd_{inv['id']}", use_container_width=True):
            N.update_invoice_fields(db_ids, inv["id"], ステータス=new_status)
            inv["ステータス"] = new_status
            st.toast(f"{inv['会社名']} → {new_status}")
            st.rerun()
        with oc3.popover("🗑️削除", use_container_width=True):
            st.warning("⚠️ 削除前に必ず請求書（PDF）の内容を確認してください。削除は取り消せません。")
            ok = st.checkbox("請求書を確認しました", key=f"delok_{inv['id']}")
            if st.button("この請求書を削除する", type="primary", disabled=not ok,
                         key=f"match_del_{inv['id']}"):
                N.delete_invoice(db_ids, inv["id"])
                st.session_state.pop("match_invoices", None)
                st.rerun()

# 一括更新: 各行のプルダウンの値でまとめてステータス更新
if visible:
    st.markdown("---")
    _bulk_msg = st.session_state.pop("match_bulk_msg", None)
    if _bulk_msg:
        st.success(_bulk_msg)
    _pending = sum(
        1 for inv in visible
        if st.session_state.get(f"match_st_{inv['id']}", inv["ステータス"]) != inv["ステータス"])
    if st.button(f"💾 全取引先を一括更新（変更 {_pending}件）", type="primary",
                 key="match_bulk_upd", disabled=_pending == 0):
        n = 0
        for inv in visible:
            new_st = st.session_state.get(f"match_st_{inv['id']}", inv["ステータス"])
            if new_st != inv["ステータス"]:
                try:
                    N.update_invoice_fields(db_ids, inv["id"], ステータス=new_st)
                    n += 1
                except Exception as e:  # noqa: BLE001
                    st.error(f"{inv['会社名']} の更新に失敗: {e}")
        st.session_state.pop("match_invoices", None)
        st.session_state["match_bulk_msg"] = f"{n}件のステータスを一括更新しました。"
        st.rerun()
