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

POLL_SECONDS = 5           # 1回読むごとに shared_secret_reads に1行残る（5秒で1時間720行）。便種・個口の変更で
                           # 待ち時間が目立ったため 15→5秒（2026-11-20）
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
    # 発送方法をまたいで付け替えるときに一緒に直す項目（コードは発送方法ごとに違う。2026-11-19 調査）
    "receive_order_hope_delivery_time_slot_id",
    "receive_order_delivery_method_id",
    "receive_order_invoice_id",
    "receive_order_temperature_id",
    "receive_order_business_office_stop_id",
    "receive_order_seal1_id", "receive_order_seal2_id", "receive_order_seal3_id", "receive_order_seal4_id",
    "receive_order_payment_method_id",
    "receive_order_payment_method_name",
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
    # 送り状（発払い／ネコポス など）。発送方法だけ変えると送り状が元の便のまま残り、
    # 配送CSVの送り状種別が食い違う（2026-11-19 本番で発覚）。項目名は inspect で確かめて使う
    "receive_order_invoice_id",
    "receive_order_delivery_method_id",
    # 発送方法をまたぐ付け替えで、元の便のコードを消す・ヤマトのコードに置き換える（2026-11-19 調査）
    "receive_order_hope_delivery_time_slot_id",
    "receive_order_seal1_id", "receive_order_seal2_id", "receive_order_seal3_id", "receive_order_seal4_id",
    # 個口数（NEに専用の項目が無く、発送伝票備考欄に数字で入れる決まり。Trello「配送サイズ違いまとめ」）
    "receive_order_delivery_cut_form_note",
}

# 送り状に当たる項目の候補（NEのAPI項目名を inspect で確かめる。値はコードなので個人情報ではない）
INVOICE_CANDIDATES = [
    "receive_order_invoice_id", "receive_order_invoice_name",
    "receive_order_delivery_method_id", "receive_order_delivery_method_name",
    "receive_order_business_office_stop_id", "receive_order_temperature_id",
    "receive_order_seal1_id", "receive_order_delivery_time_id",
]


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
            for attempt in range(3):
                fields = dict(op.get("set") or {})
                if op.get("prepend_worker_text"):
                    old = cur.get("receive_order_worker_text") or ""
                    # やり直しで同じ印を二重に書かない
                    if op["prepend_worker_text"] not in old:
                        fields["receive_order_worker_text"] = op["prepend_worker_text"] + ("\n" + old if old else "")
                if op.get("add_tag"):
                    old = cur.get("receive_order_gruoping_tag") or ""
                    tag = "[{}]".format(op["add_tag"])
                    if tag not in old:
                        fields["receive_order_gruoping_tag"] = old + tag
                xml = update_xml(fields)
                if dry:
                    res.update(ok=True, dry_run=True, fields=sorted(fields))
                    break
                try:
                    params = {"receive_order_id": oid,
                              "receive_order_last_modified_date": cur["receive_order_last_modified_date"],
                              "data": xml}
                    # 出荷確定済み（倉庫の出荷検品の後）でも直せるように。旗が無いと 011010 で断られる（11/19 確認）。
                    # 便種・個口の変更は検品のスキャンの後に起きる（2026-11-20 ユーザー指示：印刷済み・出荷済みどちらでも）
                    if str(cur.get("receive_order_order_status_id")) == "50":
                        params["receive_order_shipped_update_flag"] = "1"
                    budget.call(UPDATE_EP, params)
                    res.update(ok=True, fields=sorted(fields), attempts=attempt + 1)
                    break
                except Exception as exc:  # noqa: BLE001
                    # 011006＝読んだ後にNEの中で更新された（受注自動処理の直後など）。読み直して最大3回
                    if "011006" not in str(exc) or attempt == 2:
                        raise
                    time.sleep(5)
                    cur = _fresh_many(budget, [oid]).get(oid) or cur
        except Exception as exc:  # noqa: BLE001
            res.update(ok=False, error=type(exc).__name__, detail=str(exc)[:300])
        results.append(res)
    return {"ok": all(r.get("ok") for r in results), "results": results, "dry_run": dry}


