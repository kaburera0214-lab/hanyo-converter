# -*- coding: utf-8 -*-
"""
商品ページ自動更新（EC-UP置換の内製版）

スマホ用商品説明文にパンくずリスト・商品スコア等のブロックをマーカー方式で
自動挿入する。ここでは設定編集・プレビュー・テスト反映・診断を行い、
毎日の自動更新はGitHub Actions（batch/autopage_update.py）が担う。
"""
import copy
import json

import streamlit as st

st.set_page_config(page_title="商品ページ自動更新", layout="wide")

from lib.auth import require_role
require_role("autopage")

st.title("🛍 商品ページ自動更新")
st.caption("EC-UP相当のブロック（パンくず・商品スコア等）を自社運用で商品ページへ自動挿入します。")

from lib.autopage import config as apconfig
from lib.autopage import compose, reviews, rms_items, runner
from lib.autopage import state as apstate
from lib.event import rms_api

cfg = apconfig.load_config()

# ---- 状態バナー ----
c1, c2, c3, c4 = st.columns(4)
c1.metric("全体スイッチ", "ON" if cfg.get("enabled") else "OFF")
c2.metric("モード", "実反映" if not cfg.get("dry_run") else "dry-run")
c3.metric("対象商品", f"{len(cfg.get('allowlist') or [])}件" if cfg.get("allowlist") else "全商品(未対応)")
c4.metric("RMS認証", "設定済み" if rms_api.is_configured() else "未設定")

if not rms_api.is_configured():
    st.warning("Secretsに RMS_SERVICE_SECRET / RMS_LICENSE_KEY が未設定です。プレビュー・反映はできません。")

tab_status, tab_preview, tab_config, tab_diag = st.tabs(
    ["📊 稼働状況", "🔍 プレビュー・実行", "⚙️ 設定", "🩺 診断"])

# ---- 稼働状況 ----
with tab_status:
    log = apstate.read_latest_log()
    if not log:
        st.info("まだ実行履歴がありません。GitHub Actionsの初回実行後にここへサマリが表示されます。")
    else:
        st.subheader("最新実行サマリ")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("モード", log.get("mode", "-"))
        m2.metric("対象", log.get("targets", 0))
        m3.metric("反映/予定", (log.get("patched", 0) or 0) + (log.get("would_patch", 0) or 0))
        m4.metric("エラー", log.get("errors", 0))
        with st.expander("詳細（商品別）"):
            st.json(log.get("results", []))
    try:
        _s = apstate.State()
        st.caption(f"状態DB: 商品 {_s.summary()['items']}件 記録済み")
        _s.close()
    except Exception:
        pass

