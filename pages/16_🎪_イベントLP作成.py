# -*- coding: utf-8 -*-
"""
イベントLP作成

スーパーセール等のイベント特集ページを作る。商品管理番号を入れると商品情報
(名前・価格・画像・レビュー)を自動取得し、可変数のセクション(商品一覧/クーポン/
ポイント変倍)からレスポンシブHTMLを生成。プレビュー確認後、楽天GOLDへ
FTPアップロードして公開する。イベント定義はNotionに保存し再編集できる。
"""
import base64
import uuid
import datetime

import streamlit as st

st.set_page_config(page_title="イベントLP作成", layout="wide")

# ウィジェット生成後にはsession_stateを書き換えられないため、
# ステータス変更(公開中など)は次回実行の冒頭で反映する
if "ev_status_pending" in st.session_state:
    st.session_state["ev_status"] = st.session_state.pop("ev_status_pending")

from lib.auth import require_role
require_role("event")  # 認証ゲート（AUTH_ENABLED=false なら素通り）
st.title("🎪 イベントLP作成")
st.caption("商品管理番号からイベント特集ページを自動生成し、楽天GOLDへ公開します。")

from lib.event import app_init, gold_ftp, html_gen, item_fetch, notion_event as N

try:
    db_ids = app_init.init_event()
except Exception as e:  # noqa: BLE001
    st.error(f"初期化に失敗しました: {e}")
    st.stop()


@st.cache_data(ttl=300, show_spinner=False)
def _load_events_cached(_nonce):
    return N.load_events(db_ids)


def _events():
    return _load_events_cached(st.session_state.get("event_nonce", 0))


def _bump_nonce():
    st.session_state["event_nonce"] = st.session_state.get("event_nonce", 0) + 1


SEC_DEFAULTS = {
    "items": {"icon": "🛒", "label": "商品一覧", "heading": "おすすめ商品"},
    "coupon": {"icon": "🎫", "label": "クーポン", "heading": "お得なクーポン"},
    "point": {"icon": "⭐", "label": "ポイント変倍", "heading": "ポイントアップ対象"},
}


def _new_section(sec_type):
    sec = {"uid": uuid.uuid4().hex[:8], "type": sec_type,
           "heading": SEC_DEFAULTS[sec_type]["heading"], "lead": ""}
    if sec_type in ("items", "point"):
        sec.update({"manage_numbers": [], "columns": 3})
    if sec_type == "point":
        sec["rate"] = 2
    if sec_type == "coupon":
        sec["coupons"] = [{"label": "", "desc": "", "getkey": ""}]
    return sec


def _load_into_editor(ev):
    """Notionのイベント1件をエディタ(session_state)へ展開する。"""
    st.session_state["event_editing_id"] = ev.get("id", "")
    st.session_state["ev_name"] = ev.get("イベント名", "")
    st.session_state["ev_catch"] = ev.get("キャッチコピー", "")
    st.session_state["ev_start"] = ev.get("期間開始", "")
    st.session_state["ev_end"] = ev.get("期間終了", "")
    st.session_state["ev_color"] = ev.get("テーマカラー", "") or html_gen.DEFAULT_THEME_COLOR
    st.session_state["ev_path"] = ev.get("GOLDパス", "")
    st.session_state["ev_status"] = ev.get("ステータス", "下書き")
    secs = []
    for s in ev.get("セクション", []):
        s = dict(s)
        s["uid"] = uuid.uuid4().hex[:8]
        secs.append(s)
    st.session_state["event_sections"] = secs
    st.session_state["event_items"] = dict(ev.get("商品スナップショット", {}))
    st.session_state["event_html"] = None


def _default_new():
    ym = datetime.date.today().strftime("%y%m")
    return {
        "id": "", "イベント名": "", "キャッチコピー": "", "期間開始": "", "期間終了": "",
        "テーマカラー": html_gen.DEFAULT_THEME_COLOR,
        "GOLDパス": f"event/{ym}_event/index.html",
        "ステータス": "下書き", "セクション": [], "商品スナップショット": {},
    }