def job_ship(job, budget):
    """送り状番号を入れてから出荷確定する。ops: [{order_id, tracking}]
    既に出荷確定済み（受注状態50）のものは番号だけ確かめて飛ばす（二重に確定しない）。"""
    dry = bool(job.get("dry_run"))
    ops = {str(op["order_id"]): str(op["tracking"]).strip() for op in job.get("ops", [])}
    # only_if_shipped：ツールの締めの対象外でB2から出たもの（2回目の発行など）。出荷確定済みなら番号だけ入れ、
    # まだなら触らない（出荷していない伝票をツールが確定しない）
    soft = {str(op["order_id"]) for op in job.get("ops", []) if op.get("only_if_shipped")}
    res = {oid: {"order_id": oid} for oid in ops}
    if not ops:
        return {"ok": True, "results": [], "dry_run": dry}

    # 1) 今の状態をまとめて読み、番号が違うものだけ番号を入れる
    cur = _fresh_many(budget, ops)
    todo = []
    number_only = []
    for oid, tracking in ops.items():
        c = cur.get(oid)
        if not c:
            res[oid].update(ok=False, error="missing", detail="伝票が見つかりません")
            continue
        status = str(c.get("receive_order_order_status_id"))
        have = (c.get("receive_order_delivery_cut_form_id") or "").strip()
        if status == "50":
            # 倉庫の出荷検品で、番号が入る前に出荷確定済み（50）になる（2026-11-19 の24件で確認。
            # 番号はその後に入れる運用で、過去の伝票も50のあとに番号が入っている）。
            # 番号が空なら番号だけ入れる（確定はし直さない）。別の番号が入っていれば上書きせず止める
            if have == tracking:
                res[oid].update(ok=True, skipped="already_shipped")
                continue
            if have:
                res[oid].update(ok=False, error="different", detail="NEに別の送り状番号が入っています（上書きしません）")
                continue
            if dry:
                res[oid].update(ok=True, dry_run=True, number_only=True)
                continue
            try:
                # 出荷確定済みは receive_order_shipped_update_flag=1 が無いと 011010 で断られる（11/19 確認）
                budget.call(UPDATE_EP, {"receive_order_id": oid,
                                        "receive_order_last_modified_date": c["receive_order_last_modified_date"],
                                        "receive_order_shipped_update_flag": "1",
                                        "data": update_xml({"receive_order_delivery_cut_form_id": tracking})})
                number_only.append(oid)
            except Exception as exc:  # noqa: BLE001
                res[oid].update(ok=False, error=type(exc).__name__, detail="出荷確定済みの伝票に番号を入れられません：" + str(exc)[:250])
            continue
        if oid in soft:
            res[oid].update(ok=True, skipped="not_shipped_yet", detail="まだ出荷確定していないので触りません")
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
    if number_only:   # 番号だけ入れたものも、入ったかを読み直して確かめる
        after = _fresh_many(budget, number_only)
        for oid in number_only:
            a = after.get(oid) or {}
            ok = (a.get("receive_order_delivery_cut_form_id") or "").strip() == ops[oid]
            res[oid].update(ok=ok, number_only=True)
            if not ok:
                res[oid]["detail"] = "番号を入れたあとの確認で番号が合いません"
    results = list(res.values())
    return {"ok": all(r.get("ok") for r in results), "results": results, "dry_run": dry}


def job_holds(job, budget):
    """土日に保留した伝票（受注分類タグ[土日保留]）で、まだ出荷されていないもの。滞留の見張り用。"""
    fields = ["receive_order_id", "receive_order_shop_id", "receive_order_date", "receive_order_order_status_id",
              "receive_order_worker_text", "receive_order_gruoping_tag", "receive_order_last_modified_date"]
    rows = []
    for sid in ("2", "20", "40"):
        rows += search_all(budget, BASE_EP, fields, {"receive_order_order_status_id-eq": sid,
                                                     "receive_order_cancel_type_id-eq": "0",
                                                     "receive_order_gruoping_tag-like": "%" + job.get("tag", "土日保留") + "%"})
    return {"ok": True, "orders": rows}


