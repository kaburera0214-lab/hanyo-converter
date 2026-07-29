# -*- coding: utf-8 -*-
"""
入荷登録（倉庫にはじめて入荷する商品のロケーション・配送サイズ登録）

JANをスキャン → 商品マスタから商品コード・商品名を自動表示 → 資材ナンバー・
ロケーション・配送サイズをプルダウンで選ぶ → 「🚀 更新を実行」で自動更新:
  ① NE商品マスタ: ロケーションコード（資材ナンバー-ロケーション）＋項目1（配送サイズ）… NE API
  ② 配送サイズが変わって便種（メール便⇔宅配便）も変わる場合: 楽天の配送方法セット … RMS API
  ③ サイズアップで利益NGの場合: 目標利益率価格に再設定 … NE売価（NE API）＋楽天価格（RMS API）
     Yahooの価格・配送グループはCSVをDriveに自動保存（管理者が後でまとめて反映）
判定ロジックは価格改定の「梱包サイズ変更」を流用（lib/receiving/plan.py → lib/pricing/pipeline.py）。
"""
import pandas as pd
import streamlit as st

st.set_page_config(page_title="入荷登録", page_icon="📥", layout="wide")

from lib.auth import require_role
require_role("receiving")  # 認証ゲート（AUTH_ENABLED=false なら素通り）

st.title("📥 入荷登録")
import datetime as _dt
import os as _os
_build = _dt.datetime.fromtimestamp(_os.path.getmtime(__file__)).strftime("%Y-%m-%d %H:%M")
st.caption("JANをスキャン → 資材・ロケーション・配送サイズを選んで「🚀 更新を実行」。"
           "ネクストエンジンと楽天は自動更新されます（Yahooの分はDriveにCSVが残ります）。"
           f"　（app更新: {_build}）")

from lib import master_store
from lib.ne_api import client as ne_client
from lib.pricing import calc, export as ex, masters, rakuten_price
from lib.receiving import master as recv_master, plan as rp, runner, yahoo_queue as yq
from lib.yahoo_api import client as yahoo_client

product_folder = master_store.folder_id()
params = dict(calc.DEFAULT_PARAMS)  # 計算パラメータは既定値固定（現場では変更しない）

FORM_ROWS = 10  # 入力フォームの初期行数（行の追加も可能）
_FORM_COLUMNS = ["JANコード", "商品コード", "商品名", "現サイズ", "現ロケーション",
                 "資材ナンバー", "ロケーション", "配送サイズ"]


def _plan_table_html(rows):
    """チェック結果の要点を、横スクロール無し・セル内折り返しのHTML表にする。
    st.dataframe は列が多いと横スクロールになり現場が見落とすため、要点だけを固定幅で表示。
    価格・利益率などの全項目は別途「明細」expanderに残す。"""
    import html as _h
    cols = [("区分", "区分", "9%"), ("商品コード", "商品CD", "12%"),
            ("商品名", "商品名", "26%"), ("ロケーションコード", "ロケコード", "15%"),
            ("新項目1", "新サイズ", "8%"), ("配送設定", "配送設定", "14%"),
            ("利益チェック", "利益", "7%"), ("新販売価格", "新価格", "9%")]
    div_bg = {"サイズアップ": "rgba(255,150,0,.20)", "サイズダウン": "rgba(0,120,255,.16)",
              "変更なし": "rgba(128,128,128,.12)", "初回登録": "rgba(0,180,80,.20)",
              "同等": "rgba(128,128,128,.12)"}
    thead = "".join(
        f'<th style="width:{w};text-align:left;padding:6px 8px;'
        f'border-bottom:2px solid rgba(128,128,128,.45);">{_h.escape(lbl)}</th>'
        for _key, lbl, w in cols)
    trs = []
    for r in rows:
        tds = []
        for key, _lbl, _w in cols:
            v = r.get(key)
            v = "" if v is None else str(v)
            style = ("padding:6px 8px;border-bottom:1px solid rgba(128,128,128,.25);"
                     "word-break:break-word;vertical-align:top;")
            if key == "区分" and v in div_bg:
                style += f"background:{div_bg[v]};font-weight:600;"
            if key == "配送設定" and v.startswith("要修正"):
                style += "background:rgba(255,70,70,.20);font-weight:600;"
            if key == "利益チェック" and v == "×":
                style += "color:#e03131;font-weight:700;"
            tds.append(f'<td style="{style}">{_h.escape(v)}</td>')
        trs.append("<tr>" + "".join(tds) + "</tr>")
    return ('<div style="width:100%;overflow-x:hidden;">'
            '<table style="width:100%;table-layout:fixed;border-collapse:collapse;'
            'font-size:0.86rem;">'
            f'<thead><tr>{thead}</tr></thead><tbody>{"".join(trs)}</tbody></table></div>')


# ══ 管理者向け設定（現場スタッフは触らない） ══════════════════

if "pricing_settings" not in st.session_state:
    st.session_state["pricing_settings"] = masters.load_settings(product_folder)
_settings = st.session_state["pricing_settings"]