if "event_sections" not in st.session_state:
    _load_into_editor(_default_new())


# ============================================================
# 編集対象の選択
# ============================================================
events = _events()
options = ["（新規作成）"] + [f"{i + 1}. {e['イベント名']}（{e['ステータス']}）"
                          for i, e in enumerate(events)]
col_sel, col_btn = st.columns([4, 1])
with col_sel:
    picked = st.selectbox("編集対象", options, key="event_pick")
with col_btn:
    st.write("")
    if st.button("読み込む", use_container_width=True):
        if picked == options[0]:
            _load_into_editor(_default_new())
        else:
            _load_into_editor(events[options.index(picked) - 1])
        st.rerun()

editing_id = st.session_state.get("event_editing_id", "")
if editing_id:
    st.info("既存イベントを編集中です。保存すると上書きされます。")

# ============================================================
# 基本情報
# ============================================================
st.subheader("1. 基本情報")
c1, c2 = st.columns([2, 1])
with c1:
    st.text_input("イベント名（ページタイトル）", key="ev_name",
                  placeholder="例）楽天スーパーセール 目玉商品特集")
    st.text_input("キャッチコピー", key="ev_catch",
                  placeholder="例）期間限定！人気のおもちゃが最大50%OFF")
with c2:
    st.color_picker("テーマカラー", key="ev_color")
    st.selectbox("ステータス", N.STATUS_OPTIONS, key="ev_status")
c3, c4, c5 = st.columns(3)
with c3:
    st.text_input("期間開始", key="ev_start", placeholder="2026-09-04 20:00")
with c4:
    st.text_input("期間終了", key="ev_end", placeholder="2026-09-11 01:59")
with c5:
    st.text_input("GOLDパス（アップロード先）", key="ev_path",
                  placeholder="event/2609_ss/index.html")

# ============================================================
# セクション（可変）
# ============================================================
st.subheader("2. コンテンツ（見出し）")
st.caption("見出し付きのコンテンツを必要な数だけ追加できます。順番の入れ替えも可能です。")

sections = st.session_state["event_sections"]
for i, sec in enumerate(sections):
    uid = sec["uid"]
    meta = SEC_DEFAULTS[sec["type"]]
    with st.expander(f"{meta['icon']} {i + 1}. {sec.get('heading') or meta['label']}"
                     f"　[{meta['label']}]", expanded=True):
        sec["heading"] = st.text_input("見出し", value=sec.get("heading", ""),
                                       key=f"sec_{uid}_heading")
        sec["lead"] = st.text_input("リード文（任意）", value=sec.get("lead", ""),
                                    key=f"sec_{uid}_lead")
        if sec["type"] in ("items", "point"):
            nums = st.text_area("商品管理番号（改行・カンマ区切り）",
                                value="\n".join(sec.get("manage_numbers", [])),
                                key=f"sec_{uid}_nums", height=100)
            sec["manage_numbers"] = item_fetch.parse_manage_numbers(nums)
            sec["columns"] = st.select_slider("PC表示の列数", options=[2, 3, 4],
                                              value=sec.get("columns", 3),
                                              key=f"sec_{uid}_cols")
        if sec["type"] == "point":
            sec["rate"] = st.number_input("ポイント倍率", min_value=2, max_value=20,
                                          value=int(sec.get("rate", 2)),
                                          key=f"sec_{uid}_rate")
            st.caption("※ RMSへのポイント変倍の自動設定はPhase 2で対応予定。現状はページ表示のみ。")
        if sec["type"] == "coupon":
            st.caption("※ クーポンURLの getkey（getCoupon?getkey=◯◯ の◯◯部分）を貼り付けてください。"
                       "APIによる自動発行はPhase 2で対応予定。")
            for j, cp in enumerate(sec.get("coupons", [])):
                cc1, cc2, cc3, cc4 = st.columns([2, 3, 3, 1])
                cp["label"] = cc1.text_input("表示（例 500円OFF）", value=cp.get("label", ""),
                                             key=f"sec_{uid}_cp{j}_label")
                cp["desc"] = cc2.text_input("説明（任意）", value=cp.get("desc", ""),
                                            key=f"sec_{uid}_cp{j}_desc")
                cp["getkey"] = cc3.text_input("getkey", value=cp.get("getkey", ""),
                                              key=f"sec_{uid}_cp{j}_key")
                cc4.write("")
                if cc4.button("🗑", key=f"sec_{uid}_cp{j}_del", help="このクーポン枠を削除"):
                    sec["coupons"].pop(j)
                    st.rerun()
            if st.button("＋ クーポン枠を追加", key=f"sec_{uid}_cpadd"):
                sec["coupons"].append({"label": "", "desc": "", "getkey": ""})
                st.rerun()
        # 並べ替え・削除
        b1, b2, b3, _sp = st.columns([1, 1, 1, 5])
        if b1.button("⬆ 上へ", key=f"sec_{uid}_up", disabled=(i == 0)):
            sections[i - 1], sections[i] = sections[i], sections[i - 1]
            st.rerun()
        if b2.button("⬇ 下へ", key=f"sec_{uid}_down", disabled=(i == len(sections) - 1)):
            sections[i + 1], sections[i] = sections[i], sections[i + 1]
            st.rerun()
        if b3.button("🗑 削除", key=f"sec_{uid}_del"):
            sections.pop(i)
            st.rerun()

