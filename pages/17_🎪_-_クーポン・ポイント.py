# -*- coding: utf-8 -*-
"""
クーポン・ポイント変倍（イベントLP作成のPhase 2）

RMSクーポンAPIでクーポンを自動発行し、getkey URLをNotionに保存。
イベントLP作成ページ(16)のクーポンセクションから選択して埋め込める。
ポイント変倍はItem API 2.0で対象商品へ一括設定・解除する。
APIが使えない場合は手動設定手順書(Markdown)を出力するフォールバック付き。
"""
import datetime

import streamlit as st

st.set_page_config(page_title="クーポン・ポイント", layout="wide")

from lib.auth import require_role
require_role("event")
st.title("🎫 クーポン・ポイント変倍")
st.caption("クーポンの自動発行と、対象商品へのポイント変倍設定を行います。")

from lib.event import app_init, coupon as CP, item_fetch, notion_event as N, point as PT, rms_api

try:
    db_ids = app_init.init_event()
except Exception as e:  # noqa: BLE001
    st.error(f"初期化に失敗しました: {e}")
    st.stop()

if not rms_api.is_configured():
    st.warning("RMSキー（RMS_SERVICE_SECRET / RMS_LICENSE_KEY）が未設定のため、"
               "API自動発行は使えません。手動発行の記録は可能です。")


@st.cache_data(ttl=300, show_spinner=False)
def _load_coupons_cached(_nonce):
    return N.load_coupons(db_ids, active_only=False)


def _coupons():
    return _load_coupons_cached(st.session_state.get("coupon_nonce", 0))


def _bump():
    st.session_state["coupon_nonce"] = st.session_state.get("coupon_nonce", 0) + 1


def _discount_text(label, factor):
    if label == "送料無料":
        return "送料無料"
    return f"{int(factor):,}円OFF" if "定額" in label else f"{int(factor)}%OFF"


tab_cp, tab_pt = st.tabs(["🎫 クーポン発行", "⭐ ポイント変倍"])