# ---- プレビュー・実行 ----
with tab_preview:
    st.subheader("1商品プレビュー")
    st.caption("商品管理番号を入れると、生成ブロックと合成結果を確認できます（この操作では反映されません）。")
    mn = st.text_input("商品管理番号", key="ap_prev_mn", placeholder="例: edin0033")
    if st.button("プレビュー生成", type="primary", disabled=not mn.strip()):
        prev_cfg = copy.deepcopy(cfg)
        prev_cfg["dry_run"] = True
        prev_cfg["enabled"] = False
        stt = apstate.State()
        try:
            with st.spinner("RMSから取得して合成中..."):
                r = runner.process_item(mn.strip(), prev_cfg, stt)
        finally:
            stt.close()
        st.session_state["ap_prev_result"] = r

    r = st.session_state.get("ap_prev_result")
    if r:
        if r.get("error"):
            st.error(r["error"])
        else:
            i1, i2, i3 = st.columns(3)
            i1.metric("現在のバイト数", f"{r['bytes_before']:,}")
            i2.metric("合成後バイト数", f"{r['bytes_after']:,}",
                      delta=r["bytes_after"] - r["bytes_before"])
            i3.metric("採用ブロック", "、".join(r["included"]) or "なし")
            if r["dropped"]:
                st.warning(f"バイト上限等で間引き: {'、'.join(map(str, r['dropped']))}")
            if r.get("notes"):
                st.info(f"注記: {r['notes']}")
            st.markdown("**合成後の表示イメージ**（実際の楽天スマホページとは多少異なります）")
            st.components.v1.html(
                f'<div style="background:#fff;padding:8px;font-family:sans-serif">'
                f'{r.get("preview", "")}</div>', height=500, scrolling=True)
            with st.expander("合成後のHTMLソース"):
                st.code(r.get("preview", ""), language="html")

            st.divider()
            st.subheader("この商品への操作（本番反映）")
            st.caption("楽天の実ページを書き換えます。テスト商品でのみ使ってください。")
            agree = st.checkbox("実ページが書き換わることを理解した", key="ap_agree")
            b1, b2 = st.columns(2)
            if b1.button("⬆️ この商品に反映する", disabled=not agree):
                stt = apstate.State()
                try:
                    rr = runner.process_item(mn.strip(), cfg, stt, force=True)
                finally:
                    stt.close()
                if rr.get("error"):
                    st.error(rr["error"])
                elif rr["action"] == "patched":
                    st.success(f"反映しました（{'、'.join(rr['included'])}）。楽天ページで確認してください。")
                else:
                    st.info(f"変更なし（action={rr['action']}）")
            if b2.button("🧹 この商品から全ブロック撤去", disabled=not agree):
                stt = apstate.State()
                try:
                    rr = runner.process_item(mn.strip(), cfg, stt,
                                             remove_all=True, force=True)
                finally:
                    stt.close()
                if rr.get("error"):
                    st.error(rr["error"])
                elif rr["action"] == "patched":
                    st.success("撤去しました。自社作成部分のみに戻っています。")
                else:
                    st.info("撤去対象のブロックはありませんでした。")

    st.divider()
    st.subheader("allowlist一括dry-run")
    st.caption(f"対象: {', '.join(cfg.get('allowlist') or []) or '（未設定）'}")
    if st.button("dry-run実行", disabled=not cfg.get("allowlist")):
        run_cfg = copy.deepcopy(cfg)
        run_cfg["dry_run"] = True
        stt = apstate.State()
        try:
            with st.spinner("実行中..."):
                summary = runner.run(run_cfg, stt)
        finally:
            stt.close()
        st.json(summary)

