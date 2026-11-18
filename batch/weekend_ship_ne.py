#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
土日祝出荷（weekend-shipping）の NE API 担当。

【なぜ GitHub Actions で動くか】
  NE のトークンは呼ぶたびに入れ替わり、置き場所が2つあると片方が古くなって止まる。
  だから NE を呼ぶのはこのリポジトリの Actions だけにしている（延命・受注明細と同じ）。
  土日祝の出荷を回す本体（AIworkspace\\weekend-shipping）はローカルPCで動くので、
  PC → Drive に「指示ファイル」を置き、ここがそれを拾って NE を呼び、結果を Drive に返す。

【このリポジトリは公開】
  ログに受注の中身（伝票番号・氏名・住所・メール・送り状番号）を出さない。
  出すのは件数・指示ファイルの名前・エラーの種類だけ。中身は Drive（非公開）にだけ置く。

【NEの画面は触らない】
  NE 利用規約 第24条1項(17)。ここで使うのは公式 API だけ。

【NE API の無料枠】
  月1000回まで無料。1つの指示で使ってよい回数を JOB_BUDGET で決め、超える指示は
  途中で止めて「どこまでやったか」を結果に書く（黙って全部やったことにしない）。

PC → ここ（指示・出荷日の一覧）: 共通の置き場（Supabase shared_secrets）に Fernet で暗号化して追記
  weekend_ship_job      {"id": "...", "kind": "ping|snapshot|apply|ship", "dry_run": bool, ...}（最新の1行だけ見る）
  weekend_ship_calendar {"ship_dates": ["2026-11-21", ...]}
ここ → PC（結果）: ドライブ weekend_ship/ws_result_<id>.json・待受の印 ws_ne_alive.json（数分おき）
  ※ ドライブで往復しないのは、両方の Google 認可が drive.file で、相手のアプリが作ったファイルが見えないため

起動: PC が枝 weekend-ship-trigger に合図を push する（weekend-ship-trigger.yml・数十秒で起動）。
      時刻指定（7:40・12:00）は GitHub の混雑で数時間遅れることがある（2026-11-17 に 12:00 の回が 13:46）ので予備

実行:
    python batch/weekend_ship_ne.py --probe            # 使う項目があるか・発送方法の一覧（API 3〜4回）
    python batch/weekend_ship_ne.py --serve --until auto   # auto = 5時間40分後（21時まで）