def _alive_fields(budget, fields, filters):
    """使えない項目を外しながら検索する。返り値 (使えた項目, 行, 使えなかった項目)。"""
    return _alive_fields_ep(budget, BASE_EP, fields, filters)


def _alive_fields_ep(budget, endpoint, fields, filters):
    alive, dead = list(fields), []
    for _ in range(len(fields) + 1):
        try:
            return alive, search_all(budget, endpoint, alive, filters), dead
        except Exception as exc:  # noqa: BLE001
            named = [f for f in alive if f in str(exc)]
            if not named:
                raise
            for f in named:
                alive.remove(f)
                dead.append(f)
    return alive, [], dead


def job_inspect(job, budget):
    """送り状など、発送方法に連動する項目の名前と値を確かめる（読むだけ）。
    order_ids の伝票の値と、直近の出荷済み（受注状態50）での 発送方法×候補 の組み合わせを返す。"""
    base = ["receive_order_id", "receive_order_delivery_id", "receive_order_delivery_name",
            "receive_order_last_modified_date"]
    out = {"ok": True}
    ids = [str(x) for x in job.get("order_ids") or []]
    alive, rows, dead = _alive_fields(budget, base + INVOICE_CANDIDATES,
                                      {"receive_order_id-in": ",".join(ids)} if ids else {"receive_order_id-eq": "0"})
    out.update(fields_ok=alive, fields_ng=dead, orders=rows)
    since = (_now() - datetime.timedelta(days=14)).strftime("%Y-%m-%d")
    _, shipped, _ = _alive_fields(budget, alive, {"receive_order_order_status_id-eq": "50",
                                                  "receive_order_send_date-gte": since})
    combo = {}
    for r in shipped:
        k = "|".join("{}={}".format(f.replace("receive_order_", ""), r.get(f)) for f in alive
                     if f not in ("receive_order_id", "receive_order_last_modified_date"))
        combo[k] = combo.get(k, 0) + 1
    out["shipped_combos"] = dict(sorted(combo.items(), key=lambda kv: -kv[1])[:40])
    return out


# 発送方法に連動する項目（NE API 仕様 2026-11-19 確認：発送方法を変えても NE はこれらとの整合を確かめない）
DELIVERY_LINKED = [
    "receive_order_hope_delivery_time_slot_id", "receive_order_hope_delivery_time_slot_name",
    "receive_order_delivery_method_id", "receive_order_delivery_method_name",
    "receive_order_invoice_id", "receive_order_invoice_name",
    "receive_order_temperature_id", "receive_order_temperature_name",
    "receive_order_business_office_stop_id", "receive_order_business_office_stop_name",
    "receive_order_seal1_id", "receive_order_seal1_name", "receive_order_seal2_id", "receive_order_seal2_name",
    "receive_order_seal3_id", "receive_order_seal3_name", "receive_order_seal4_id", "receive_order_seal4_name",
    "receive_order_unattended_delivery_location_id", "receive_order_unattended_delivery_location_name",
    "receive_order_unattended_delivery_flg",
    "receive_order_payment_method_id", "receive_order_payment_method_name",
]
# 値の中身ではなく「入っているか」だけ数える項目（日付・金額・自由記入）
DELIVERY_PRESENCE = ["receive_order_hope_delivery_date", "receive_order_charge_amount",
                     "receive_order_delivery_cut_form_note"]