with st.expander("🔐 NE API接続（管理者用）", expanded=False):
    from lib.ne_api import usage as ne_usage
    ne_usage.render(compact=True)   # 課金監視（回数はホームでも常時表示）
    from lib.notify import chatwork as _cw
    _cwc1, _cwc2 = st.columns([2, 1])
    _cwc1.caption("課金アラートはChatwork「souko puppy land」にタスクで通知します"
                  f"（{'設定済み' if _cw.is_configured() else 'Secrets CHATWORK_API_TOKEN 未設定'}）。")
    if _cwc2.button("🔔 通知テスト", key="recv_cw_test", disabled=not _cw.is_configured()):
        ok = _cw.create_task("[info][title]NE API通知テスト[/title]"
                             "この通知が届けば設定は完了です（テスト送信）。[/info]")
        st.success("Chatworkにテストタスクを作成しました。") if ok \
            else st.error("送信に失敗しました。トークン/ルームIDを確認してください。")

    with st.expander("📝 今月の使用量に予測値をセット（計測開始前の過去分）", expanded=False):
        st.caption("使用量カウントは導入後のNE呼び出しから記録されます。導入前に実行した分を、"
                   "予測値として今月だけ手入力できます（来月からは実測のみ）。")
        _b1, _b2 = st.columns(2)
        _bc = _b1.number_input("今月の呼び出し回数（予測）", 0, 100000, 200, 10, key="recv_seed_calls")
        _bm = _b2.number_input("今月の通信量（予測・MB）", 0.0, 100000.0, 50.0, 1.0, key="recv_seed_mb")
        if st.button("この予測値を今月にセット", key="recv_seed_btn"):
            ne_usage.set_baseline(int(_bc), int(_bm * 1024 * 1024))
            st.success(f"今月を 呼び出し{int(_bc):,}回・{_bm:.0f}MB で初期化しました。"
                       "以降の実測はこれに加算されます。")
            st.rerun()
    if not ne_client.is_configured():
        st.error("Secrets に NE_CLIENT_ID / NE_CLIENT_SECRET が未設定です。"
                 "設定するまでNEの自動更新は実行できません。")
    else:
        # 設定確認: いまアプリが使っている client_id と redirect_uri（本番切替の確認用）
        _cid = str(st.secrets.get("NE_CLIENT_ID", "")).strip()
        _cid_mask = (f"{_cid[:4]}…{_cid[-4:]}（{len(_cid)}文字）"
                     if len(_cid) > 8 else _cid)
        st.caption(f"現在のNE_CLIENT_ID: **{_cid_mask}** ／ "
                   f"NE_REDIRECT_URI: `{ne_client.redirect_uri() or '(未設定)'}`。"
                   "テスト環境の旧IDは `1D78…cdfz`。本番の値に変わっているか確認してください。")
        _tok = ne_client.token_status()
        if _tok:
            st.success(f"認可済み（トークン保存: {_tok.get('saved_at', '不明')}）。"
                       "API呼び出しのたびに自動で更新されるため、通常は再認可不要です。")
        else:
            st.warning("未認可です。下のボタンからNEにログインして認可してください。")
        c1, c2 = st.columns(2)
        try:
            c1.link_button("🔑 NEにログインして認可する", ne_client.auth_url(),
                           use_container_width=True)
        except ne_client.NENotConfigured:
            c1.caption("Secrets に NE_REDIRECT_URI が未設定のため、下の手貼り付け方式を使ってください。")
        if c2.button("📶 接続テスト（商品マスタを1件検索）", use_container_width=True,
                     key="recv_ne_test"):
            try:
                res = ne_client.call("api_v1_master_goods/search",
                                     {"fields": "goods_id", "limit": "1"})
                ne_usage.flush()   # この1回ぶんを即カウントへ反映
                st.success(f"接続OK（商品マスタ {res.get('count', '?')}件）。"
                           "使用量カウントに+1されます（ホームで確認）。")
            except Exception as e:  # noqa: BLE001
                st.error(f"接続に失敗しました: {e}")

        st.caption("**商品コードの存在確認**: 更新が「新規登録時必須です」で失敗する場合、"
                   "その商品コードがNEに（同じ表記で）登録されているかを確認します。")
        d1, d2 = st.columns([2, 1])
        _diag = d1.text_input("商品コード（例: kawa3935）", key="recv_ne_diag_code",
                              label_visibility="collapsed", placeholder="商品コード")
        if d2.button("🔎 NEで探す", key="recv_ne_diag_btn", disabled=not _diag.strip()):
            _code = _diag.strip()

            def _ne_search(params):
                params = {**params, "fields": "goods_id,goods_name"}
                return ne_client.call("api_v1_master_goods/search", params).get("data") or []

            # ① 完全一致（入力どおり／大文字／小文字）
            hit = None
            _err = False
            for _label, _val in [("入力どおり", _code), ("大文字", _code.upper()),
                                 ("小文字", _code.lower())]:
                try:
                    _rows = _ne_search({"goods_id-eq": _val, "limit": "1"})
                except Exception as e:  # noqa: BLE001
                    st.error(f"検索に失敗しました: {e}")
                    _err = True
                    break
                if _rows:
                    hit = (_label, _rows[0])
                    break
            # ② 完全一致でダメなら部分一致（-like）でNEの実際のコードを探す
            like_rows = None
            if hit is None and not _err:
                try:
                    like_rows = _ne_search({"goods_id-like": _code, "limit": "20"})
                except Exception as e:  # noqa: BLE001
                    st.error(f"部分一致検索に失敗しました: {e}")
                    _err = True

            if hit:
                st.success(f"NEに存在します（{hit[0]}の完全一致）→ 商品コード"
                           f"「**{hit[1].get('goods_id')}**」／{hit[1].get('goods_name', '')}。"
                           "この表記で自動更新できます。")
            elif like_rows:
                _exact = [r for r in like_rows
                          if str(r.get("goods_id", "")).strip().lower() == _code.lower()]
                if _exact:
                    st.success(f"部分一致で見つかりました → NEでの正確な商品コードは"
                               f"「**{_exact[0].get('goods_id')}**」。"
                               "更新時はこの表記を自動で使うので、そのまま実行できます。")
                else:
                    st.warning(f"「{_code}」に部分一致する商品コードはありますが、"
                               "完全一致はありません。下の一覧で実際のコードを確認してください。")
                st.caption("部分一致した商品コード（NEの実際の表記）:")
                st.dataframe(pd.DataFrame(like_rows), use_container_width=True, hide_index=True)
            elif not _err:
                st.error(f"「{_code}」はNEのAPIで見つかりません（完全一致・部分一致とも）。"
                         "接続先のNEアカウントが商品管理画面と同じか確認してください。"
                         "下はNEのサンプルです。")
                try:
                    _samples = _ne_search({"limit": "8"})
                    if _samples:
                        st.dataframe(pd.DataFrame(_samples), use_container_width=True,
                                     hide_index=True)
                except Exception:  # noqa: BLE001
                    pass
        st.caption("⚠️ **本番環境の資格情報を使ってください**。検索で自社の商品が出ず"
                   "「hanasakuスプーン＆フォーク（kira0096）」等のサンプルしか出ない場合は、"
                   "NEアプリの**テスト環境**に接続しています。NEデベロッパー画面でアプリを"
                   "審査→「販売開始」し、**本番環境のclient_id/client_secret**を取得して"
                   "Secrets（NE_CLIENT_ID / NE_CLIENT_SECRET）を差し替え、再認可してください。")
        st.caption("**フォールバック（手貼り付け）**: コールバックページに表示された uid / state を"
                   "貼り付けてトークンを取得します（uidは短命なのですぐに実行してください）。")
        f1, f2, f3 = st.columns([2, 2, 1])
        _uid = f1.text_input("uid", key="recv_ne_uid")
        _state = f2.text_input("state", key="recv_ne_state")
        if f3.button("トークン取得", key="recv_ne_exchange",
                     disabled=not (_uid.strip() and _state.strip())):
            try:
                ne_client.exchange(_uid.strip(), _state.strip())
                st.success("認可が完了しました（トークンをDriveに保存）。")
                st.rerun()
            except Exception as e:  # noqa: BLE001
                st.error(f"トークン取得に失敗しました: {e}")

with st.expander("🔐 Yahoo API接続（管理者用）", expanded=False):
    if not yahoo_client.is_configured():
        st.info("Secrets に YAHOO_CLIENT_ID / YAHOO_CLIENT_SECRET / YAHOO_SELLER_ID / "
                "YAHOO_REDIRECT_URI を設定すると、Yahooの価格も自動更新（updateItems＋反映予約）"
                "できます。未設定の間は価格は下の『Yahoo反映待ちキュー』でCSV運用します。")
    else:
        _yt = yahoo_client.token_status()
        _use_test = yahoo_client._secret("YAHOO_USE_TEST").lower() in ("true", "1", "yes")
        if _use_test:
            st.warning("現在【テスト環境】に接続する設定です（YAHOO_USE_TEST=true）。"
                       "本番反映するには YAHOO_USE_TEST を外してください。")
        if _yt:
            st.success(f"認可済み（トークン保存: {_yt.get('saved_at', '不明')}）。"
                       "アクセストークンは自動更新されます。")
        else:
            st.warning("未認可です。店舗オーナーのYahoo IDでログインして認可してください。")
        yc1, yc2 = st.columns(2)
        try:
            yc1.link_button("🔑 Yahooにログインして認可する", yahoo_client.authorize_url(),
                            use_container_width=True)
        except yahoo_client.YahooNotConfigured:
            yc1.caption("YAHOO_REDIRECT_URI が未設定です。")
        if yc2.button("📶 接続テスト", use_container_width=True, key="recv_yahoo_test"):
            try:
                from lib.yahoo_api import items as _yi
                st.success(f"接続OK（アクセストークン {_yi.test_connection()}）。"
                           f"seller_id={yahoo_client.seller_id() or '未設定'} / "
                           f"{'テスト環境' if _use_test else '本番環境'}")
            except Exception as e:  # noqa: BLE001
                st.error(f"接続に失敗しました: {e}")
        st.caption("公開鍵は店舗（ストアクリエイターPro）に登録済みなら共用で問題なく、"
                   "リフレッシュトークンは28日有効です。切れたら再認可してください。")

