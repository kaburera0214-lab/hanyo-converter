# -*- coding: utf-8 -*-
"""
便種・個口の変更（平日も土日も）

送り状を出したあとに「これは入らない」で便や個口を変えるときに使う。配送会社と個口を選んで
「確かめる」→「実行」を押すだけで、倉庫のPC（AIworkspace\\weekend-shipping の change_worker）が
NEの修正（発送方法・送り状・時間帯・個口＝発送伝票備考欄）・B2の古い送り状の削除・新しい送り状の発行と印刷まで行う。

- このページ → 倉庫のPC: Googleドライブ weekend_ship/ws_chg_*.json（PCは読むだけ）
- 倉庫のPC → このページ: 共通の置き場（Supabase shared_secret_get）の weekend_ship_change_YYYYMMDD。
  伝票番号・便・個口・商品名・数・判定の文言だけ（氏名・住所は無い）
- やってよい変更かの決まりの正本は weekend-shipping の src/change_rules.py（このページに決まりを書かない）
- Team EC（Eシス）の注文はプルダウンで選ぶ。J番号で「実行」だけ（確かめる工程なし）。B2だけを直し、
  同じ便なら増えた分だけ足す／余った分だけ消す（weekend-shipping の src/te_change.py。2026-11-26）
"""
import datetime
import json
import re
import time
import uuid

import requests
import streamlit as st

st.set_page_config(page_title="便種・個口の変更", page_icon="📦", layout="centered")

from lib.auth import require_role  # noqa: E402
require_role("receiving")

from streamlit_autorefresh import st_autorefresh  # noqa: E402

from lib import master_store                       # noqa: E402
from lib.invoice import drive_master               # noqa: E402

JST = datetime.timezone(datetime.timedelta(hours=9))
SUBFOLDER = "weekend_ship"
# 画面の選択肢（NEの発送方法ID）。名前は weekend-shipping の change_rules.CARRIERS と同じ
CHOICES = {"20": "ヤマト 宅急便", "28": "ヤマト ネコポス", "17": "佐川 宅配便", "30": "ゆうパック", "35": "ゆうパケット"}
# Team EC（Eシス）の注文：B2で出す2つだけ。確かめる工程なしでB2だけ直す（2026-11-26 ユーザー指示。
# 正本は weekend-shipping の src/te_change.py）
TE_CHOICES = {"20": "ヤマト 宅急便", "28": "ヤマト ネコポス"}
TARGETS = ("パピー（NEの伝票）", "Team EC（Eシスの注文）")
J_NO = re.compile(r"^[Jj]\d{4,7}$")


def _secret(k):
    return str(st.secrets.get(k, "")).strip()


@st.cache_resource(show_spinner=False)
def _root():
    return drive_master.get_or_create_folder(SUBFOLDER, master_store.folder_id())


def read_state(day_s):
    url, anon, key = _secret("SHARED_SECRETS_URL"), _secret("SHARED_SECRETS_ANON_KEY"), _secret("SHARED_SECRETS_KEY")
    if not (url and anon and key):
        return None, "共通の置き場の設定（SHARED_SECRETS_*）がありません"
    try:
        r = requests.post(url.rstrip("/") + "/rest/v1/rpc/shared_secret_get", timeout=15,
                          json={"p_name": "weekend_ship_change_" + day_s.replace("-", ""), "p_reader": "streamlit"},
                          headers={"apikey": anon, "Authorization": "Bearer " + anon})
        r.raise_for_status()
        rows = r.json() or []
        if not rows:
            return None, ""
        from cryptography.fernet import Fernet
        return json.loads(Fernet(key.encode()).decrypt(rows[0]["ciphertext"].encode()).decode("utf-8")), ""
    except Exception as exc:  # noqa: BLE001
        return None, "倉庫のPCの状態を読めません（{}）".format(type(exc).__name__)


def send(cmd, body):
    now = datetime.datetime.now(JST)
    body = dict(body, cmd=cmd, at=now.isoformat(timespec="seconds"),
                by=st.session_state.get("auth_user", "") or "page")
    name = "ws_chg_{}_{}.json".format(now.strftime("%Y%m%d_%H%M%S%f"), cmd)
    drive_master.upload_bytes(json.dumps(body, ensure_ascii=False).encode("utf-8"), name, _root(),
                              mimetype="application/json")