# ============================================================
# クーポン発行
# ============================================================
with tab_cp:
    st.subheader("新規クーポン発行")
    c1, c2 = st.columns(2)
    with c1:
        cp_name = st.text_input("クーポン名（お客様に表示されます）",
                                placeholder="例）スーパーセール限定 500円OFFクーポン")
        cp_caption = st.text_input("説明（任意）", placeholder="例）5,000円以上のお買い物で利用可")
        cp_type = st.radio("値引きタイプ", list(CP.DISCOUNT_TYPES.keys()), horizontal=True)
        cp_factor = st.number_input(
            "値引き額（円） / 率（%）", min_value=0, max_value=1000000,
            value=500, disabled=(cp_type == "送料無料"))
    with c2:
        cp_start = st.text_input("利用期間 開始", placeholder="2026-09-04 20:00")
        cp_end = st.text_input("利用期間 終了", placeholder="2026-09-11 01:59")
        cc1, cc2 = st.columns(2)
        cp_issue = cc1.number_input("発行枚数（総数）", min_value=1, max_value=999999, value=100)
        cp_member = cc2.number_input("1人あたり利用回数", min_value=1, max_value=99, value=1)
        cp_combine = cc1.checkbox("他クーポンと併用可", value=True)
        cp_display = cc2.checkbox("ショップページに表示", value=True)
    cp_all = st.checkbox("全商品を対象にする（受注クーポン）", value=False)
    cp_items_text = st.text_area("対象商品の管理番号（改行・カンマ区切り）",
                                 disabled=cp_all, height=80)
    cp_numbers = item_fetch.parse_manage_numbers(cp_items_text)

    def _issue_kwargs():
        return dict(coupon_name=cp_name, caption=cp_caption, start=cp_start, end=cp_end,
                    discount_label=cp_type,
                    discount_factor=(0 if cp_type == "送料無料" else cp_factor),
                    issue_count=cp_issue, member_max=cp_member,
                    manage_numbers=cp_numbers, all_items=cp_all,
                    combine=cp_combine, display=cp_display)

    def _validate():
        if not cp_name.strip():
            return "クーポン名を入力してください。"
        if not cp_start.strip() or not cp_end.strip():
            return "利用期間（開始・終了）を『YYYY-MM-DD HH:MM』形式で入力してください。"
        if not cp_all and not cp_numbers:
            return "対象商品の管理番号を入力するか、全商品対象にチェックしてください。"
        if cp_type != "送料無料" and int(cp_factor) <= 0:
            return "値引き額/率を入力してください。"
        return None

    with st.expander("送信内容の確認（XMLプレビュー）"):
        try:
            st.code(CP.build_issue_xml(**_issue_kwargs()), language="xml")
        except CP.CouponError as e:
            st.caption(f"（入力が揃うと表示されます: {e}）")
        except Exception:  # noqa: BLE001
            st.caption("（入力が揃うと表示されます）")

    if st.button("🎫 クーポンを発行", type="primary", disabled=not rms_api.is_configured()):
        err = _validate()
        if err:
            st.error(err)
        else:
            try:
                xml_body = CP.build_issue_xml(**_issue_kwargs())
                with st.spinner("RMSへ発行リクエスト中..."):
                    result = CP.issue(xml_body)
                N.save_coupon(db_ids, {
                    "クーポン名": cp_name,
                    "couponCode": result["couponCode"],
                    "getkey URL": result["getkey_url"],
                    "値引き表示": _discount_text(cp_type, cp_factor),
                    "説明": cp_caption,
                    "期間開始": cp_start, "期間終了": cp_end,
                    "対象商品": "全商品" if cp_all else ",".join(cp_numbers),
                    "発行方式": "API自動",
                })
                _bump()
                st.success(f"発行しました！ couponCode: `{result['couponCode']}`")
                st.markdown(f"**獲得URL**: {result['getkey_url']}")
                st.caption("イベントLP作成ページのクーポンセクションで「発行済みから選択」できます。")
            except (CP.CouponError, rms_api.RMSError) as e:
                st.error(str(e))
                st.download_button(
                    "📄 手動発行手順書をダウンロード",
                    data=CP.manual_procedure_md(**{
                        k: v for k, v in _issue_kwargs().items()
                        if k not in ("combine", "display")}).encode("utf-8"),
                    file_name="クーポン手動発行手順.md", mime="text/markdown")

    with st.expander("手動発行したクーポンを記録する（APIを使わない場合）"):
        m1, m2 = st.columns(2)
        manual_name = m1.text_input("クーポン名", key="manual_cp_name")
        manual_disc = m2.text_input("値引き表示（例 500円OFF）", key="manual_cp_disc")
        manual_getkey = st.text_input("getkey（getCoupon?getkey=◯◯ の◯◯部分）",
                                      key="manual_cp_key")
        if st.button("記録する", key="manual_cp_save"):
            if not manual_name or not manual_getkey:
                st.error("クーポン名とgetkeyを入力してください。")
            else:
                N.save_coupon(db_ids, {
                    "クーポン名": manual_name,
                    "couponCode": manual_getkey,
                    "getkey URL": f"https://coupon.rakuten.co.jp/getCoupon?getkey={manual_getkey}",
                    "値引き表示": manual_disc, "発行方式": "手動",
                })
                _bump()
                st.success("記録しました。")

    st.subheader("発行済みクーポン")
    coupons = _coupons()
    if not coupons:
        st.caption("まだクーポンがありません。")
    else:
        import pandas as pd
        st.dataframe(pd.DataFrame([{
            "クーポン名": c["クーポン名"], "値引き": c["値引き表示"],
            "期間": f"{c['期間開始']}〜{c['期間終了']}", "対象": c["対象商品"],
            "方式": c["発行方式"], "状態": c["状態"], "couponCode": c["couponCode"],
        } for c in coupons]), use_container_width=True, hide_index=True)
        active = [c for c in coupons if c["状態"] == "有効"]
        if active and rms_api.is_configured():
            with st.expander("クーポンを削除する"):
                sel = st.selectbox("削除対象",
                                   [f"{c['クーポン名']}（{c['couponCode']}）" for c in active])
                if st.button("🗑 RMSから削除", key="cp_delete"):
                    target = active[[f"{c['クーポン名']}（{c['couponCode']}）"
                                     for c in active].index(sel)]
                    try:
                        CP.delete(target["couponCode"])
                        N.mark_coupon_deleted(db_ids, target["id"])
                        _bump()
                        st.success("削除しました。")
                    except Exception as e:  # noqa: BLE001
                        st.error(f"削除に失敗: {e}")