with st.expander("🟡 Yahoo反映待ちキュー（管理者がまとめてアップ）", expanded=False):
    st.caption("入荷登録で価格・配送グループが変わった商品が貯まります。"
               "**下の一括CSVを1回ダウンロード → ストアクリエイターProへアップ → 「アップ済み」**で"
               "キューを空にします（内容はDrive「Yahoo反映済み」へ自動アーカイブ）。"
               "YahooのAPI自動反映（審査通過後）が入れば、このキューは不要になります。")
    _yp = yq.load_prices(product_folder)
    _yd = yq.load_delivery(product_folder)
    yc1, yc2 = st.columns(2)
    with yc1:
        st.markdown(f"**価格の反映待ち: {len(_yp)}件**")
        if len(_yp):
            st.dataframe(_yp, use_container_width=True, hide_index=True, height=180)
            st.download_button("⬇️ Yahoo一括アップ用（価格 code,price）",
                               yq.upload_csv_bytes(_yp, "price"), "yahoo_data.csv",
                               "text/csv", key="yq_dl_price", use_container_width=True)
            if st.checkbox("価格をYahooにアップ済みにする", key="yq_price_confirm"):
                if st.button("✅ 価格キューを空にする", key="yq_price_clear"):
                    n = yq.clear_prices(product_folder)
                    st.success(f"{n}件をアーカイブしてキューを空にしました。")
                    st.rerun()
        else:
            st.caption("なし")
    with yc2:
        st.markdown(f"**配送グループの反映待ち: {len(_yd)}件**")
        if len(_yd):
            st.dataframe(_yd, use_container_width=True, hide_index=True, height=180)
            st.download_button("⬇️ Yahoo一括アップ用（配送 code,グループ番号）",
                               yq.upload_csv_bytes(_yd, "配送グループ管理番号"),
                               "yahoo_delivery.csv", "text/csv",
                               key="yq_dl_dv", use_container_width=True)
            if st.checkbox("配送グループをYahooにアップ済みにする", key="yq_dv_confirm"):
                if st.button("✅ 配送キューを空にする", key="yq_dv_clear"):
                    n = yq.clear_delivery(product_folder)
                    st.success(f"{n}件をアーカイブしてキューを空にしました。")
                    st.rerun()
        else:
            st.caption("なし")

with st.expander("⚙️ 楽天 配送方法セット管理番号（便種変更の自動修正に必要）", expanded=False):
    st.caption("番号はRMS「店舗設定→配送方法セット」の一覧で確認できます。"
               "下の🔧調査ツールで既存商品を見て確かめるのも確実です"
               "（例: gais0020のメール便SKUはshippingMethodGroup=\"2\"）。")
    s1, s2, s3 = st.columns([1, 1, 1])
    g_tak = s1.text_input("「宅配便のみ」の管理番号",
                          value=str(_settings.get("rakuten_group_takuhai", "")),
                          key="recv_grp_tak")
    g_mail = s2.text_input("「メール便」の管理番号",
                           value=str(_settings.get("rakuten_group_mail", "")),
                           key="recv_grp_mail")
    if s3.button("💾 番号を保存", key="recv_grp_save",
                 disabled=not (g_tak.strip() and g_mail.strip())):
        _settings["rakuten_group_takuhai"] = g_tak.strip()
        _settings["rakuten_group_mail"] = g_mail.strip()
        try:
            masters.save_settings(_settings, product_folder)
            st.success("保存しました（次回から自動入力されます）。")
        except Exception as e:  # noqa: BLE001
            st.warning(f"Drive保存に失敗（この画面では有効）: {e}")

with st.expander("🔧 楽天API調査（配送・価格フィールドの下調べ）", expanded=False):
    st.caption("商品管理番号を1つ入れて実行すると、その商品の配送関連フィールドと"
               "SKUの生データを表示します（表示価格などAPIフィールドの実物確認用）。")
    probe_mn = st.text_input("商品管理番号（例: gais0020）", key="recv_probe_mn")
    if st.button("🔍 調査する", key="recv_probe_btn",
                 disabled=not (probe_mn.strip() and rakuten_price.is_configured())):
        try:
            st.session_state["recv_probe_result"] = rakuten_price.probe_item(probe_mn.strip())
        except Exception as e:  # noqa: BLE001
            st.session_state.pop("recv_probe_result", None)
            st.error(f"取得失敗: {e}")
    _probe = st.session_state.get("recv_probe_result")
    if _probe:
        item = _probe.get("item", _probe)
        st.write("商品フィールド一覧:", sorted(item.keys()))

        def _find_keys(obj, path=""):
            hits = {}
            if isinstance(obj, dict):
                for k, v in obj.items():
                    p = f"{path}.{k}" if path else str(k)
                    if any(s in str(k).lower() for s in ("ship", "delivery", "postage", "price")):
                        hits[p] = v
                    hits.update(_find_keys(v, p))
            elif isinstance(obj, list):
                for i, v in enumerate(obj[:3]):
                    hits.update(_find_keys(v, f"{path}[{i}]"))
            return hits

        st.write("配送・価格関連フィールド（全階層を検索）:")
        st.json(_find_keys(item) or {"(該当なし)": "SKUの生データで確認してください"})
        variants = item.get("variants") or {}
        if isinstance(variants, dict) and variants:
            first_key = next(iter(variants))
            st.write(f"SKU 1件分の生データ（SKU管理番号: {first_key}）:")
            st.json(variants[first_key])

with st.expander("📚 NE商品マスタ（全機能共通・実行時に最新を自動取得）", expanded=False):
    st.caption("汎用マスタ変換・価格改定と同じ商品マスタ（Driveの最新版）を使います。"
               "この画面で使う列は **商品コード・JANコード・商品名・原価・項目1・ロケーションコード**。"
               "手動アップは `master_…`、API自動取得は `master_auto_…` の名前で保存され、"
               "**日付・版が新しい方**が自動で使われます（列の順序・数は問いません）。")
    _f = master_store.latest_file()
    if _f:
        st.success(f"Driveの最新版: {_f['name']}（更新 {str(_f.get('modifiedTime', ''))[:10]}）")
    else:
        st.info("Driveに商品マスタ（master_*）がありません。下からアップロードしてください。")

    st.markdown("**🔄 NEマスタをAPIで取得（最低限カラム・手動アップの代替）**")
    st.caption("週次自動更新が間に合わないときに、今すぐ最新をNEから取得してDriveに保存します"
               "（`master_auto_…`）。全カラムが必要なときは従来どおりNEからDLしてアップしてください。")
    if st.button("🔄 今すぐNEマスタをAPIで取得", key="recv_master_sync",
                 disabled=not ne_client.is_configured()):
        from lib.ne_api import master_sync
        bar = st.progress(0.0, text="NEから取得中…")
        try:
            df_auto, jan_ok = master_sync.fetch_master(
                on_progress=lambda d, t: bar.progress(
                    min(d / max(t, 1), 1.0), text=f"NEから取得中… {d:,}/{t:,}件"))
            bar.empty()
            _name = master_sync.save_master_auto(df_auto, product_folder)
            ne_usage.flush()   # 取得ぶんのAPI回数を即座にカウンタへ反映
            st.success(f"取得しました: **{_name}**（{len(df_auto):,}件）。次回実行から使われます。")
            st.dataframe(df_auto.head(5), use_container_width=True, hide_index=True)
            if not jan_ok:
                st.warning("JANコード列が空でした。フィールド名（goods_jan_code）を確認します。")
            st.session_state.pop("_master_store", None)   # 次回load_masterで読み直す
        except Exception as e:  # noqa: BLE001
            bar.empty()
            st.error(f"NEマスタの取得に失敗しました: {e}")

    st.divider()
    if master_store.upload_widget("recv_master_up"):
        st.rerun()