day = datetime.datetime.now(JST).date().isoformat()
st.title("📦 便種・個口の変更")
st.caption("送り状を出したあとでも使えます。古い送り状の削除と、新しい送り状の発行・印刷までツールが行います。")

req = st.session_state.get("chg_req")
if req:
    st_autorefresh(interval=6000, key="chg_refresh")
s, err = read_state(day)
if err:
    st.error(err)
# 倉庫のPCの担当が止まったとき（2026-11-24 ユーザー決定）。担当は1分後に自動で起き直す（1日10回まで）。
# 起き直した担当は止まっていた間の依頼を拾わないので、ここで「もう一度押して」と出す。
# 起き直せないとき（上限・PCが落ちている）は、ここから管理者のChatworkへ直接知らせる（稼働監視の赤には頼らない）
now_h = datetime.datetime.now(JST).hour
stale = bool(s) and time.time() - float(s.get("published_ts") or 0) > 300
gave_up = bool(s) and bool(s.get("gave_up"))
restarted = bool(req and s and not gave_up and not stale
                 and float(s.get("started_ts") or 0) > float(req.get("sent_ts") or 0)
                 and req["req_id"] not in (s.get("requests") or {}))


def _tell_admin(why):
    """依頼が止まっているときだけ、依頼ごとに1回。"""
    key = "chg_admin_told_" + req["req_id"]
    if st.session_state.get(key):
        return True
    from lib.notify import chatwork
    ok = chatwork.create_task("[便種・個口の変更] 伝票{}の依頼が進みません：{}".format(req["order_id"], why),
                              limit_days=1, audience=chatwork.ADMIN)
    st.session_state[key] = ok
    return ok


if req and not (8 <= now_h < 20):
    st.warning("便種・個口の変更の受付は 8:00〜20:00 です。")
elif req and gave_up:
    st.error("倉庫のPC（変更の担当）が今日何度も止まったため、止めています。管理者にChatworkで知らせてあります。")
elif req and (stale or not s) and not err:
    told = _tell_admin("倉庫のPCの担当が5分以上動いていません（PCが止まっている可能性）")
    st.error("倉庫のPC（変更の担当）が5分以上動いていません。"
             + ("管理者にChatworkで知らせました。" if told else "Chatworkで管理者に連絡してください。"))
elif stale:
    st.error("倉庫のPC（変更の担当）が5分以上動いていません。Chatworkで管理者に連絡してください。")
if restarted:
    if req.get("te"):
        # Team EC は「目標の状態」で動くので、押し直しても二重には出ない（済んでいれば「変更なし」になる）
        st.warning("倉庫のPC（変更の担当）が起き直しました。もう一度「実行」を押してください"
                   "（済んだところは飛ばし、残りだけ行います）。")
    elif st.session_state.get("chg_ran") == req["req_id"]:
        # 「実行」の後に止まった：NEやB2が途中まで変わっていることがある。確かめ直せば今の状態から計画し直す
        st.warning("倉庫のPC（変更の担当）が実行の途中で起き直しました。もう一度「確かめる」を押すと、"
                   "今のNEとB2の状態から確かめ直します。")
    else:
        st.warning("倉庫のPC（変更の担当）が起き直しました。この依頼は届いていません。もう一度「確かめる」を押してください。")
    st.session_state.pop("chg_ran", None)

target = st.selectbox("どちらの注文ですか", TARGETS, index=1 if (req or {}).get("te") else 0)
te = target == TARGETS[1]
with st.form("chg_form"):
    if te:
        oid = st.text_input("Eシスの番号（送り状の品名にある J から始まる番号。例 J81828）",
                            value=(req or {}).get("order_id", "") if (req or {}).get("te") else "", max_chars=10)
    else:
        oid = st.text_input("伝票番号（NEの伝票番号）",
                            value="" if (req or {}).get("te") else (req or {}).get("order_id", ""), max_chars=12)
    c1, c2 = st.columns(2)
    menu = TE_CHOICES if te else CHOICES
    dv = c1.selectbox("配送会社・便", list(menu), format_func=menu.get)
    boxes = c2.selectbox("個口", list(range(1, 11)))
    go = st.form_submit_button("実行" if te else "確かめる", type="primary", use_container_width=True)