# ---- 設定 ----
with tab_config:
    st.subheader("基本設定")
    new_cfg = copy.deepcopy(cfg)
    col1, col2 = st.columns(2)
    with col1:
        new_cfg["enabled"] = st.toggle(
            "全体スイッチ（OFFの間はバッチは一切反映しない）", value=bool(cfg.get("enabled")))
        new_cfg["dry_run"] = st.toggle(
            "dry-runモード（ONの間は差分レポートのみ）", value=bool(cfg.get("dry_run", True)))
        allow_text = st.text_area(
            "対象商品allowlist（1行1管理番号。空にすると何も処理しない）",
            value="\n".join(cfg.get("allowlist") or []), height=120)
        new_cfg["allowlist"] = [x.strip() for x in allow_text.splitlines() if x.strip()]
    with col2:
        new_cfg["byte_limit"] = st.number_input(
            "バイト上限", value=int(cfg.get("byte_limit", 10240)), step=256)
        new_cfg["byte_reserve"] = st.number_input(
            "温存バイト数（セール用バナー等のために空けておく）",
            value=int(cfg.get("byte_reserve", 250)), step=50)
        new_cfg["shop_id"] = st.text_input("店舗ID（ショップ内検索リンク用）",
                                           value=str(cfg.get("shop_id", "")))

    st.subheader("システム別設定")
    sys_cfg = new_cfg["systems"]
    s1, s2 = st.columns(2)
    with s1:
        st.markdown("**パンくずリスト**")
        sys_cfg["breadcrumb"]["enabled"] = st.toggle(
            "パンくずを表示", value=bool(cfg["systems"]["breadcrumb"].get("enabled")))
        sys_cfg["breadcrumb"]["link"] = st.radio(
            "リンク先", ["category", "search"],
            index=0 if cfg["systems"]["breadcrumb"].get("link") != "search" else 1,
            format_func=lambda x: "カテゴリページ" if x == "category" else "ショップ内検索結果ページ",
            horizontal=True)
        sys_cfg["breadcrumb"]["category_position"] = st.radio(
            "使用する表示先カテゴリ", ["first", "last"],
            index=0 if cfg["systems"]["breadcrumb"].get("category_position") == "first" else 1,
            format_func=lambda x: "1番目" if x == "first" else "最終番号", horizontal=True)
    with s2:
        st.markdown("**商品スコア**")
        sys_cfg["score"]["enabled"] = st.toggle(
            "スコアを表示", value=bool(cfg["systems"]["score"].get("enabled")))
        sys_cfg["score"]["min_average"] = st.selectbox(
            "表示条件（総合評価）", [4.5, 4.0],
            index=0 if float(cfg["systems"]["score"].get("min_average", 4.0)) >= 4.5 else 1,
            format_func=lambda x: f"{x}点以上")
        sys_cfg["score"]["min_count"] = st.number_input(
            "最低レビュー件数", value=int(cfg["systems"]["score"].get("min_count", 3)),
            min_value=1)
        st.markdown("**更新日**")
        sys_cfg["update_date"]["enabled"] = st.toggle(
            "更新日を表示", value=bool(cfg["systems"]["update_date"].get("enabled")))

    st.subheader("非表示商品（部分一致）")
    h1, h2 = st.columns(2)
    with h1:
        hn = st.text_area("商品名に含む語（1行1件）",
                          value="\n".join(cfg["hidden_items"].get("name_contains", [])),
                          height=80)
        new_cfg["hidden_items"]["name_contains"] = [x.strip() for x in hn.splitlines() if x.strip()]
    with h2:
        hm = st.text_area("商品管理番号に含む語（1行1件）",
                          value="\n".join(cfg["hidden_items"].get("manage_number_contains", [])),
                          height=80)
        new_cfg["hidden_items"]["manage_number_contains"] = [x.strip() for x in hm.splitlines() if x.strip()]

    if st.button("💾 設定を保存", type="primary"):
        apconfig.save_config_local(new_cfg)
        ok, err = apconfig.save_config_github(new_cfg)
        if ok:
            st.success("保存しました（GitHubへコミット済み。次回バッチから反映されます）")
        else:
            st.warning(f"ローカルには保存しましたが、GitHubへのコミットに失敗: {err}")
        st.rerun()

    with st.expander("現在の設定JSON"):
        st.code(json.dumps(cfg, ensure_ascii=False, indent=2), language="json")

# ---- 診断 ----
with tab_diag:
    st.subheader("RMS APIエンドポイント診断")
    st.caption("カテゴリAPI等のレスポンス形状を確認するための機能です。初回セットアップ時とエラー調査時に使います。")
    dmn = st.text_input("診断に使う商品管理番号", key="ap_diag_mn",
                        placeholder="例: edin0033")
    if st.button("診断実行", disabled=not dmn.strip()):
        with st.spinner("実行中..."):
            res = rms_items.diagnostics(dmn.strip())
        for label, r in res.items():
            st.markdown(f"**{label}** — {'✅ OK' if r['ok'] else '❌ エラー'}")
            if r["ok"]:
                with st.expander(f"{label} レスポンス"):
                    st.json(r["data"])
                if label == "category_mapping":
                    cats = rms_items.parse_item_categories(r["data"])
                    st.caption(f"パース結果（パンくず候補）: {cats or '取得できず'}")
            else:
                st.error(r["error"])
    st.divider()
    st.caption(f"レビューAPI(RAKUTEN_APP_ID): {'設定済み' if reviews.is_configured() else '未設定'}")