# ══ マスタ読み込み・プルダウン選択肢 ══════════════════════════

with st.spinner("商品マスタの最新版を確認中…"):
    ne_df, master_meta = master_store.load_master()
if ne_df is None:
    st.error(master_meta)
    st.stop()
st.caption(f"使用マスタ: {master_meta}")

_missing = [c for c in ("JANコード", "商品名", "原価", "項目1") if c not in ne_df.columns]
if _missing:
    st.warning(f"マスタに次の列がありません: {'／'.join(_missing)}。"
               "NEから**全カラム**でDLしてアップし直してください。")

jan_map, code_info = master_store.memo("pricing_lookup",
                                       lambda: masters.build_lookup(ne_df))

# 商品コード → 現在のロケーションコード（「現ロケ」表示用。プルダウンの選択肢には使わない）
if "ロケーションコード" in ne_df.columns:
    def _build_loc_map():
        codes = ne_df["商品コード"].map(masters.norm_key).tolist()
        locs = ne_df["ロケーションコード"].astype(str).tolist()
        return {c.lower(): ("" if l.strip() in ("", "nan") else l.strip())
                for c, l in zip(codes, locs) if c and c != "nan"}
    loc_map = master_store.memo("recv_loc_map", _build_loc_map)
else:
    loc_map = {}

# ── 資材ナンバー・ロケーションのプルダウン選択肢＝入荷登録マスタ（Drive・画面で編集） ──
# 誤登録が混ざるNEの既存値ではなく、この画面で管理する専用マスタを正本にする。
if "recv_master" not in st.session_state:
    rm = recv_master.load(product_folder)
    seeded = False
    if not rm["materials"]:
        rm["materials"] = list(recv_master.DEFAULT_MATERIALS)      # 初期19種
        seeded = True
    if not rm["locations"]:
        rm["locations"] = recv_master.load_bundled_locations()     # 同梱のロケ一覧
        seeded = True
    st.session_state["recv_master"] = rm
    st.session_state["recv_master_seeded"] = seeded

recv_m = st.session_state["recv_master"]
material_opts = recv_m["materials"]
loc_rows = recv_m["locations"]

with st.expander("🗂 資材ナンバー・ロケーションマスタ（プルダウンの選択肢をここで管理）", expanded=False):
    st.caption("入荷登録のプルダウンは、このマスタの内容で決まります。"
               "ロケーションは**3階層**（第一階層＝エリア／第二階層＝棚・列／第三階層＝段）で、"
               "作業者は上から順に選びます。**NEに登録されるのは最下層の値**"
               "（例: トイプー／TA／TA10B → `TA10B`、梱包室／CB1／空 → `CB1`）。"
               "行の追加＝最下段の空行に入力、削除＝左端のチェック→右上のゴミ箱。"
               "**編集したら「💾 保存」を押してください**（保存前でもこの画面のプルダウンには反映されます）。")
    if st.session_state.get("recv_master_seeded"):
        st.info("初期値をセットしました（資材ナンバー19種・ロケーション一覧）。"
                "内容を確認して「💾 保存」でDriveに確定してください。")
    mcol, lcol = st.columns([1, 3])
    mat_edit = mcol.data_editor(
        pd.DataFrame({"資材ナンバー": material_opts}), key="recv_mat_editor",
        num_rows="dynamic", hide_index=True, use_container_width=True,
        column_config={"資材ナンバー": st.column_config.TextColumn("資材ナンバー", required=True)})
    loc_edit = lcol.data_editor(
        recv_master.locations_to_df(loc_rows), key="recv_loc_editor",
        num_rows="dynamic", hide_index=True, use_container_width=True, height=320,
        column_config={
            "第一階層": st.column_config.TextColumn("第一階層（エリア）", required=True),
            "第二階層": st.column_config.TextColumn("第二階層（棚・列）", required=True),
            "第三階層": st.column_config.TextColumn("第三階層（段・空欄可）"),
        })
    _new_mats = recv_master._norm_list(mat_edit["資材ナンバー"].tolist())
    _new_locs = recv_master.norm_locations(
        loc_edit[recv_master.LOC_COLUMNS].itertuples(index=False, name=None))
    # 編集内容は保存前でも即プルダウンに反映（この描画内で使う変数を更新）
    st.session_state["recv_master"] = {"materials": _new_mats, "locations": _new_locs}
    material_opts, loc_rows = _new_mats, _new_locs
    _dirty = (_new_mats != recv_m["materials"] or _new_locs != recv_m["locations"]
              or st.session_state.get("recv_master_seeded"))
    if st.button("💾 保存（Driveに確定）", key="recv_master_save", type="primary",
                 disabled=not _dirty):
        try:
            recv_master.save(_new_mats, _new_locs, product_folder)
            st.session_state["recv_master_seeded"] = False
            st.success(f"保存しました（資材ナンバー {len(_new_mats)}種・"
                       f"ロケーション {len(_new_locs)}件）。")
            st.rerun()
        except Exception as e:  # noqa: BLE001
            st.error(f"Driveへの保存に失敗しました: {e}")

loc_tree = recv_master.hierarchy(loc_rows)                 # {第一階層: {第二階層: [第三階層…]}}
loc_flat = recv_master.flat_options(loc_rows)              # [(表示ラベル, コード)]
location_opts = [code for _label, code in loc_flat]        # 最下層コードの一覧
_label_of_code = {code: label for label, code in loc_flat}

if not material_opts or not location_opts:
    st.warning("資材ナンバーまたはロケーションのマスタが空です。"
               "上の🗂マスタで登録・保存してください（プルダウンが選べません）。")

# 配送サイズ（項目1）のプルダウン選択肢＝入荷登録で実際に使う7種（2026-07-22ユーザー確定）。
# 送料・資材（サイズ変更時の利益計算用）は従来どおり送料・資材マスタから引く。
RECEIVING_SIZE_OPTS = ["nekop", "60", "80", "100", "120", "140", "160"]
if "recv_cost_df" not in st.session_state:
    cost_df = None
    try:
        cost_df = masters.load_cost_master_drive(product_folder)
    except Exception as e:  # noqa: BLE001
        st.caption(f"（Driveの送料マスタ読込をスキップ: {e}）")
    if cost_df is None:
        cost_df = masters.load_cost_master_bundled()
    st.session_state["recv_cost_df"] = cost_df
cost_df = st.session_state["recv_cost_df"]
cost_table = masters.cost_lookup(cost_df)
size_opts = RECEIVING_SIZE_OPTS

# 楽天SKU対応表（価格改定と共通・📡取得時に自動構築してDrive保存）
if "pricing_sku_table" not in st.session_state:
    table = {}
    try:
        sku_df = masters.load_sku_master_drive(product_folder)
        if sku_df is not None:
            table = masters.sku_lookup(sku_df)
    except Exception:  # noqa: BLE001
        pass
    st.session_state["pricing_sku_table"] = table
sku_table = st.session_state["pricing_sku_table"]


def _save_sku_table():
    try:
        masters.save_sku_master_drive(
            masters.sku_table_to_df(st.session_state["pricing_sku_table"]), product_folder)
    except Exception:  # noqa: BLE001
        pass