a1, a2, a3, _sp = st.columns([1.2, 1.2, 1.4, 4])
if a1.button("＋ 商品一覧"):
    sections.append(_new_section("items"))
    st.rerun()
if a2.button("＋ クーポン"):
    sections.append(_new_section("coupon"))
    st.rerun()
if a3.button("＋ ポイント変倍"):
    sections.append(_new_section("point"))
    st.rerun()


def _current_event():
    """エディタの現在値からイベントdictを組み立てる(セクションのuidは除く)。"""
    secs = []
    for s in st.session_state["event_sections"]:
        s = {k: v for k, v in s.items() if k != "uid"}
        secs.append(s)
    return {
        "id": st.session_state.get("event_editing_id", ""),
        "イベント名": st.session_state.get("ev_name", ""),
        "キャッチコピー": st.session_state.get("ev_catch", ""),
        "期間開始": st.session_state.get("ev_start", ""),
        "期間終了": st.session_state.get("ev_end", ""),
        "テーマカラー": st.session_state.get("ev_color", ""),
        "GOLDパス": st.session_state.get("ev_path", ""),
        "ステータス": st.session_state.get("ev_status", "下書き"),
        "セクション": secs,
        "商品スナップショット": st.session_state.get("event_items", {}),
    }


def _all_manage_numbers(ev):
    out = []
    for s in ev["セクション"]:
        for mn in s.get("manage_numbers", []):
            if mn not in out:
                out.append(mn)
    return out


# ============================================================
# 生成・プレビュー
# ============================================================
st.subheader("3. HTML生成・プレビュー")
if st.button("🔄 商品情報を取得してHTML生成", type="primary"):
    ev = _current_event()
    if not ev["イベント名"]:
        st.error("イベント名を入力してください。")
    elif not ev["セクション"]:
        st.error("コンテンツを1つ以上追加してください。")
    else:
        numbers = _all_manage_numbers(ev)
        with st.spinner(f"商品情報を取得中...（{len(numbers)}件）"):
            items, errors, warnings = item_fetch.fetch_items(numbers)
        st.session_state["event_items"] = items
        st.session_state["event_fetch_errors"] = errors
        st.session_state["event_fetch_warnings"] = warnings
        ev["商品スナップショット"] = items
        st.session_state["event_html"] = html_gen.render_lp(ev, items)

for w in st.session_state.get("event_fetch_warnings", []):
    st.warning(w)
errors = st.session_state.get("event_fetch_errors", {})
if errors:
    st.error("取得できなかった商品:\n" + "\n".join(f"- {k}: {v}" for k, v in errors.items()))

