# -*- coding: utf-8 -*-
"""
土日祝出荷（倉庫スタッフだけで回す日のための、一本道の案内ページ）

実際の処理（出荷する伝票を決める・B2で送り状を発行・印刷・送り状番号をNEへ）は、倉庫のPCの
AIworkspace\\weekend-shipping が行う。このページは「今の1手順」を出し、スタッフのボタンと
NEから保存したファイルを受け取って、倉庫のPCへ渡すだけ。

- 倉庫のPC → このページ（今の手順）: 共通の置き場（Supabase の関数 shared_secret_get）の
  weekend_ship_state_YYYYMMDD。PCが暗号化して追記する。手順名・件数・文言だけで、氏名・住所は無い
- このページ → 倉庫のPC（ボタン）: Googleドライブの weekend_ship/ws_cmd_*.json。PCは読むだけ
- NEで保存したファイルは、倉庫PCのChromeの保存先（パソコン版ドライブで同期する「土日祝出荷_NEの保存先」）
  から倉庫のPCが自動で拾う。このページにファイルを入れる必要は無い（2026-11-18 ユーザー指示：ドラッグさせない）
- 手順と文言の正本は weekend-shipping の src/flow.py（このページに手順を書かない）
"""
import datetime
import json
import time

import requests
import streamlit as st

st.set_page_config(page_title="土日祝出荷", page_icon="🚚", layout="centered")

from lib.auth import require_role  # noqa: E402
require_role("receiving")          # 入荷登録と同じ（倉庫スタッフ）

from streamlit_autorefresh import st_autorefresh  # noqa: E402

from lib import master_store                       # noqa: E402
from lib.invoice import drive_master               # noqa: E402

JST = datetime.timezone(datetime.timedelta(hours=9))
SUBFOLDER = "weekend_ship"
# 試験の日に保存してもらう見本（中身は空。名前だけ本番のNEのファイルに合わせる）
TEST_PDF = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n")
TEST_CSV = "お客様管理番号,送り状種類\n".encode("cp932")


def _today():
    return datetime.datetime.now(JST).date()


def _secret(k):
    return str(st.secrets.get(k, "")).strip()


@st.cache_resource(show_spinner=False)
def _folder_ids(day_s):
    root = drive_master.get_or_create_folder(SUBFOLDER, master_store.folder_id())
    files = drive_master.get_or_create_folder("files", root)
    return root, drive_master.get_or_create_folder(day_s, files)


def read_state(day_s):
    url, anon, key = _secret("SHARED_SECRETS_URL"), _secret("SHARED_SECRETS_ANON_KEY"), _secret("SHARED_SECRETS_KEY")
    if not (url and anon and key):
        return None, "共通の置き場の設定（SHARED_SECRETS_*）がありません"
    try:
        r = requests.post(url.rstrip("/") + "/rest/v1/rpc/shared_secret_get", timeout=15,
                          json={"p_name": "weekend_ship_state_" + day_s.replace("-", ""), "p_reader": "streamlit"},
                          headers={"apikey": anon, "Authorization": "Bearer " + anon})
        r.raise_for_status()
        rows = r.json() or []
        if not rows:
            return None, ""
        from cryptography.fernet import Fernet
        return json.loads(Fernet(key.encode()).decrypt(rows[0]["ciphertext"].encode()).decode("utf-8")), ""
    except Exception as exc:  # noqa: BLE001
        return None, "倉庫のPCの状態を読めません（{}）".format(type(exc).__name__)


def send(day_s, cmd, step, extra=None):
    root, _ = _folder_ids(day_s)
    body = {"day": day_s, "cmd": cmd, "step": step, "at": datetime.datetime.now(JST).isoformat(timespec="seconds"),
            "by": st.session_state.get("auth_user", "") or "page"}
    body.update(extra or {})
    name = "ws_cmd_{}_{}_{}.json".format(day_s.replace("-", ""), datetime.datetime.now(JST).strftime("%H%M%S%f"), cmd)
    drive_master.upload_bytes(json.dumps(body, ensure_ascii=False).encode("utf-8"), name, root,
                              mimetype="application/json")