# ══ 入力フォーム（JANスキャン → 自動補完 → プルダウン選択） ═══

def _cell(value):
    """セル値を文字列に正規化（NaN/None → 空文字）。"""
    s = masters.norm_key(value)
    return "" if s in ("nan", "None") else s


def _resolve_code(jan):
    """JAN（または商品コード）→ 商品コード。見つからなければ空文字。"""
    if not jan:
        return ""
    code = jan_map.get(jan, "")
    if not code and jan.lower() in code_info:
        code = code_info[jan.lower()]["商品コード"]   # JAN欄に商品コードを入れても通す
    return code


st.markdown("### ① 入荷商品の入力")

with st.expander("📄 NE現状の点検（誤登録さがし・一覧ダウンロード）", expanded=False):
    st.caption("プルダウンの選択肢は上の🗂マスタで管理します。ここはNE商品マスタに"
               "**実際に登録されているロケーションコードの点検用**です。"
               "**件数が極端に少ない資材ナンバーは誤登録の可能性大**。NE側を直して"
               "マスタを取り直すと反映されます。")
    if st.button("📄 一覧を作成", key="recv_opts_report"):
        if "ロケーションコード" in ne_df.columns:
            mat_counts, loc_counts = rp.split_location_counts(
                ne_df["ロケーションコード"].astype(str).tolist())
        else:
            mat_counts, loc_counts = {}, {}
        lines = ["入荷登録 プルダウン選択肢一覧（点検用）",
                 f"生成: {_dt.datetime.now().strftime('%Y-%m-%d %H:%M')} / 使用マスタ: {master_meta}",
                 "",
                 f"■資材ナンバー（{len(mat_counts)}種類・件数の多い順）"]
        lines += [f"{v}\t{n}件" for v, n in sorted(mat_counts.items(), key=lambda x: -x[1])]
        lines += ["", f"■ロケーション（{len(loc_counts)}種類・件数の多い順）"]
        lines += [f"{v}\t{n}件" for v, n in sorted(loc_counts.items(), key=lambda x: -x[1])]
        lines += ["", f"■配送サイズ（送料・資材マスタのキー {len(size_opts)}種類）",
                  "、".join(size_opts)]
        st.session_state["recv_opts_txt"] = "\r\n".join(lines).encode("utf-8-sig")
    if st.session_state.get("recv_opts_txt") is not None:
        st.download_button("⬇️ pulldown_options.txt をダウンロード",
                           st.session_state["recv_opts_txt"],
                           "pulldown_options.txt", "text/plain", key="recv_opts_dl")

    st.divider()
    st.caption("**商品コード × ロケーションコードの一覧**（資材ナンバーの誤登録さがし用）。"
               "資材ナンバー順に並ぶので、同じ棚なのに資材ナンバーが違う商品を見つけやすくなります。")
    if st.button("📄 商品×ロケーション一覧を作成", key="recv_loc_report"):
        rows = []  # (資材ナンバー, ロケーション, 商品コード, 商品名, ロケーションコード原文)
        if "ロケーションコード" in ne_df.columns:
            codes_col = ne_df["商品コード"].map(masters.norm_key).tolist()
            names_col = (ne_df["商品名"].astype(str).tolist()
                         if "商品名" in ne_df.columns else [""] * len(ne_df))
            locs_col = ne_df["ロケーションコード"].astype(str).tolist()
            for code, name, raw in zip(codes_col, names_col, locs_col):
                if not code or code == "nan":
                    continue
                loc_raw = masters.norm_key(raw)
                if not loc_raw or loc_raw == "nan":
                    continue  # ロケーション未設定は対象外（点検は登録済みのみ）
                mat, _, loc = loc_raw.partition("-")
                rows.append((mat, loc, code, name, loc_raw))
        rows.sort(key=lambda r: (r[0], r[1], r[2]))
        lines = ["入荷登録 商品コード×ロケーションコード一覧（資材ナンバーの誤登録点検用）",
                 f"生成: {_dt.datetime.now().strftime('%Y-%m-%d %H:%M')} / 使用マスタ: {master_meta}",
                 f"対象: ロケーション登録済み {len(rows)}商品（資材ナンバー→ロケーション→商品コード順）",
                 "",
                 "資材ナンバー\tロケーション\t商品コード\tロケーションコード\t商品名"]
        lines += [f"{m}\t{loc}\t{code}\t{raw}\t{name}" for m, loc, code, name, raw in rows]
        st.session_state["recv_loc_txt"] = "\r\n".join(lines).encode("utf-8-sig")
    if st.session_state.get("recv_loc_txt") is not None:
        st.download_button("⬇️ product_locations.txt をダウンロード",
                           st.session_state["recv_loc_txt"],
                           "product_locations.txt", "text/plain", key="recv_loc_dl")

tab_one, tab_bulk = st.tabs(["📱 1商品ずつ", "📋 複数商品まとめて"])

# ── 1商品ずつ（スキャン→3つのプルダウン。全体が1画面に収まる） ──
single_rows = []
with tab_one:
    st.caption("JANをスキャン（Enterで確定）→ **▼のプルダウンを3つ選ぶ** → 下の「🧮 チェック」へ。")
    jan1 = _cell(st.text_input("JAN（スキャン→Enter）", key="recv1_jan"))
    code1 = _resolve_code(jan1)
    if jan1 and not code1:
        st.error(f"⚠️ マスタに無いJANです: {jan1}")
    if code1:
        info1 = code_info[code1.lower()]
        old_size1 = _cell(info1.get("項目1", ""))
        i1, i2 = st.columns(2)
        i1.markdown(f"**{info1['商品コード']}**　{info1.get('商品名', '')}")
        i2.markdown(f"現サイズ: **{old_size1 or '（未設定）'}**　／　"
                    f"現ロケーション: **{loc_map.get(code1.lower(), '') or '（未設定）'}**")

        # ロケーションは階層で上から順に選ぶ（第三階層が無い棚は第二階層で確定）
        st.markdown("**📍 ロケーション（上から順に選択）**")
        h1, h2, h3 = st.columns(3)
        lv1 = h1.selectbox("第一階層（エリア）", list(loc_tree), index=None,
                           placeholder="▼ 選択", key="recv1_l1")
        lv2_opts = list(loc_tree.get(lv1, {})) if lv1 else []
        lv2 = h2.selectbox("第二階層（棚・列）", lv2_opts, index=None,
                           placeholder="▼ 選択", key="recv1_l2",
                           disabled=not lv2_opts)
        lv3_opts = loc_tree.get(lv1, {}).get(lv2, []) if (lv1 and lv2) else []
        lv3 = h3.selectbox("第三階層（段）", lv3_opts, index=None,
                           placeholder="▼ 選択" if lv3_opts else "この棚は第二階層まで",
                           key="recv1_l3", disabled=not lv3_opts)
        # 第三階層があるのに未選択のときだけ未確定。無い棚は第二階層で確定。
        loc1 = lv3 if lv3_opts else (lv2 if lv2 else None)
        if lv3_opts and not lv3:
            loc1 = None

        s1, s3 = st.columns(2)
        mat1 = s1.selectbox("📂 資材ナンバー", material_opts, index=None,
                            placeholder="▼ タップして選択", key="recv1_mat")
        # 資材ナンバーが変わったら配送サイズを自動セット（60A→60・MB系→nekop・ND/STはセットなし）
        if st.session_state.get("recv1_mat_prev") != mat1:
            st.session_state["recv1_mat_prev"] = mat1
            _ds1 = rp.default_size(mat1, size_opts) if mat1 else None
            if _ds1:
                st.session_state["recv1_size"] = _ds1
        size1 = s3.selectbox("📦 配送サイズ（項目1・自動セット／変更可）", size_opts, index=None,
                             placeholder="▼ タップして選択", key="recv1_size")
        if mat1 and loc1 and size1:
            single_rows = [{"商品コード": code1, "資材ナンバー": mat1,
                            "ロケーション": loc1, "配送サイズ": size1}]
            st.success(f"ロケーションコード: **{rp.location_code(mat1, loc1)}** ／ "
                       f"サイズ: **{size1}** → 下の「🧮 チェック」へ")
        else:
            st.info("ロケーション（階層）・資材ナンバー・配送サイズをすべて選択してください。")