def job_fields(job, budget):
    """発送方法ごとに、連動する項目にどんな値が入っているか（読むだけ）。値は区分コードと名称・件数だけを返す。"""
    days = int(job.get("days") or 60)
    since = (_now() - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    base = ["receive_order_id", "receive_order_delivery_id", "receive_order_delivery_name"]
    ids = [str(x) for x in job.get("order_ids") or []]
    flt = ({"receive_order_id-in": ",".join(ids)} if ids else
           {"receive_order_order_status_id-eq": "50", "receive_order_send_date-gte": since})
    alive, rows, dead = _alive_fields(budget, base + DELIVERY_LINKED + DELIVERY_PRESENCE + ["receive_order_last_modified_date"], flt)
    if ids:   # 伝票ごとの値（区分コードと名称・日付の有無だけ。住所や氏名は取らない）
        return {"ok": True, "fields_ng": dead, "orders": [
            {k.replace("receive_order_", ""): (bool(r.get(k)) if k in DELIVERY_PRESENCE else r.get(k)) for k in alive}
            for r in rows]}
    per = {}
    for r in rows:
        d = "{} {}".format(r.get("receive_order_delivery_id"), r.get("receive_order_delivery_name"))
        g = per.setdefault(d, {"件数": 0})
        g["件数"] += 1
        for f in alive:
            if f in base:
                continue
            v = r.get(f)
            key = f.replace("receive_order_", "")
            if f in DELIVERY_PRESENCE:
                if v not in (None, "", "0", "0.0"):
                    g.setdefault(key + "（入っている件数）", 0)
                    g[key + "（入っている件数）"] += 1
                continue
            c = g.setdefault(key, {})
            c[str(v)] = c.get(str(v), 0) + 1
    out = {"ok": True, "since": since, "fields_ng": dead, "by_delivery": per}
    try:
        out["delivery_master"] = budget.call("api_v1_system_delivery/info").get("data")
    except Exception as exc:  # noqa: BLE001
        out["delivery_master_error"] = str(exc)[:200]
    try:
        out["forwarding_agent"] = search_all(
            budget, "api_v1_receiveorder_forwardingagent/search",
            ["forwarding_agent_id", "forwarding_agent_type", "forwarding_agent_type_id",
             "forwarding_agent_type_name", "forwarding_agent_deleted_flag"], {})
    except Exception as exc:  # noqa: BLE001
        out["forwarding_agent_error"] = str(exc)[:200]
    return out


# 送り状のCSVを NE API から組み立てるための項目（便種・個口の変更で、NE画面の配送情報出力の代わり）。
# 氏名・住所を含むので、結果はドライブ（weekend_ship・本人のアカウント）にだけ置き、ログには出さない
CSV_FIELDS = [
    "receive_order_id", "receive_order_shop_id", "receive_order_delivery_id", "receive_order_invoice_id",
    "receive_order_send_plan_date", "receive_order_hope_delivery_date", "receive_order_hope_delivery_time_slot_id",
    "receive_order_temperature_id", "receive_order_delivery_cut_form_id", "receive_order_delivery_cut_form_note",
    "receive_order_total_amount", "receive_order_payment_method_id", "receive_order_payment_method_name",
    "receive_order_consignee_name", "receive_order_consignee_kana", "receive_order_consignee_tel",
    "receive_order_consignee_zip_code", "receive_order_consignee_address1", "receive_order_consignee_address2",
    "receive_order_purchaser_name", "receive_order_purchaser_tel", "receive_order_purchaser_zip_code",
    "receive_order_purchaser_address1", "receive_order_purchaser_address2",
    "receive_order_order_status_id", "receive_order_last_modified_date",
    # 発送方法をまたぐ変更で一緒に直す項目（rules.cross_carrier_fields が見る）
    "receive_order_delivery_method_id", "receive_order_business_office_stop_id",
    "receive_order_seal1_id", "receive_order_seal2_id", "receive_order_seal3_id", "receive_order_seal4_id",
    "receive_order_cancel_type_id", "receive_order_worker_text",
]


def job_csvsrc(job, budget):
    """伝票番号を指定して、送り状CSVの元になる項目と明細を返す（読むだけ）。"""
    ids = [str(x) for x in job.get("order_ids") or []]
    if not ids:
        return {"ok": False, "error": "order_ids がありません"}
    alive, rows, dead = _alive_fields(budget, CSV_FIELDS, {"receive_order_id-in": ",".join(ids)})
    lines = search_all(budget, ROW_EP, ROW_FIELDS, {"receive_order_row_receive_order_id-in": ",".join(ids),
                                                    "receive_order_row_cancel_flag-eq": "0"})
    return {"ok": True, "fields_ng": dead, "orders": rows, "rows": lines}


def job_history(job, budget):
    """便種・個口の変更のチェック用：指定の商品を含む、過去の出荷済み伝票の 商品と数・発送方法・個口（読むだけ）。
    個口は発送伝票備考欄（数字。空なら1個口）。氏名・住所は取らない。"""
    goods = [str(g) for g in job.get("goods_ids") or []][:30]
    if not goods:
        return {"ok": False, "error": "goods_ids がありません"}
    since = (_now() - datetime.timedelta(days=int(job.get("days") or 365))).strftime("%Y-%m-%d")
    hit = search_all(budget, ROW_EP, ["receive_order_row_receive_order_id"],
                     {"receive_order_row_goods_id-in": ",".join(goods), "receive_order_row_cancel_flag-eq": "0",
                      "receive_order_row_last_modified_date-gte": since})
    ids = sorted({str(r["receive_order_row_receive_order_id"]) for r in hit}, key=int, reverse=True)[:600]
    orders, lines = [], []
    for i in range(0, len(ids), 200):
        chunk = ",".join(ids[i:i + 200])
        orders += search_all(budget, BASE_EP, ["receive_order_id", "receive_order_delivery_id",
                                               "receive_order_delivery_cut_form_note", "receive_order_send_date"],
                             {"receive_order_id-in": chunk, "receive_order_order_status_id-eq": "50"})
        lines += search_all(budget, ROW_EP, ["receive_order_row_receive_order_id", "receive_order_row_goods_id",
                                             "receive_order_row_quantity"],
                            {"receive_order_row_receive_order_id-in": chunk, "receive_order_row_cancel_flag-eq": "0"})
    items = {}
    for r in lines:
        d = items.setdefault(str(r["receive_order_row_receive_order_id"]), {})
        g = str(r["receive_order_row_goods_id"])
        d[g] = d.get(g, 0) + int(float(r.get("receive_order_row_quantity") or 0))
    past = [{"order_id": str(o["receive_order_id"]), "send_date": o.get("receive_order_send_date"),
             "delivery_id": str(o.get("receive_order_delivery_id")),
             "note": o.get("receive_order_delivery_cut_form_note") or "",
             "items": items.get(str(o["receive_order_id"]), {})} for o in orders]
    return {"ok": True, "since": since, "past": past}


def job_exit(job, budget):
    """PC が「今日はもう用が無い」と言ったら待ち受けを終える（延命バッチなどを待たせない）。"""
    return {"ok": True, "exit": True}


def job_shipped_list(job, budget):
    """指定日に出荷確定した伝票（受注状態50・出荷確定日=その日）の番号まわりだけ（読むだけ・氏名や住所は取らない）。
    「出荷したのに番号が入っていない」伝票を見つけるため。"""
    day = str(job.get("day") or _now().strftime("%Y-%m-%d"))
    fields = ["receive_order_id", "receive_order_shop_id", "receive_order_delivery_id", "receive_order_delivery_name",
              "receive_order_delivery_cut_form_id", "receive_order_send_date", "receive_order_order_status_id",
              "receive_order_cancel_type_id", "receive_order_last_modified_date"]
    rows = search_all(budget, BASE_EP, fields, {"receive_order_order_status_id-eq": "50",
                                                  "receive_order_send_date-gte": day + " 00:00:00",
                                                  "receive_order_send_date-lte": day + " 23:59:59"})
    return {"ok": True, "day": day, "orders": rows}


# 確認待ちの判定に使う項目（読むだけ。氏名・住所・電話は取らない）
CONFIRM_FIELDS = ["receive_order_id", "receive_order_shop_id", "receive_order_shop_cut_form_id", "receive_order_date", "receive_order_order_status_id",
                  "receive_order_order_status_name", "receive_order_confirm_check_id", "receive_order_confirm_ids",
                  "receive_order_important_check_id", "receive_order_important_check_name",
                  "receive_order_goods_amount", "receive_order_delivery_fee_amount", "receive_order_total_amount",
                  "receive_order_delivery_id", "receive_order_delivery_name", "receive_order_worker_text",
                  "receive_order_gruoping_tag", "receive_order_payment_method_name", "receive_order_cancel_type_id",
                  "receive_order_last_modified_date"]
CONFIRM_ROW_FIELDS = ["receive_order_row_receive_order_id", "receive_order_row_no", "receive_order_row_goods_id",
                      "receive_order_row_goods_name", "receive_order_row_quantity", "receive_order_row_unit_price",
                      "receive_order_row_cancel_flag"]


def job_confirm_peek(job, budget):
    """確認待ちの伝票の、確認内容・金額・明細（読むだけ）。order_ids か、status（受注状態）で指定。
    確認内容のマスタ（ID→名前）も返す（取れなければ空）。"""
    ids = [str(x) for x in job.get("order_ids") or []]
    if ids:
        flt = {"receive_order_id-in": ",".join(ids)}
    elif job.get("worker_like"):   # 作業用欄の文字で（例 ロミジュリ）。since 以降の受注日
        flt = {"receive_order_worker_text-like": "%{}%".format(job["worker_like"]),
               "receive_order_date-gte": str(job.get("since") or "2026-08-01")}
    else:
        flt = {"receive_order_order_status_id-in": str(job.get("status") or "2"), "receive_order_cancel_type_id-eq": "0"}
    alive, rows, dead = _alive_fields(budget, CONFIRM_FIELDS, flt)
    oids = [str(r["receive_order_id"]) for r in rows][:500]
    lines = search_all(budget, ROW_EP, CONFIRM_ROW_FIELDS, {"receive_order_row_receive_order_id-in": ",".join(oids)}) if oids else []
    # 明細の商品の仕入先コード（同じ取引先の直送どうしの判定用。商品コードの先頭はぶれることがあるので使わない）
    gids = sorted({x["receive_order_row_goods_id"] for x in lines if x.get("receive_order_row_goods_id")})
    goods = []
    for i in range(0, len(gids), 200):
        part = gids[i:i + 200]
        _, g, gdead = _alive_fields_ep(budget, "api_v1_master_goods/search",
                                       ["goods_id", "goods_supplier_id", "goods_merchandise_name", "goods_type_id",
                                        "goods_type_name"], {"goods_id-in": ",".join(part)})
        goods.extend(g)
    return {"ok": True, "fields_ng": dead, "orders": rows, "rows": lines, "goods": goods}


# 起票済み（受注状態2）の伝票が、なぜ印刷待ちに進んでいないか（待つ理由）を見るための項目（読むだけ）
WAIT_FIELDS = ["receive_order_id", "receive_order_shop_id", "receive_order_date", "receive_order_order_status_id",
               "receive_order_confirm_check_id", "receive_order_confirm_ids", "receive_order_cancel_type_id",
               "receive_order_deposit_type_id", "receive_order_deposit_type_name", "receive_order_deposit_date",
               "receive_order_payment_method_id", "receive_order_payment_method_name",
               "receive_order_statement_delivery_instruct_printing_date", "receive_order_send_plan_date",
               "receive_order_gruoping_tag", "receive_order_worker_text", "receive_order_last_modified_date"]
WAIT_ROW_FIELDS = ["receive_order_row_receive_order_id", "receive_order_row_no", "receive_order_row_goods_id",
                   "receive_order_row_quantity", "receive_order_row_stock_allocation_quantity",
                   "receive_order_row_stock_allocation_date", "receive_order_row_cancel_flag"]


def job_wait_reasons(job, budget):
    """起票済み（または order_ids の）伝票の、入金・引当・印刷指示日・確認の状態（読むだけ・氏名や住所は取らない）。"""
    ids = [str(x) for x in job.get("order_ids") or []]
    flt = ({"receive_order_id-in": ",".join(ids)} if ids else
           {"receive_order_order_status_id-eq": "2", "receive_order_cancel_type_id-eq": "0"})
    alive, rows, dead = _alive_fields(budget, WAIT_FIELDS, flt)
    oids = [str(r["receive_order_id"]) for r in rows][:1000]
    lines, rdead = [], []
    for i in range(0, len(oids), 200):
        _, part, rdead = _alive_fields_ep(budget, ROW_EP, WAIT_ROW_FIELDS,
                                          {"receive_order_row_receive_order_id-in": ",".join(oids[i:i + 200])})
        lines += part
    return {"ok": True, "fields_ng": dead + rdead, "orders": rows, "rows": lines}


def job_confirm_stats(job, budget):
    """since 以降の受注の、確認内容ID・作業用欄・店舗・受注状態（読むだけ・氏名や住所は取らない）と、
    確認内容の定義一覧（api_v1_receiveorder_confirm/search）。どの確認待ちが多いかを数えるため。"""
    since = str(job.get("since") or "2026-10-01")
    fields = ["receive_order_id", "receive_order_shop_id", "receive_order_date", "receive_order_order_status_id",
              "receive_order_cancel_type_id", "receive_order_confirm_ids", "receive_order_confirm_check_id",
              "receive_order_worker_text", "receive_order_gruoping_tag"]
    _, rows, dead = _alive_fields(budget, fields, {"receive_order_date-gte": since})
    master = []
    try:
        _, master, _ = _alive_fields_ep(budget, "api_v1_receiveorder_confirm/search",
                                        ["confirm_id", "confirm_name", "confirm_valid_flag"], {})
    except Exception as exc:  # noqa: BLE001
        master = [{"_error": str(exc)[:200]}]
    return {"ok": True, "since": since, "fields_ng": dead, "orders": rows, "confirm_master": master}


def job_dokon_peek(job, budget):
    """「同梱可能な受注が存在します」（確認内容CT）が付いた起票済みの伝票（読むだけ）。
    同じお客様の組を見分けるため、届け先（氏名・郵便番号・住所・電話）と購入者は、ここでハッシュにしてから返す
    （氏名・住所そのものはドライブの結果ファイルに出さない）。"""
    import hashlib
    fields = ["receive_order_id", "receive_order_shop_id", "receive_order_date", "receive_order_order_status_id",
              "receive_order_cancel_type_id", "receive_order_confirm_ids", "receive_order_confirm_check_id",
              "receive_order_deposit_type_id", "receive_order_payment_method_name", "receive_order_worker_text",
              "receive_order_last_modified_date",
              "receive_order_consignee_name", "receive_order_consignee_zip_code", "receive_order_consignee_address1",
              "receive_order_consignee_address2", "receive_order_consignee_tel", "receive_order_purchaser_name"]
    ids = [str(x) for x in job.get("order_ids") or []]
    flt = ({"receive_order_id-in": ",".join(ids)} if ids else
           {"receive_order_order_status_id-eq": "2", "receive_order_cancel_type_id-eq": "0",
            "receive_order_confirm_ids-like": "%CT%"})
    _, rows, dead = _alive_fields(budget, fields, flt)

    def norm(v):
        return "".join(str(v or "").split()).replace("-", "").replace("ー", "")

    out = []
    for r in rows:
        key = "|".join(norm(r.get(k)) for k in ("receive_order_consignee_name", "receive_order_consignee_zip_code",
                                                  "receive_order_consignee_address1", "receive_order_consignee_address2",
                                                  "receive_order_consignee_tel"))
        x = {k: v for k, v in r.items() if "consignee" not in k and "purchaser" not in k}
        x["consignee_key"] = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        x["purchaser_key"] = hashlib.sha256(norm(r.get("receive_order_purchaser_name")).encode("utf-8")).hexdigest()[:16]
        out.append(x)
    return {"ok": True, "fields_ng": dead, "orders": out}


UPLOAD_EP = "api_v1_receiveorder_base/upload"
QUE_EP = "api_v1_system_que/search"


def job_order_prep(job, budget):
    """イレギュラーな受注を API で起票する前の下調べ（読むだけ）。
    受注一括登録パターン・店舗・支払方法・発送方法の一覧と、goods_like に当たる商品を返す。"""
    out = {"ok": True}
    pats = budget.call("api_v1_receiveorder_uploadpattern/info").get("data") or []
    out["patterns"] = [{k.replace("receive_order_upload_pattern_", ""): p.get(k) for k in
                        ("receive_order_upload_pattern_id", "receive_order_upload_pattern_name",
                         "receive_order_upload_pattern_format_id", "receive_order_upload_pattern_shop_id",
                         "receive_order_upload_pattern_deleted_flag")} for p in pats]
    out["shops"] = search_all(budget, "api_v1_master_shop/search",
                              ["shop_id", "shop_name", "shop_deleted_flag"], {})
    for key, ep in (("payments", "api_v1_system_paymentmethod/info"), ("deliveries", "api_v1_system_delivery/info")):
        try:
            out[key] = budget.call(ep).get("data") or []
        except Exception as exc:  # noqa: BLE001
            out[key] = {"error": str(exc)[:200]}
    goods = []
    for like in job.get("goods_like") or []:
        for col in ("goods_name", "goods_id"):
            goods += search_all(budget, "api_v1_master_goods/search",
                                ["goods_id", "goods_name", "goods_selling_price", "goods_deleted_flag"],
                                {col + "-like": like})
    out["goods"] = list({g["goods_id"]: g for g in goods}.values())
    return out


def job_order_upload(job, budget):
    """受注CSV（受注一括登録パターンの形式）を1回アップロードし、取込の結果を待って、
    店舗伝票番号で起票された伝票を読み直して返す。同じ店舗伝票番号が既にあれば上げない（二重起票しない）。"""
    cut = str(job.get("shop_cut_form_id") or "").strip()
    fields = ["receive_order_id", "receive_order_shop_id", "receive_order_shop_cut_form_id",
              "receive_order_order_status_name", "receive_order_worker_text", "receive_order_total_amount",
              "receive_order_delivery_name", "receive_order_payment_method_name",
              "receive_order_hope_delivery_date", "receive_order_hope_delivery_time_slot_name",
              "receive_order_cancel_type_id"]

    def find():
        if not cut:
            return []
        _, rows, _ = _alive_fields(budget, fields, {"receive_order_shop_cut_form_id-eq": cut})
        return rows

    before = find()
    if before:
        return {"ok": False, "error": "exists", "detail": "同じ店舗伝票番号の伝票が既にあります", "orders": before}
    if job.get("dry_run"):
        return {"ok": True, "dry_run": True, "orders": []}
    res = budget.call(UPLOAD_EP, {"receive_order_upload_pattern_id": str(job["pattern_id"]),
                                  "data_type_1": "csv", "data_1": job["csv"], "wait_flag": "1"})
    que = str(res.get("que_id"))
    hist = []
    for _ in range(40):          # 最大約10分
        time.sleep(15)
        rows = budget.call(QUE_EP, {"fields": "que_id,que_status_id,que_message",
                                    "que_id-eq": que}).get("data") or []
        st = rows[0] if rows else {}
        hist.append(st)
        if str(st.get("que_status_id")) in ("2", "-1"):
            break
    last = hist[-1] if hist else {}
    orders = find()
    return {"ok": str(last.get("que_status_id")) == "2" and bool(orders), "que_id": que,
            "que": last, "orders": orders}


JOBS = {"order_prep": job_order_prep, "order_upload": job_order_upload,
        "ping": job_ping, "snapshot": job_snapshot, "apply": job_apply, "ship": job_ship,
        "holds": job_holds, "inspect": job_inspect, "fields": job_fields, "csvsrc": job_csvsrc, "history": job_history, "shipped_list": job_shipped_list, "confirm_peek": job_confirm_peek, "wait_reasons": job_wait_reasons, "confirm_stats": job_confirm_stats, "dokon_peek": job_dokon_peek, "exit": job_exit}


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
    return bool(out.get("exit"))


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
                stop = run_job(job)
                handled += 1
                last_job = time.time()
                if stop:
                    _write_json("ws_ne_alive.json", {"at": _now().isoformat(timespec="seconds"), "ended": True,
                                                     "handled": handled, "by": "exit"})
                    print("[weekend_ship] PCの指示で待ち受けを終えます handled={}".format(handled), flush=True)
                    return 0
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