"""
import argparse
import datetime
import json
import os
import sys
import time
import traceback
from xml.sax.saxutils import escape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from batch import st_shim                              # noqa: E402
st_shim.install()

from lib import master_store                           # noqa: E402
from lib.invoice import drive_master                   # noqa: E402
from lib.ne_api import client, usage                   # noqa: E402

JST = datetime.timezone(datetime.timedelta(hours=9))
SUBFOLDER = "weekend_ship"
BASE_EP = "api_v1_receiveorder_base/search"
ROW_EP = "api_v1_receiveorder_row/search"
UPDATE_EP = "api_v1_receiveorder_base/update"
SHIPPED_EP = "api_v1_receiveorder_base/shipped"

POLL_SECONDS = 15          # 1回読むごとに shared_secret_reads に1行残る（出荷日に数百行）
ALIVE_EVERY = 300          # 待受の印を更新する間隔（秒）
IDLE_EXIT_MINUTES = 75     # 出荷日でも、この時間なにも指示が来なければ終わる（PCが止まっている）
JOB_BUDGET = 400           # 1つの指示で使ってよい NE API 回数

# snapshot で取る伝票の項目。氏名・住所は取らない（照合に要るのは購入者メールと店舗の注文番号だけ）
BASE_FIELDS = [
    "receive_order_id",
    "receive_order_shop_id",
    "receive_order_shop_cut_form_id",
    "receive_order_date",
    "receive_order_order_status_id",
    "receive_order_order_status_name",
    "receive_order_confirm_check_id",
    "receive_order_cancel_type_id",
    "receive_order_delivery_id",
    "receive_order_delivery_name",
    "receive_order_delivery_cut_form_id",
    "receive_order_delivery_cut_form_note",
    "receive_order_send_plan_date",
    "receive_order_statement_delivery_instruct_printing_date",
    "receive_order_gruoping_tag",
    "receive_order_worker_text",
    "receive_order_purchaser_mail_address",
    "receive_order_hope_delivery_date",
    "receive_order_last_modified_date",
]
ROW_FIELDS = [
    "receive_order_row_receive_order_id",
    "receive_order_row_no",
    "receive_order_row_goods_id",
    "receive_order_row_goods_name",
    "receive_order_row_quantity",
    "receive_order_row_cancel_flag",
]
# update で書いてよい項目（これ以外は受け付けない＝PC側の間違いで別の項目を壊さない）
UPDATABLE = {
    "receive_order_confirm_check_id",
    "receive_order_delivery_id",
    "receive_order_send_plan_date",
    "receive_order_worker_text",
    "receive_order_gruoping_tag",
    "receive_order_delivery_cut_form_id",
}


class Budget:
    def __init__(self, limit):
        self.limit, self.used = limit, 0

    def call(self, endpoint, params=None):
        if self.used >= self.limit:
            raise RuntimeError("この指示で使ってよいNE APIの回数（{}回）に達しました".format(self.limit))
        self.used += 1
        return client.call(endpoint, params)


# ---------- Drive ----------

def _folder():
    return drive_master.get_or_create_folder(SUBFOLDER, master_store.folder_id())


def _write_json(name, data):
    body = json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
    drive_master.upload_or_replace(body, name, _folder(), mimetype="application/json")


def _read_json(file_id):
    return json.loads(drive_master.download_bytes(file_id).decode("utf-8"))


def _now():
    return datetime.datetime.now(JST)


# ---------- 純関数（テスト対象） ----------

def update_xml(fields):
    """update API の data（XML）。許可した項目だけを書く。"""
    bad = sorted(set(fields) - UPDATABLE)
    if bad:
        raise ValueError("更新を許していない項目です: {}".format(", ".join(bad)))
    parts = ["<{0}>{1}</{0}>".format(k, escape(str(v))) for k, v in fields.items()]
    return ('<?xml version="1.0" encoding="utf-8"?><root><receiveorder_base>'
            + "".join(parts) + "</receiveorder_base></root>")


def search_all(budget, endpoint, fields, filters, page=10000):
    rows, offset = [], 0
    while True:
        params = {"fields": ",".join(fields), "limit": str(page), "offset": str(offset)}
        params.update(filters)
        data = budget.call(endpoint, params).get("data") or []
        rows.extend(data)
        if len(data) < page:
            return rows
        offset += page


# ---------- 指示の種類 ----------

def job_ping(job, budget):
    budget.call(client.KEEPALIVE_ENDPOINT)
    return {"ok": True}


def job_snapshot(job, budget):
    """出荷に関わる伝票を取る。status_ids（既定 20=納品書印刷待ち）と、
    受注日の上限 (order_date_lte) で絞る。明細も一緒に返す。"""
    status_ids = [str(s) for s in job.get("status_ids", [20])]
    orders = []
    for sid in status_ids:
        filters = {"receive_order_order_status_id-eq": sid,
                   "receive_order_cancel_type_id-eq": "0"}
        if job.get("order_date_gte"):
            filters["receive_order_date-gte"] = job["order_date_gte"]
        if job.get("order_date_lte"):
            filters["receive_order_date-lte"] = job["order_date_lte"]
        orders += search_all(budget, BASE_EP, BASE_FIELDS, filters)
    if not job.get("with_rows", True):
        return {"ok": True, "orders": orders, "rows": [], "status_ids": status_ids}
    ids = [o["receive_order_id"] for o in orders]
    rows = []
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        rows += search_all(budget, ROW_EP, ROW_FIELDS,
                           {"receive_order_row_receive_order_id-in": ",".join(chunk),
                            "receive_order_row_cancel_flag-eq": "0"})
    return {"ok": True, "orders": orders, "rows": rows, "status_ids": status_ids}


FRESH_FIELDS = ("receive_order_id,receive_order_last_modified_date,receive_order_order_status_id,"
                "receive_order_worker_text,receive_order_gruoping_tag,receive_order_delivery_cut_form_id,"
                "receive_order_cancel_type_id")


def _fresh_many(budget, order_ids):
    """伝票の今の状態をまとめて取る（200件ずつ1回）。伝票ごとに読み直すと回数が倍になるため。"""
    ids = [str(i) for i in order_ids]
    out = {}
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        for row in search_all(budget, BASE_EP, FRESH_FIELDS.split(","),
                              {"receive_order_id-in": ",".join(chunk)}):
            out[str(row["receive_order_id"])] = row
    return out


def job_apply(job, budget):
    """伝票ごとに項目を更新する。ops: [{order_id, last_modified, set:{...}, prepend_worker_text, add_tag}]
    作業用欄は上書きせず、先頭に足す（大前提1）。最終更新日がPCの読んだ時点と違えば、
    人がNEで触った後なので更新せずに返す（取り違えて上書きしない）。"""
    dry = bool(job.get("dry_run"))
    ops = job.get("ops", [])
    now = _fresh_many(budget, [op["order_id"] for op in ops]) if ops else {}
    results = []
    for op in ops:
        oid = str(op["order_id"])
        res = {"order_id": oid}
        try:
            cur = now.get(oid)
            if not cur:
                raise RuntimeError("伝票が見つかりません")
            if op.get("last_modified") and cur["receive_order_last_modified_date"] != op["last_modified"]:
                res.update(ok=False, error="changed", detail="PCが読んだ後にNEで更新されています")
                results.append(res)
                continue
            fields = dict(op.get("set") or {})
            if op.get("prepend_worker_text"):
                old = cur.get("receive_order_worker_text") or ""
                fields["receive_order_worker_text"] = op["prepend_worker_text"] + ("\n" + old if old else "")
            if op.get("add_tag"):
                old = cur.get("receive_order_gruoping_tag") or ""
                tag = "[{}]".format(op["add_tag"])
                if tag not in old:
                    fields["receive_order_gruoping_tag"] = old + tag
            xml = update_xml(fields)
            if dry:
                res.update(ok=True, dry_run=True, fields=sorted(fields))
            else:
                budget.call(UPDATE_EP, {"receive_order_id": oid,
                                        "receive_order_last_modified_date": cur["receive_order_last_modified_date"],
                                        "data": xml})
                res.update(ok=True, fields=sorted(fields))
        except Exception as exc:  # noqa: BLE001
            res.update(ok=False, error=type(exc).__name__, detail=str(exc)[:300])
        results.append(res)
    return {"ok": all(r.get("ok") for r in results), "results": results, "dry_run": dry}


def job_ship(job, budget):
    """送り状番号を入れてから出荷確定する。ops: [{order_id, tracking}]
    既に出荷確定済み（受注状態50）のものは番号だけ確かめて飛ばす（二重に確定しない）。"""
    dry = bool(job.get("dry_run"))
    ops = {str(op["order_id"]): str(op["tracking"]).strip() for op in job.get("ops", [])}
    res = {oid: {"order_id": oid} for oid in ops}
    if not ops:
        return {"ok": True, "results": [], "dry_run": dry}

    # 1) 今の状態をまとめて読み、番号が違うものだけ番号を入れる
    cur = _fresh_many(budget, ops)
    todo = []
    for oid, tracking in ops.items():
        c = cur.get(oid)
        if not c:
            res[oid].update(ok=False, error="missing", detail="伝票が見つかりません")
            continue
        status = str(c.get("receive_order_order_status_id"))
        have = (c.get("receive_order_delivery_cut_form_id") or "").strip()
        if status == "50":
            res[oid].update(ok=(have == tracking), skipped="already_shipped",
                            detail="" if have == tracking else "出荷確定済みだが送り状番号が違います")
            continue
        if status not in ("20", "40") or str(c.get("receive_order_cancel_type_id")) != "0":
            res[oid].update(ok=False, error="status", detail="受注状態{}（キャンセル区分{}）は出荷確定できません".format(
                status, c.get("receive_order_cancel_type_id")))
            continue
        if dry:
            res[oid].update(ok=True, dry_run=True)
            continue
        if have != tracking:
            try:
                budget.call(UPDATE_EP, {"receive_order_id": oid,
                                        "receive_order_last_modified_date": c["receive_order_last_modified_date"],
                                        "data": update_xml({"receive_order_delivery_cut_form_id": tracking})})
            except Exception as exc:  # noqa: BLE001
                res[oid].update(ok=False, error=type(exc).__name__, detail=str(exc)[:300])
                continue
        todo.append(oid)

    # 2) 最終更新日が変わったので読み直して、出荷確定
    if todo:
        cur = _fresh_many(budget, todo)
        for oid in todo:
            try:
                budget.call(SHIPPED_EP, {"receive_order_id": oid,
                                         "receive_order_last_modified_date": cur[oid]["receive_order_last_modified_date"]})
            except Exception as exc:  # noqa: BLE001
                res[oid].update(ok=False, error=type(exc).__name__, detail=str(exc)[:300])

        # 3) 本当に出荷確定済みで、番号が入っているかを確かめる（成功を推測で書かない）
        after = _fresh_many(budget, todo)
        for oid in todo:
            if res[oid].get("error"):
                continue
            a = after.get(oid) or {}
            ok = (str(a.get("receive_order_order_status_id")) == "50"
                  and (a.get("receive_order_delivery_cut_form_id") or "").strip() == ops[oid])
            res[oid]["ok"] = ok
            if not ok:
                res[oid]["detail"] = "出荷確定後の確認で状態か番号が合いません"
    results = list(res.values())
    return {"ok": all(r.get("ok") for r in results), "results": results, "dry_run": dry}


JOBS = {"ping": job_ping, "snapshot": job_snapshot, "apply": job_apply, "ship": job_ship}


def run_job(job):
    jid = str(job["id"])
    kind = job.get("kind")
    budget = Budget(int(job.get("budget") or JOB_BUDGET))
    started = _now().isoformat(timespec="seconds")
    try:
        if kind not in JOBS:
            raise ValueError("知らない指示です: {}".format(kind))
        out = JOBS[kind](job, budget)
    except Exception as exc:  # noqa: BLE001
        out = {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:300],
               "trace": traceback.format_exc()[-1500:]}
    out.update(id=jid, kind=kind, started_at=started, finished_at=_now().isoformat(timespec="seconds"),
               ne_calls=budget.used)
    _write_json("ws_result_{}.json".format(jid), out)
    # 公開ログに出すのは件数と種類だけ
    n = len(out.get("results") or out.get("orders") or [])
    print("[weekend_ship] job={} kind={} ok={} items={} ne_calls={}".format(
        jid, kind, out.get("ok"), n, budget.used), flush=True)


def _shared_get(name):
    """共通の置き場（GapBoard の Supabase・shared_secrets）から最新の1行を読んで復号する。
    PC（weekend-shipping）が指示と出荷日の一覧をここへ暗号化して追記する。
    ドライブを使わないのは、PCとこのリポジトリのGoogle認可がどちらも drive.file で、
    相手のアプリが作ったファイルが見えないため（2026-11-17 に確認）。"""
    import requests
    from cryptography.fernet import Fernet
    url = os.environ.get("SHARED_SECRETS_URL", "").rstrip("/")
    anon = os.environ.get("SHARED_SECRETS_ANON_KEY", "")
    key = os.environ.get("SHARED_SECRETS_KEY", "")
    if not (url and anon and key):
        raise RuntimeError("SHARED_SECRETS_* が未設定です")
    r = requests.post(url + "/rest/v1/rpc/shared_secret_get", timeout=20,
                      headers={"apikey": anon, "Authorization": "Bearer " + anon, "Content-Type": "application/json"},
                      json={"p_name": name, "p_reader": "github-actions"})
    r.raise_for_status()
    rows = r.json() or []
    if not rows:
        return None
    return json.loads(Fernet(key.encode()).decrypt(rows[0]["ciphertext"].encode()).decode("utf-8"))


def pending_jobs():
    job = _shared_get("weekend_ship_job")
    if not job:
        return []
    done = drive_master.find_file("ws_result_{}.json".format(job["id"]), _folder())
    return [] if done else [job]


def is_ship_day(today):
    try:
        cal = _shared_get("weekend_ship_calendar") or {}
        return today in (cal.get("ship_dates") or [])
    except Exception as exc:  # noqa: BLE001
        print("[weekend_ship] 出荷日の一覧を読めません: {}".format(type(exc).__name__), flush=True)
        return False


def serve(until_hhmm, force=False):
    today = _now().strftime("%Y-%m-%d")
    if not force and not is_ship_day(today):
        print("[weekend_ship] {} は出荷日の一覧にありません。待ち受けずに終わります。".format(today), flush=True)
        return 0
    if until_hhmm == "auto":   # ジョブの上限6時間の手前まで。21時を越えない
        end = min(_now() + datetime.timedelta(hours=5, minutes=40),
                  _now().replace(hour=21, minute=0, second=0, microsecond=0))
    else:
        hh, mm = (int(x) for x in until_hhmm.split(":"))
        end = _now().replace(hour=hh, minute=mm, second=0, microsecond=0)
    last_job = time.time()
    handled = 0
    print("[weekend_ship] 待ち受け開始（{}まで）".format(end.strftime("%H:%M")), flush=True)
    _write_json("ws_ne_alive.json", {"at": _now().isoformat(timespec="seconds"),
                                     "until": end.isoformat(timespec="seconds"), "handled": 0})
    last_alive = time.time()
    while _now() < end:
        if time.time() - last_alive > ALIVE_EVERY:
            _write_json("ws_ne_alive.json", {"at": _now().isoformat(timespec="seconds"),
                                             "until": end.isoformat(timespec="seconds"), "handled": handled})
            last_alive = time.time()
        try:
            for job in pending_jobs():
                run_job(job)
                handled += 1
                last_job = time.time()
        except Exception as exc:  # noqa: BLE001
            print("[weekend_ship] 待ち受け中のエラー: {}".format(type(exc).__name__), flush=True)
        if time.time() - last_job > IDLE_EXIT_MINUTES * 60 and handled == 0:
            print("[weekend_ship] {}分間指示が来ません。PCが止まっている可能性があります。".format(
                IDLE_EXIT_MINUTES), flush=True)
            _write_json("ws_ne_alive.json", {"at": _now().isoformat(timespec="seconds"), "idle_exit": True,
                                             "handled": handled})
            # PC 側が止まっていると、PC からは知らせられない。ここ（クラウド）から管理者へ知らせる
            try:
                from lib.notify import chatwork
                chatwork.create_task(
                    "[info][title]🔴 土日祝出荷：PCから指示が来ません[/title]"
                    "今日は出荷日ですが、{}分間 weekend-shipping（倉庫のPC）から指示がありません。"
                    "PCの電源・スリープ・Wi-Fiを確かめてください。伝票は出ていません。[/info]".format(IDLE_EXIT_MINUTES),
                    limit_days=1, audience=chatwork.ADMIN)
            except Exception:  # noqa: BLE001
                pass
            return 1    # 赤にして気づけるようにする
        usage.flush()
        time.sleep(POLL_SECONDS)
    _write_json("ws_ne_alive.json", {"at": _now().isoformat(timespec="seconds"), "ended": True,
                                     "handled": handled})
    print("[weekend_ship] 待ち受け終了 handled={}".format(handled), flush=True)
    return 0


def probe():
    """使う項目が実在するかと、発送方法・受注状態の一覧を Drive に書く。ログには件数だけ。"""
    budget = Budget(40)
    report = {"at": _now().isoformat(timespec="seconds"), "fields": {}, "lists": {}}
    alive = list(BASE_FIELDS)
    for _ in range(10):
        try:
            data = budget.call(BASE_EP, {"fields": ",".join(alive), "limit": "50",
                                         "receive_order_order_status_id-eq": "20"}).get("data") or []
            break
        except Exception as exc:  # noqa: BLE001
            named = [f for f in alive if f in str(exc)]
            if not named:
                report["error"] = str(exc)[:300]
                data = []
                break
            for f in named:
                alive.remove(f)
                report["fields"][f] = "使えない"
    for f in alive:
        report["fields"][f] = "使える"
    # 値の分布（個人情報でない項目だけ）
    dist = {}
    for key in ("receive_order_delivery_id", "receive_order_delivery_name", "receive_order_gruoping_tag",
                "receive_order_confirm_check_id", "receive_order_shop_id"):
        if key in alive:
            c = {}
            for row in data:
                v = str(row.get(key))
                c[v] = c.get(v, 0) + 1
            dist[key] = c
    report["status20_sample"] = {"count": len(data), "distribution": dist}
    # 印刷待ち・印刷済みの全件の内訳（受注月・店舗・発送方法・確認チェック・出荷予定日の有無）。
    # 古い伝票が残っていると、受注状態だけで対象を決めたときに一緒に出荷してしまうため
    keys = ["receive_order_id", "receive_order_date", "receive_order_shop_id", "receive_order_delivery_name",
            "receive_order_confirm_check_id", "receive_order_send_plan_date", "receive_order_last_modified_date",
            "receive_order_statement_delivery_instruct_printing_date"]
    for sid in ("20", "40"):
        try:
            rows = search_all(budget, BASE_EP, keys, {"receive_order_order_status_id-eq": sid,
                                                      "receive_order_cancel_type_id-eq": "0"})
        except Exception as exc:  # noqa: BLE001
            report["status_" + sid] = "取得できません: " + str(exc)[:200]
            continue
        agg = {"total": len(rows)}
        for label, fn in (("order_month", lambda r: (r.get("receive_order_date") or "")[:7]),
                          ("modified_month", lambda r: (r.get("receive_order_last_modified_date") or "")[:7]),
                          ("shop", lambda r: r.get("receive_order_shop_id")),
                          ("delivery", lambda r: r.get("receive_order_delivery_name")),
                          ("confirm", lambda r: r.get("receive_order_confirm_check_id")),
                          ("send_plan", lambda r: (r.get("receive_order_send_plan_date") or "")[:10]),
                          ("instruct_print", lambda r: (r.get("receive_order_statement_delivery_instruct_printing_date") or "")[:10])):
            c = {}
            for r in rows:
                v = str(fn(r))
                c[v] = c.get(v, 0) + 1
            agg[label] = dict(sorted(c.items(), key=lambda kv: -kv[1])[:40])
        # 直近14日に受注したものだけの内訳
        recent = [r for r in rows if (r.get("receive_order_date") or "") >= (_now() - datetime.timedelta(days=14)).strftime("%Y-%m-%d")]
        agg["recent14_total"] = len(recent)
        c = {}
        for r in recent:
            k = "{}|{}|confirm={}".format(r.get("receive_order_shop_id"), r.get("receive_order_delivery_name"),
                                          r.get("receive_order_confirm_check_id"))
            c[k] = c.get(k, 0) + 1
        agg["recent14"] = c
        report["status_" + sid] = agg
    for name, ep in (("delivery", "api_v1_system_delivery/info"),
                     ("order_status", "api_v1_system_orderstatus/info"),
                     ("shop", "api_v1_master_shop/search")):
        try:
            params = {"fields": "shop_id,shop_name"} if name == "shop" else None
            report["lists"][name] = budget.call(ep, params).get("data")
        except Exception as exc:  # noqa: BLE001
            report["lists"][name] = "取得できません: " + str(exc)[:200]
    _write_json("ws_probe.json", report)
    print("[weekend_ship] probe: 使える項目{}個 / 使えない{}個 / 印刷待ちの見本{}件 / NE API {}回".format(
        sum(1 for v in report["fields"].values() if v == "使える"),
        sum(1 for v in report["fields"].values() if v == "使えない"), len(data), budget.used), flush=True)
    return 0


def main():
    ap = argparse.ArgumentParser(description="土日祝出荷の NE API 担当")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--until", default="11:30")
    ap.add_argument("--force", action="store_true", help="出荷日の一覧に無くても待ち受ける（試験用）")
    args = ap.parse_args()
    try:
        if args.probe:
            return probe()
        if args.serve:
            return serve(args.until, args.force)
        ap.print_help()
        return 2
    finally:
        usage.flush()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print("[weekend_ship] FAILED: {}".format(type(exc).__name__), file=sys.stderr, flush=True)
        sys.exit(1)