# ── 複数商品まとめて（表形式・横スクロール無しで全列表示） ──
bulk_rows = []
_errors = []
with tab_bulk:
    st.caption("JAN列にスキャン（Enterで確定）すると商品情報が自動表示されます。"
               "**▼が付いた列**はセルをクリックするとプルダウンが開きます（手入力不可）。")

    # 表のセルでは階層を辿れないため、階層は表の外で絞り込む（同じエリアへの入荷が多いため）
    f1, f2, f3 = st.columns([1, 1, 2])
    _ALL = "（すべて）"
    fl1 = f1.selectbox("絞り込み: 第一階層", [_ALL] + list(loc_tree), key="recv_flt_l1")
    _f2_opts = list(loc_tree.get(fl1, {})) if fl1 != _ALL else []
    fl2 = f2.selectbox("絞り込み: 第二階層", [_ALL] + _f2_opts, key="recv_flt_l2",
                       disabled=not _f2_opts)
    _filtered = [r for r in loc_rows
                 if (fl1 == _ALL or r[0] == fl1) and (fl2 == _ALL or r[1] == fl2)]
    _loc_choices = [recv_master.location_code(r) for r in _filtered] or location_opts
    f3.caption(f"▼ロケ列の選択肢: **{len(_loc_choices)}件**"
               f"{'（絞り込み中）' if _loc_choices is not location_opts else '（全件）'}。"
               "絞り込むと目的の棚を選びやすくなります。")

    if "recv_df" not in st.session_state:
        st.session_state["recv_df"] = pd.DataFrame(
            [{c: ("" if c in ("JANコード", "商品コード", "商品名", "現サイズ", "現ロケーション")
                  else None) for c in _FORM_COLUMNS} for _ in range(FORM_ROWS)])
        st.session_state["recv_nonce"] = 0

    # 絞り込みを変えても入力済みの値が消えないよう、選択済みの値は必ず候補に残す
    _already = [_cell(v) for v in st.session_state["recv_df"]["ロケーション"].tolist()]
    _loc_opts_for_editor = list(dict.fromkeys(
        _loc_choices + [v for v in _already if v and v in location_opts]))

    _nonce = st.session_state["recv_nonce"]
    # 列名は全列が横スクロール無しで収まるよう短縮（表のヘッダは改行表示ができないため）
    edited = st.data_editor(
        st.session_state["recv_df"], key=f"recv_editor_{_nonce}",
        num_rows="dynamic", hide_index=True, use_container_width=True,
        column_config={
            "JANコード": st.column_config.TextColumn("JAN", width="medium",
                                                     help="ハンディでスキャン→Enter"),
            "商品コード": st.column_config.TextColumn("商品CD", disabled=True, width="small"),
            "商品名": st.column_config.TextColumn("商品名", disabled=True, width="medium"),
            "現サイズ": st.column_config.TextColumn("現サイズ", disabled=True, width="small"),
            "現ロケーション": st.column_config.TextColumn("現ロケ", disabled=True, width="small"),
            "資材ナンバー": st.column_config.SelectboxColumn("▼資材", options=material_opts,
                                                        width="small",
                                                        help="プルダウンから選択"),
            "ロケーション": st.column_config.SelectboxColumn(
                "▼ロケ", options=_loc_opts_for_editor, width="small",
                help="プルダウンから選択（上の絞り込みで候補を減らせます）"),
            "配送サイズ": st.column_config.SelectboxColumn("▼サイズ", options=size_opts,
                                                      width="small",
                                                      help="プルダウンから選択（項目1）"),
        })

    # JANが変わった行だけマスタから補完する（変化があれば nonce+1 で再描画）
    _changed = False
    for i in edited.index:
        jan = _cell(edited.at[i, "JANコード"])
        cur_code = _cell(edited.at[i, "商品コード"])
        code = _resolve_code(jan)
        if jan and code and code != cur_code:
            info = code_info[code.lower()]
            old_size = masters.norm_key(info.get("項目1", ""))
            edited.loc[i, ["商品コード", "商品名", "現サイズ", "現ロケーション"]] = (
                info["商品コード"], info.get("商品名", ""),
                "" if old_size == "nan" else old_size,
                loc_map.get(code.lower(), ""))
            _changed = True
        elif (not jan or not code) and cur_code:   # JANを消した/変えた → 補完をクリア
            edited.loc[i, ["商品コード", "商品名", "現サイズ", "現ロケーション"]] = ("", "", "", "")
            _changed = True
        # 資材ナンバー→配送サイズを自動セット（配送サイズが空のときだけ・60A→60/MB系→nekop）
        _mat = _cell(edited.at[i, "資材ナンバー"])
        if _mat and not _cell(edited.at[i, "配送サイズ"]):
            _ds = rp.default_size(_mat, size_opts)
            if _ds:
                edited.at[i, "配送サイズ"] = _ds
                _changed = True
    st.session_state["recv_df"] = edited
    if _changed:
        st.session_state["recv_nonce"] = _nonce + 1
        st.rerun()

    # 入力チェック（JAN未解決・選択漏れ・重複）
    _jan_s = edited["JANコード"].map(_cell)
    _code_s = edited["商品コード"].map(_cell)
    _active = edited[(_jan_s != "") | (_code_s != "")]
    _bad_jan = _active[_active["商品コード"].map(_cell) == ""]
    if len(_bad_jan):
        _errors.append("マスタに無いJAN: " + "、".join(
            str(j) for j in _bad_jan["JANコード"].tolist()[:10]))
    _incomplete = _active[(_active["商品コード"].map(_cell) != "")
                          & (_active[["資材ナンバー", "ロケーション", "配送サイズ"]]
                             .isna().any(axis=1))]
    if len(_incomplete):
        _errors.append("▼資材／▼ロケ／▼サイズが未選択: " + "、".join(
            _incomplete["商品コード"].tolist()[:10]))
    _codes = [c for c in _active["商品コード"].map(_cell).tolist() if c]
    _dups = sorted({c for c in _codes if _codes.count(c) > 1})
    if _dups:
        _errors.append("同じ商品が複数行にあります: " + "、".join(_dups[:10]))
    for e in _errors:
        st.error("⚠️ " + e)

    bulk_rows = [
        {"商品コード": _cell(r["商品コード"]), "資材ナンバー": r["資材ナンバー"],
         "ロケーション": r["ロケーション"], "配送サイズ": r["配送サイズ"]}
        for _, r in _active.iterrows()
        if _cell(r["商品コード"]) and not pd.isna(r["資材ナンバー"])
        and not pd.isna(r["ロケーション"]) and not pd.isna(r["配送サイズ"])
    ] if not _errors else []