if go and te:
    oid = oid.strip().upper()
    if oid.isdigit():
        st.error("数字だけの番号はパピー（NEの伝票番号）です。上で「パピー」を選んでください")
    elif not J_NO.match(oid):
        st.error("J から始まる番号（例 J81828）で入れてください")
    else:
        req = {"req_id": uuid.uuid4().hex[:12], "order_id": oid, "delivery_id": dv, "boxes": int(boxes), "te": True}
        send("te_run", req)
        st.session_state["chg_req"] = dict(req, sent_ts=time.time())
        st.session_state["chg_ran"] = req["req_id"]
        st.rerun()
elif go:
    oid = oid.strip()
    if J_NO.match(oid):
        st.error("J から始まる番号は Team EC の注文です。上で「Team EC」を選んでください")
    elif not oid.isdigit():
        st.error("伝票番号は数字だけで入れてください")
    else:
        req = {"req_id": uuid.uuid4().hex[:12], "order_id": oid, "delivery_id": dv, "boxes": int(boxes)}
        send("check", req)
        st.session_state["chg_req"] = dict(req, sent_ts=time.time())   # 担当が起き直す前の依頼かを見分ける
        st.session_state.pop("chg_ran", None)
        st.rerun()

if req and req.get("te") and not restarted:
    r = ((s or {}).get("requests") or {}).get(req["req_id"])
    with st.container(border=True):
        st.markdown("**Team EC {}** → {}・{}個口".format(req["order_id"], TE_CHOICES.get(req["delivery_id"]), req["boxes"]))
        if not r or r.get("status") in ("queued", "running"):
            st.info("倉庫のPCがB2の送り状を直しています（1〜3分かかります）…")
            st.stop()
        if r.get("status") == "error":
            st.error(r.get("message", "失敗しました"))
        elif r.get("status") == "done":
            st.success(r.get("message", "終わりました"))
        if st.button("別の注文を変える"):
            st.session_state.pop("chg_req", None)
            st.session_state.pop("chg_ran", None)
            st.rerun()
elif req and not restarted:
    r = ((s or {}).get("requests") or {}).get(req["req_id"])
    box = st.container(border=True)
    with box:
        st.markdown("**伝票 {}** → {}・{}個口".format(req["order_id"], CHOICES.get(req["delivery_id"]), req["boxes"]))
        if not r or r.get("status") in ("queued", "checking"):
            st.info("倉庫のPCが確かめています（NEとB2を見るので1〜3分かかります）…")
            st.stop()
        if r.get("status") == "error":
            st.error(r.get("message", "失敗しました"))
            st.stop()
        cur = r.get("current") or {}
        if cur:
            st.write("今：{}・{}個口　／　送り状：{}".format(cur.get("delivery_name", ""), cur.get("boxes", "?"),
                                                    cur.get("issued_text", "")))
            for it in cur.get("items", []):
                st.caption("・{} × {}".format(it.get("name", ""), it.get("qty", "")))
        for m in r.get("ng", []):
            st.error("🚫 " + m)
        for m in r.get("warn", []):
            st.warning("⚠️ " + m)
        for m in r.get("info", []):
            st.info(m)
        if r.get("plan"):
            st.markdown("**実行すると：**")
            for p in r["plan"]:
                st.write("・" + p)
        if r.get("status") == "checked" and not r.get("ng"):
            ok_warn = True
            if r.get("warn"):
                ok_warn = st.checkbox("上の注意を確かめました（入れ方を確認した）", key="chg_ack_" + req["req_id"])
            if st.session_state.get("chg_ran") == req["req_id"]:
                st.info("実行しています…")
            elif st.button("実行", type="primary", use_container_width=True, disabled=not ok_warn):
                send("run", dict({k: v for k, v in req.items() if k != "sent_ts"}, warn_ack=bool(r.get("warn"))))
                st.session_state["chg_ran"] = req["req_id"]
                st.session_state["chg_req"] = dict(req, sent_ts=time.time())
                st.rerun()
        elif r.get("status") == "running":
            st.info("実行しています（NEの修正 → B2の削除 → 発行・印刷）…")
        elif r.get("status") == "done":
            st.success(r.get("message", "終わりました"))
            if st.button("別の伝票を変える"):
                st.session_state.pop("chg_req", None)
                st.rerun()