day = _today().isoformat()
st_autorefresh(interval=8000, key="ws_refresh")
s, err = read_state(day)

st.title("🚚 土日祝出荷")
if err:
    st.error(err)
if not s:
    st.info("倉庫のPC（出荷ツール）がまだ動いていません。今日が土日祝の出荷日なら、8:30ごろに準備ができます。"
            "9:00を過ぎてもこの表示のままなら、Chatworkで管理者に連絡してください。")
    st.stop()

if s.get("dry_run"):
    st.warning("試験（ドライラン）です。NEへの書き込み・送り状の発行・印刷・Chatworkの投稿はしません。")

age = time.time() - float(s.get("published_ts") or 0)
# PCは2分おきに状態を出す。5分届かなければ、出荷ツール（倉庫のPC）が止まっている
stale = age > 300 and s.get("status") not in ("done", "stopped")
if stale:
    st.error("出荷ツール（倉庫のPC）が{:.0f}分間 動いていません。ボタンを押しても進みません。"
             "Chatworkで管理者に連絡してください。".format(age / 60))

stopped = s.get("status") == "stopped"
box = st.container(border=True)
with box:
    if stopped:
        st.markdown("### 🛑 ここで止めて、Chatworkを見てください")
        st.write("管理者に連絡が届いています。このページのまま、ほかの操作はしないでください。")
    else:
        st.caption("あなたの作業" if s.get("who") == "staff" else "ツールが作業中です（そのままお待ちください）")
        st.markdown("### " + s.get("title", ""))
        st.write(s.get("text", ""))
        if s.get("busy"):
            st.progress(float(s.get("percent") or 0))
        steps = s.get("steps") or []
        all_checked = True
        for i, item in enumerate(steps, 1):
            t = item["text"] if isinstance(item, dict) else str(item)
            label = "{}. {}".format(i, t)
            if s.get("button"):
                # 手順を1つずつチェックしてからでないと押せない（読まずに押すのを防ぐ）
                if not st.checkbox(label, key="chk_{}_{}".format(s["step"], i)):
                    all_checked = False
            else:
                st.markdown(label)
            if isinstance(item, dict) and item.get("link"):
                st.link_button(item.get("link_label") or "開く", item["link"])
        if s.get("test_downloads"):
            # 試験の日：本番と同じ道（Chromeの保存 → パソコン版ドライブで同期 → 倉庫のPC）を通すための見本
            c1, c2 = st.columns(2)
            c1.download_button("試験用の納品書を保存", data=TEST_PDF, file_name="sod_試験.pdf",
                               mime="application/pdf", use_container_width=True)
            c2.download_button("試験用の配送データを保存", data=TEST_CSV, file_name="yamatoCSVB2v6_試験.csv",
                               mime="text/csv", use_container_width=True)
        # 「押した」は、この回（run_id）のこの手順ごとに覚える（やり直しの回で押せなくならないように）
        pressed_key = "pressed_{}_{}".format(s.get("run_id", ""), s.get("step", ""))
        if stale:
            st.session_state.pop(pressed_key, None)
        if s.get("button"):
            if st.session_state.get(pressed_key):
                st.info("確かめています…（数十秒かかることがあります）")
            elif st.button(s["button"], type="primary", use_container_width=True, key="btn_" + s["step"],
                           disabled=not all_checked):
                send(day, "press", s["step"])
                st.session_state[pressed_key] = True
                st.rerun()
            if not all_checked:
                st.caption("上の手順に全部チェックを付けると押せます。")
    if s.get("message"):
        st.write("ℹ️ " + s["message"])

st.divider()
for p in s.get("progress", []):
    mark = {"done": "✅", "now": "👉", "todo": "・"}[p["state"]]
    st.write("{} {}".format(mark, p["title"]))
if s.get("files_folder"):
    st.caption("今日の伝票ファイル（Googleドライブ）: " + s["files_folder"])