# 両タブの入力をまとめる（通常はどちらか一方だけ使う）
_both = [r["商品コード"] for r in single_rows + bulk_rows]
_dups_both = sorted({c for c in _both if _both.count(c) > 1})
if _dups_both:
    st.error("⚠️ 「1商品ずつ」と「まとめて」の両方に同じ商品があります: " + "、".join(_dups_both))
    _errors.append("タブ間の重複")
_input_rows = (single_rows + bulk_rows) if not _errors else []


# ══ ② チェック（実行プランの作成・プレビュー） ═══════════════

st.markdown("### ② チェック → ③ 更新")

if st.button(f"🧮 チェック（実行プランを作成）　対象 {len(_input_rows)}件",
             type="primary", disabled=not _input_rows, key="recv_check"):
    # サイズが変わる行は利益チェックに楽天の現在価格が必要 → ここで自動取得
    need_price = []
    for r in _input_rows:
        info = code_info.get(masters.norm_key(r["商品コード"]).lower(), {})
        old_size = masters.norm_key(info.get("項目1", ""))
        old_size = "" if old_size == "nan" else old_size
        if old_size and masters.norm_key(r["配送サイズ"]) != old_size:
            need_price.append(info["商品コード"])
    cache = st.session_state.setdefault("pricing_rk_prices", {})
    fetch_codes = [c for c in need_price if c.lower() not in cache]
    if fetch_codes and rakuten_price.is_configured():
        bar = st.progress(0.0, text="楽天から現在価格を取得中…")
        info_rk, _rk_errors, _rk_warnings = rakuten_price.fetch_for_codes(
            fetch_codes, sku_table,
            on_progress=lambda done, total: bar.progress(
                done / max(total, 1), text=f"楽天から取得中… {done}/{total}商品"))
        bar.empty()
        cache.update(rakuten_price.to_prices(info_rk))
        st.session_state["pricing_sku_table"].update(rakuten_price.to_sku_table(info_rk))
        _save_sku_table()
        for w in _rk_warnings:
            st.warning(w)
    elif fetch_codes:
        st.warning("RMSキー（RMS_SERVICE_SECRET / RMS_LICENSE_KEY）未設定のため、"
                   "サイズ変更の利益チェックができません。")
    st.session_state["recv_plan"] = rp.build_plan(
        _input_rows, code_info, cost_table, params,
        cur_prices=st.session_state.get("pricing_rk_prices", {}))
    st.session_state["recv_plan_key"] = repr(_input_rows)  # プランと入力の対応を検知する用
    st.session_state.pop("recv_result", None)
    st.session_state.pop("recv_failed", None)
    st.rerun()

plan_rows = st.session_state.get("recv_plan")
_plan_stale = (plan_rows is not None
               and st.session_state.get("recv_plan_key") != repr(_input_rows))
if plan_rows and _plan_stale:
    st.warning("入力内容がチェック時から変わっています。もう一度「🧮 チェック」を押してください。")
if plan_rows and not _plan_stale:
    # 現場が見落とさないよう、要点だけを横スクロール無し・折り返しで表示する
    st.markdown(_plan_table_html(plan_rows), unsafe_allow_html=True)
    _warn_rows = [r for r in plan_rows if str(r.get("警告", "")).strip()]
    if _warn_rows:
        def _clean_warn(w):   # page21には📡ボタンが無い（自動取得）ので文言を差し替える
            return str(w).replace(
                "→ 📡「楽天から現在価格を取得」を押してください",
                "（楽天の現在価格を自動取得できませんでした。下で価格を入力してください）")
        st.warning("⚠️ 警告があります（確認してください）:\n"
                   + "\n".join(f"- **{r['商品コード']}**: {_clean_warn(r['警告'])}"
                               for r in _warn_rows))
    with st.expander("🔎 明細（価格・利益率など全項目）", expanded=False):
        plan_df = pd.DataFrame(plan_rows)
        lead = ["商品コード", "商品名", "警告", "区分", "配送設定", "利益チェック"]
        plan_df = plan_df[lead + [c for c in plan_df.columns if c not in lead]]
        st.dataframe(plan_df, use_container_width=True, hide_index=True,
                     column_config={"新利益率": st.column_config.NumberColumn(format="percent")})

    dv_rows = rp.delivery_rows(plan_rows, sku_table)
    price_list, price_missing = rp.price_tasks(plan_rows, code_info, sku_table)
    n_first = sum(1 for r in plan_rows if r["区分"] == "初回登録")
    n_ng = sum(1 for r in plan_rows if r.get("新販売価格"))
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("NE更新（全行）", f"{len(plan_rows)}件")
    c2.metric("初回登録", f"{n_first}件")
    c3.metric("配送設定の変更", f"{len(dv_rows)}件")
    c4.metric("利益NG→価格再設定", f"{n_ng}件")

    # サイズアップなのに楽天現在価格が取れず利益チェックができていない行（更新をブロックする）
    _no_price_rows = [r for r in plan_rows
                      if r["区分"] == "サイズアップ" and r["利益チェック"] == "-"]

    blockers = []
    if dv_rows and not (str(_settings.get("rakuten_group_takuhai", "")).strip()
                        and str(_settings.get("rakuten_group_mail", "")).strip()):
        blockers.append("便種変更があります。上の⚙️で楽天の配送方法セット管理番号を設定してください。")
    if dv_rows and not rakuten_price.is_configured():
        blockers.append("便種変更がありますが、RMSキー未設定のため楽天を自動修正できません。")
    if n_ng and not rakuten_price.is_configured():
        blockers.append("価格再設定がありますが、RMSキー未設定のため楽天価格を自動更新できません。")
    if not ne_client.is_configured():
        blockers.append("NE APIが未設定です（Secrets NE_CLIENT_ID / NE_CLIENT_SECRET）。")
    if _no_price_rows:
        blockers.append("サイズアップの利益チェックができませんでした（"
                        + "、".join(r["商品コード"] for r in _no_price_rows)
                        + "）。楽天の現在価格を自動取得できない商品です。"
                        "**この商品はEC運営層に連絡してください**（楽天の登録・商品管理番号の確認が必要）。"
                        "他の商品を進める場合は、この行をフォームから外してください。")
    for b in blockers:
        st.error("🛑 " + b)
    if price_missing:
        st.warning("楽天のSKU番号が分からず価格を自動更新できない商品: "
                   + "、".join(price_missing)
                   + "（EC運営層へ連絡してください）")

    agree = st.checkbox("上記の内容で**本番データ（ネクストエンジン・楽天）を更新**することを確認しました",
                        key="recv_agree")
    if st.button("🚀 更新を実行", type="primary", key="recv_run",
                 disabled=not agree or bool(blockers)):
        import traceback as _tb
        results, failed, files = [], {}, {}
        run_name, url, err = "", "", ""
        try:
            group_of = {"宅配便": str(_settings.get("rakuten_group_takuhai", "")).strip(),
                        "メール便": str(_settings.get("rakuten_group_mail", "")).strip()}
            main_rows, ne_price_rows = rp.ne_rows_from_plan(plan_rows)
            # Yahoo価格: API設定済みならAPIで自動更新（親コード単位）、未設定なら後段でCSVキューへ
            _repriced_rows = [r for r in plan_rows if r.get("新販売価格")]
            _yahoo_api_on = yahoo_client.api_enabled()   # YAHOO_DISABLE=trueで切り分け可
            yahoo_price_map = {}
            if _repriced_rows and _yahoo_api_on:
                _ymall = [{"商品コード": r["商品コード"], "楽天販売価格": r["新販売価格"],
                           "Yahoo販売価格": r["新販売価格"]} for r in _repriced_rows]
                _yr, _ = ex.yahoo_rows(_ymall, sku_table)
                yahoo_price_map = {r["code"]: int(r["price"]) for r in _yr}
            tasks = {
                "ne_main": main_rows,
                "ne_price": ne_price_rows,
                "rakuten_delivery": [{**d, "group_id": group_of.get(d["新便種"], "")}
                                     for d in dv_rows],
                "rakuten_price": price_list,
                "yahoo_price": yahoo_price_map,
            }
            total_units = ((1 if tasks["ne_main"] else 0) + (1 if tasks["ne_price"] else 0)
                           + len(tasks["rakuten_delivery"]) + len(tasks["rakuten_price"])
                           + (1 if tasks["yahoo_price"] else 0))
            bar = st.progress(0.0, text="更新中…")
            _done = {"n": 0}

            def _on_step(message):
                _done["n"] += 1
                bar.progress(min(_done["n"] / max(total_units, 1), 1.0), text=message)

            print(f"[recv] START yahoo_api_on={_yahoo_api_on} "
                  f"ne_main={len(tasks['ne_main'])} ne_price={len(tasks['ne_price'])} "
                  f"rk_price={len(tasks['rakuten_price'])} yahoo={len(tasks['yahoo_price'])}",
                  flush=True)
            try:
                results, failed = runner.execute(tasks, on_step=_on_step)
            except Exception as e:  # noqa: BLE001
                results = [{"ステップ": "実行", "対象": "-", "状態": "失敗",
                            "メッセージ": f"実行中に想定外のエラー: {e}"}]
            print(f"[recv] runner.execute DONE results={len(results)}", flush=True)
            try:
                ne_usage.flush()
            except Exception:  # noqa: BLE001
                pass

            # 証跡（プラン・出力CSV・実行結果）をDriveの「価格改定履歴」へ保存。
            bar.progress(0.95, text="証跡CSVを生成中…")
            print("[recv] evidence_files START", flush=True)
            try:
                files = rp.evidence_files(plan_rows, dv_rows, code_info, sku_table)
                files["run_result.csv"] = ex.detail_csv(pd.DataFrame(results))
            except Exception as e:  # noqa: BLE001
                err = f"証跡CSVの生成に失敗: {e}"
            print(f"[recv] evidence_files DONE files={len(files)}", flush=True)

            # Yahoo反映待ちキューへ追記（API未設定時のフォールバック。配送グループは常にCSV）
            try:
                if _repriced_rows and not _yahoo_api_on:
                    _mall = [{"商品コード": r["商品コード"], "楽天販売価格": r["新販売価格"],
                              "Yahoo販売価格": r["新販売価格"]} for r in _repriced_rows]
                    _yrows, _ = ex.yahoo_rows(_mall, sku_table)
                    yq.append_prices([{"code": r["code"], "price": r["price"]}
                                      for r in _yrows], product_folder)
                if dv_rows:
                    _ydv = [{"code": str(d["商品管理番号"]).lower(),
                             "配送グループ管理番号":
                                 ex.YAHOO_DELIVERY_VALUE.get(d["新便種"], d["新便種"])}
                            for d in dv_rows]
                    yq.append_delivery(_ydv, product_folder)
            except Exception:  # noqa: BLE001
                pass

            if files:
                bar.progress(0.98, text="Driveに証跡を保存中…")
                print("[recv] save_run_to_drive START", flush=True)
                try:
                    run_name, run_id = masters.save_run_to_drive(
                        files, "入荷登録", product_folder)
                    url = f"https://drive.google.com/drive/folders/{run_id}"
                except Exception as e:  # noqa: BLE001
                    err = (err + " / " if err else "") + f"Drive保存失敗: {e}"
                print(f"[recv] save_run_to_drive DONE run={run_name} err={err[:80]}", flush=True)
            bar.progress(1.0, text="完了")
            bar.empty()
        except Exception:  # noqa: BLE001（想定外を必ず捕捉して画面に出す）
            err = (err + " / " if err else "") + "想定外のエラー:\n" + _tb.format_exc()
            print("[recv] EXCEPTION:\n" + _tb.format_exc(), flush=True)
            if not results:
                results = [{"ステップ": "実行", "対象": "-", "状態": "失敗",
                            "メッセージ": "想定外のエラー（下の詳細を確認）"}]

        print("[recv] recv_result SET, about to rerun", flush=True)
        st.session_state["recv_result"] = {"results": results, "run": run_name,
                                           "url": url, "err": err,
                                           "files": files, "n_dv": len(dv_rows)}
        st.session_state["recv_failed"] = failed
        st.rerun()