# ============================================================
# ポイント変倍
# ============================================================
with tab_pt:
    st.subheader("ポイント変倍の一括設定")
    st.caption("対象商品にポイント変倍（2〜20倍）と適用期間を設定します。イベント終了後の解除もここから。")
    p1, p2, p3 = st.columns([1, 1.5, 1.5])
    pt_rate = p1.number_input("倍率", min_value=2, max_value=20, value=5)
    pt_start = p2.text_input("適用開始", placeholder="2026-09-04 20:00", key="pt_start")
    pt_end = p3.text_input("適用終了", placeholder="2026-09-11 01:59", key="pt_end")
    pt_items_text = st.text_area("対象商品の管理番号（改行・カンマ区切り）", height=80, key="pt_items")
    pt_numbers = item_fetch.parse_manage_numbers(pt_items_text)

    if st.button("🔍 現在の設定を確認", disabled=not (pt_numbers and rms_api.is_configured())):
        rows = []
        for mn in pt_numbers:
            try:
                cur = PT.get_current(mn)
                rows.append({"管理番号": mn,
                             "現在の倍率": cur["rate"] if cur else "（未設定）",
                             "期間": f"{cur['start']}〜{cur['end']}" if cur else ""})
            except Exception as e:  # noqa: BLE001
                rows.append({"管理番号": mn, "現在の倍率": f"取得エラー: {e}", "期間": ""})
        import pandas as pd
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    b1, b2 = st.columns(2)
    if b1.button("⭐ 一括設定を実行", type="primary",
                 disabled=not (pt_numbers and rms_api.is_configured())):
        try:
            PT._fmt_dt(pt_start), PT._fmt_dt(pt_end)  # 事前バリデーション
            with st.spinner(f"{len(pt_numbers)}商品に設定中..."):
                ok, errors = PT.bulk_apply(pt_numbers, rate=pt_rate,
                                           start=pt_start, end=pt_end)
            if ok:
                st.success(f"設定完了: {len(ok)}商品（{pt_rate}倍 / {pt_start}〜{pt_end}）")
            if errors:
                st.error("失敗:\n" + "\n".join(f"- {k}: {v}" for k, v in errors.items()))
                st.download_button(
                    "📄 手動設定手順書をダウンロード",
                    data=PT.manual_procedure_md(rate=pt_rate, start=pt_start, end=pt_end,
                                                manage_numbers=list(errors)).encode("utf-8"),
                    file_name="ポイント変倍手動設定手順.md", mime="text/markdown")
        except PT.PointError as e:
            st.error(str(e))
    with b2:
        pt_confirm = st.checkbox("解除を実行してよい（対象商品の変倍が消えます）")
        if st.button("🧹 一括解除", disabled=not (pt_numbers and pt_confirm
                                              and rms_api.is_configured())):
            with st.spinner("解除中..."):
                ok, errors = PT.bulk_clear(pt_numbers)
            if ok:
                st.success(f"解除完了: {len(ok)}商品")
            if errors:
                st.error("失敗:\n" + "\n".join(f"- {k}: {v}" for k, v in errors.items()))