items = st.session_state.get("event_items", {})
if items:
    import pandas as pd
    df = pd.DataFrame([{
        "管理番号": v["manage_number"], "商品名": v["name"], "価格": v.get("price"),
        "レビュー": f"{v.get('review_average', 0)}（{v.get('review_count', 0)}件）",
        "画像": "○" if v.get("image_url") else "×",
    } for v in items.values()])
    st.dataframe(df, use_container_width=True, hide_index=True)

html = st.session_state.get("event_html")
if html:
    data_uri = "data:text/html;base64," + base64.b64encode(html.encode("utf-8")).decode()
    tab_pc, tab_sp, tab_src = st.tabs(["🖥 PCプレビュー", "📱 スマホプレビュー", "HTMLソース"])
    with tab_pc:
        st.iframe(data_uri, height=900)
    with tab_sp:
        st.iframe(data_uri, width=375, height=800)
    with tab_src:
        st.code(html[:20000], language="html")
    st.download_button("⬇ HTMLをダウンロード", data=html.encode("utf-8"),
                       file_name="index.html", mime="text/html")

# ============================================================
# 保存・公開
# ============================================================
st.subheader("4. 保存・公開")
s1, s2 = st.columns(2)

with s1:
    if st.button("💾 Notionに保存", use_container_width=True):
        ev = _current_event()
        if not ev["イベント名"]:
            st.error("イベント名を入力してください。")
        else:
            if st.session_state.get("event_html") and ev["ステータス"] == "下書き":
                ev["ステータス"] = "生成済"
            try:
                page_id = N.upsert_event(db_ids, ev)
                st.session_state["event_editing_id"] = page_id
                _bump_nonce()
                st.success("保存しました。")
            except Exception as e:  # noqa: BLE001
                st.error(f"保存に失敗しました: {e}")

with s2:
    if html:
        ev = _current_event()
        st.download_button(
            "📦 アップ用パッケージをダウンロード", type="primary", use_container_width=True,
            data=gold_ftp.build_upload_package(ev["GOLDパス"], html),
            file_name="gold_upload.zip", mime="application/zip")
        st.caption("ダウンロードしたzipをローカルPCの `tools\\gold_upload.bat` に"
                   "ドラッグ&ドロップするとGOLDへアップされます。"
                   f"公開URL: {gold_ftp.public_url(ev['GOLDパス'])}")
    else:
        st.info("HTMLを生成すると、GOLDアップ用パッケージをダウンロードできます。")
    with st.expander("クラウドから直接FTP（Streamlit Cloudは遮断のため通常は不可）"):
        if not gold_ftp.is_configured():
            st.info("GOLD FTPのSecrets（GOLD_FTP_USER/GOLD_FTP_PASS）が未設定です。")
        else:
            if st.button("🔌 FTP接続テスト", use_container_width=True):
                try:
                    names = gold_ftp.test_connection()
                    st.success(f"接続OK。ルート直下: {', '.join(names[:10]) or '(空)'}")
                except gold_ftp.GoldFTPError as e:
                    st.error(str(e))
            if st.button("🚀 楽天GOLDへアップロード", use_container_width=True,
                         disabled=not html):
                ev = _current_event()
                try:
                    result = gold_ftp.upload_html(ev["GOLDパス"], html)
                    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    ev.update({"ステータス": "公開中", "公開URL": result["url"],
                               "最終アップ日時": now})
                    page_id = N.upsert_event(db_ids, ev)
                    st.session_state["event_editing_id"] = page_id
                    st.session_state["ev_status_pending"] = "公開中"
                    _bump_nonce()
                    st.success(f"アップロード完了（{result['size']:,} bytes）")
                    st.markdown(f"**公開URL**: [{result['url']}]({result['url']})")
                    st.caption("※ GOLDは反映まで数分かかることがあります。")
                except gold_ftp.GoldFTPError as e:
                    st.error(str(e))
                except Exception as e:  # noqa: BLE001
                    st.error(f"アップロードに失敗しました: {e}")