# ══ ③ 実行結果 ═══════════════════════════════════════════════

res = st.session_state.get("recv_result")
if res:
    print("[recv] rendering result section", flush=True)
    results = res["results"]
    rdf = pd.DataFrame(results)
    n_ok = int((rdf["状態"] == "成功").sum()) if len(rdf) else 0
    n_fail = int((rdf["状態"] == "失敗").sum()) if len(rdf) else 0
    n_skip = int((rdf["状態"] == "スキップ").sum()) if len(rdf) else 0
    if n_fail == 0:
        st.success(f"✅ 更新が完了しました（成功 {n_ok}件）")
    else:
        st.error(f"⚠️ 一部の更新に失敗しました（成功 {n_ok}／失敗 {n_fail}／スキップ {n_skip}）")
    st.dataframe(rdf, use_container_width=True, hide_index=True)

    if runner.has_auth_error(results):
        st.error("🔐 認証切れが発生しています。上の「NE API接続」または"
                 "RMSライセンスキー（Secrets）を確認して再認可・更新後、"
                 "「失敗した処理だけ再実行」を押してください。")

    failed = st.session_state.get("recv_failed") or {}
    if any(failed.values()):
        if st.button("🔁 失敗した処理だけ再実行", key="recv_retry", type="primary"):
            bar = st.progress(0.0, text="再実行中…")
            retry_results, still_failed = runner.execute(
                failed, on_step=lambda m: bar.progress(0.5, text=m))
            bar.empty()
            ok_before = [r for r in results if r["状態"] == "成功"]
            st.session_state["recv_result"]["results"] = ok_before + retry_results
            st.session_state["recv_failed"] = still_failed
            st.rerun()

    if res["err"]:
        st.warning("処理中にエラーがありました（下に詳細）:")
        st.code(str(res["err"]))
    if res["run"]:
        st.caption(f"証跡をDriveに保存しました: **{res['run']}**"
                   "（プラン・実行結果・Yahoo用CSVを含む）")
        if res["url"]:
            st.link_button("📁 証跡フォルダを開く", res["url"])

    # Yahoo: 価格はAPI設定済みなら自動反映（上の結果表に⑤で出る）。未設定/配送はキューへ。
    _has_price = any(r.get("新販売価格") for r in (st.session_state.get("recv_plan") or []))
    if _has_price and not yahoo_client.is_configured():
        st.info("🟡 Yahoo価格は下の「Yahoo反映待ちキュー」に貯まりました（API未設定のためCSV運用）。")
    if res.get("n_dv"):
        st.info("🟡 Yahoo配送グループは下の「Yahoo反映待ちキュー」に貯まりました"
                "（配送グループは安全のためCSV運用です）。")

    if st.button("🧹 フォームをクリアして次の入荷へ", key="recv_clear"):
        for k in ("recv_df", "recv_plan", "recv_result", "recv_failed", "recv_agree",
                  "recv1_jan", "recv1_mat", "recv1_loc", "recv1_size"):
            st.session_state.pop(k, None)
        st.session_state["recv_nonce"] = st.session_state.get("recv_nonce", 0) + 1
        st.rerun()
