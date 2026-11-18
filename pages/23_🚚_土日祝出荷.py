# -*- coding: utf-8 -*-
"""
土日祝出荷（倉庫スタッフだけで回す日のための、一本道の案内ページ）

実際の処理（出荷する伝票を決める・B2で送り状を発行・印刷・送り状番号をNEへ）は、倉庫のPCの
AIworkspace\\weekend-shipping が行う。このページは「今の1手順」を出し、スタッフのボタンと
NEから保存したファイルを受け取って、倉庫のPCへ渡すだけ。

- 倉庫のPC → このページ（今の手順）: 共通の置き場（Supabase の関数 shared_secret_get）の
  weekend_ship_state_YYYYMMDD。PCが暗号化して追記する。手順名・件数・文言だけで、氏名・住所は無い
- このページ → 倉庫のPC（ボタン・ファイル）: Googleドライブの weekend_ship フォルダ
  （ボタン = ws_cmd_*.json、ファイル = files/YYYYMMDD/）。PCは読むだけ
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
if age > 180 and s.get("status") not in ("done", "stopped"):
    st.error("倉庫のPCから{:.0f}分間 状態が届いていません。PCの電源・Wi-Fiを確かめ、Chatworkで管理者に連絡してください。".format(age / 60))

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
        if s.get("link"):
            st.link_button("NEを開く", s["link"])
        pressed_key = "pressed_" + s.get("step", "")
        if s.get("button"):
            if st.session_state.get(pressed_key):
                st.info("確かめています…（数十秒かかることがあります）")
            elif st.button(s["button"], type="primary", use_container_width=True, key="btn_" + s["step"]):
                send(day, "press", s["step"])
                st.session_state[pressed_key] = True
                st.rerun()
        if s.get("upload"):
            files = st.file_uploader("NEで保存したファイルをここに入れてください（納品書PDFと配送データCSV）",
                                     type=["pdf", "csv"], accept_multiple_files=True, key="up_" + s["step"])
            if files and st.button("このファイルを渡す", type="primary", use_container_width=True, key="send_files"):
                _, day_folder = _folder_ids(day)
                names = []
                for f in files:
                    mt = "application/pdf" if f.name.lower().endswith(".pdf") else "text/csv"
                    drive_master.upload_bytes(f.getvalue(), f.name, day_folder, mimetype=mt)
                    names.append(f.name)
                send(day, "uploaded", s["step"], {"files": names})
                st.success("渡しました: " + "、".join(names))
    if s.get("message"):
        st.write("ℹ️ " + s["message"])

st.divider()
for p in s.get("progress", []):
    mark = {"done": "✅", "now": "👉", "todo": "・"}[p["state"]]
    st.write("{} {}".format(mark, p["title"]))
if s.get("files_folder"):
    st.caption("今日の伝票ファイル（Googleドライブ）: " + s["files_folder"])
