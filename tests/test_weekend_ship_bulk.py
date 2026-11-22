# -*- coding: utf-8 -*-
"""NE担当が伝票の更新・出荷確定をまとめて送ること（2026-11-22 無料枠超えの対策）のテスト。
偽のNEで、呼び出し回数・一部失敗・まるごと失敗のときの戻り道・読み直しを確かめる。通信しない。"""
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from batch import weekend_ship_ne as w  # noqa: E402
from lib.ne_api import client  # noqa: E402


class FakeNE:
    """伝票を持ち、search / update / bulkupdate / shipped / bulkupdateshipped に答える偽のNE。"""

    def __init__(self, orders, bulk_broken=False, fail_ids=(), touch_once=()):
        self.o = {k: dict(v) for k, v in orders.items()}
        self.calls = []
        self.bulk_broken = bulk_broken          # まとめての送信をまるごと断る（形式違いの想定）
        self.fail_ids = set(fail_ids)           # この伝票だけ失敗させる
        self.touch_once = set(touch_once)       # 1回目だけ「読んだ後に更新された」にする
        self.n = 0

    def _bump(self, oid):
        self.n += 1
        self.o[oid]["receive_order_last_modified_date"] = "2026-11-22 12:00:%02d" % self.n

    def call(self, ep, params=None):
        self.calls.append(ep)
        params = params or {}
        if ep == w.BASE_EP:
            ids = params["receive_order_id-in"].split(",")
            return {"result": "success", "data": [dict(self.o[i]) for i in ids if i in self.o]}
        if ep == w.BULK_UPDATE_EP:
            if self.bulk_broken:
                raise client.NEError("NE APIエラー（bulkupdate）: 020001 data が不正です")
            errs = []
            for m in re.finditer(r'<receiveorder receive_order_id="(\d+)" receive_order_last_modified_date="([^"]+)">'
                                 r'<receiveorder_base>(.*?)</receiveorder_base>', params["data"], re.S):
                oid, lm, body = m.groups()
                o = self.o[oid]
                if oid in self.touch_once:
                    self.touch_once.discard(oid)
                    self._bump(oid)
                    errs.append('{"receive_order_id":"%s","code":"020006","message":"更新されています"}' % oid)
                    continue
                if oid in self.fail_ids or lm != o["receive_order_last_modified_date"]:
                    errs.append('{"receive_order_id":"%s","code":"020099","message":"だめ"}' % oid)
                    continue
                if o["receive_order_order_status_id"] == "50" and params.get("receive_order_shipped_update_flag") != "1":
                    errs.append('{"receive_order_id":"%s","code":"020010","message":"出荷確定済"}' % oid)
                    continue
                for k, v in re.findall(r"<(\w+)>(.*?)</\1>", body, re.S):
                    o[k] = v.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
                self._bump(oid)
            if errs:
                raise client.NEError("NE APIエラー（bulkupdate）: 020500 [" + ",".join(errs) + "]")
            return {"result": "success"}
        if ep == w.UPDATE_EP:
            oid = params["receive_order_id"]
            body = re.search(r"<receiveorder_base>(.*)</receiveorder_base>", params["data"], re.S).group(1)
            for k, v in re.findall(r"<(\w+)>(.*?)</\1>", body, re.S):
                self.o[oid][k] = v
            self._bump(oid)
            return {"result": "success"}
        if ep == w.BULK_SHIPPED_EP:
            if self.bulk_broken:
                raise client.NEError("NE APIエラー（bulkupdateshipped）: 031001 data が不正です")
            errs = []
            for oid, lm in re.findall(r'receive_order_id="(\d+)" receive_order_last_modified_date="([^"]+)"',
                                      params["data"]):
                if oid in self.fail_ids:
                    errs.append('{"receive_order_id":"%s","code":"031009","message":"条件を満たしていない"}' % oid)
                    continue
                self.o[oid]["receive_order_order_status_id"] = "50"
                self._bump(oid)
            if errs:
                raise client.NEError("NE APIエラー（bulkupdateshipped）: 031500 [" + ",".join(errs) + "]")
            return {"result": "success"}
        if ep == w.SHIPPED_EP:
            oid = params["receive_order_id"]
            self.o[oid]["receive_order_order_status_id"] = "50"
            self._bump(oid)
            return {"result": "success"}
        raise AssertionError(ep)


def _orders(n, status="20"):
    return {str(200000 + i): {"receive_order_id": str(200000 + i),
                              "receive_order_last_modified_date": "2026-11-22 09:00:00",
                              "receive_order_order_status_id": status,
                              "receive_order_worker_text": "前のメモ", "receive_order_gruoping_tag": "",
                              "receive_order_delivery_cut_form_id": "", "receive_order_cancel_type_id": "0"}
            for i in range(n)}


@pytest.fixture
def ne(monkeypatch):
    holder = {}

    def use(fake):
        holder["ne"] = fake
        monkeypatch.setattr(w.client, "call", fake.call)
        monkeypatch.setattr(w.time, "sleep", lambda s: None)
        return fake
    return use


def _apply_ops(orders):
    return [{"order_id": oid, "last_modified": o["receive_order_last_modified_date"],
             "set": {"receive_order_delivery_id": "20"}, "prepend_worker_text": "【締め】"} for oid, o in orders.items()]


def test_apply_40_orders_in_one_update_call(ne):
    orders = _orders(40)
    fake = ne(FakeNE(orders))
    out = w.job_apply({"ops": _apply_ops(orders)}, w.Budget(400))
    assert out["ok"]
    assert fake.calls.count(w.BULK_UPDATE_EP) == 1 and w.UPDATE_EP not in fake.calls
    assert len(fake.calls) == 2      # 読む1回＋まとめて書く1回（1件ずつだと41回）
    o = fake.o["200000"]
    assert o["receive_order_worker_text"] == "【締め】\n前のメモ"     # 上書きせず先頭に足す
    assert o["receive_order_delivery_id"] == "20"


def test_apply_shipped_orders_go_with_flag(ne):
    orders = _orders(2)
    orders.update({k: dict(v, receive_order_order_status_id="50") for k, v in _orders(3).items() if k > "200001"})
    fake = ne(FakeNE(orders))
    out = w.job_apply({"ops": _apply_ops(orders)}, w.Budget(400))
    assert out["ok"], out
    assert fake.calls.count(w.BULK_UPDATE_EP) == 2      # 出荷確定済みは旗つきで別に送る


def test_apply_partial_failure_reports_only_that_order(ne):
    orders = _orders(5)
    fake = ne(FakeNE(orders, fail_ids={"200002"}))
    out = w.job_apply({"ops": _apply_ops(orders)}, w.Budget(400))
    bad = [r["order_id"] for r in out["results"] if not r["ok"]]
    assert bad == ["200002"]
    assert fake.o["200003"]["receive_order_worker_text"].startswith("【締め】")


def test_apply_retries_order_touched_after_read(ne):
    orders = _orders(3)
    fake = ne(FakeNE(orders, touch_once={"200001"}))
    out = w.job_apply({"ops": _apply_ops(orders)}, w.Budget(400))
    assert out["ok"], out
    assert fake.o["200001"]["receive_order_worker_text"] == "【締め】\n前のメモ"   # 二重に書かない


def test_apply_falls_back_to_one_by_one_when_bulk_is_rejected(ne):
    orders = _orders(3)
    fake = ne(FakeNE(orders, bulk_broken=True))
    out = w.job_apply({"ops": _apply_ops(orders)}, w.Budget(400))
    assert out["ok"], out
    assert fake.calls.count(w.UPDATE_EP) == 3
    assert all(r.get("way") == "one" for r in out["results"])


def test_apply_skips_order_changed_since_pc_read(ne):
    orders = _orders(2)
    fake = ne(FakeNE(orders))
    ops = _apply_ops(orders)
    ops[0]["last_modified"] = "2026-11-22 08:00:00"
    out = w.job_apply({"ops": ops}, w.Budget(400))
    assert out["results"][0]["error"] == "changed" and out["results"][1]["ok"]


def test_ship_40_orders_in_few_calls(ne):
    orders = _orders(40, status="40")
    fake = ne(FakeNE(orders))
    ops = [{"order_id": oid, "tracking": "4373%08d" % i} for i, oid in enumerate(orders)]
    out = w.job_ship({"ops": ops}, w.Budget(400))
    assert out["ok"], out
    # 読む・番号をまとめて入れる・読み直す・まとめて出荷確定・確かめる＝5回（1件ずつだと約120回）
    assert len(fake.calls) == 5
    assert all(o["receive_order_order_status_id"] == "50" for o in fake.o.values())


def test_ship_number_only_for_already_shipped(ne):
    orders = _orders(2, status="50")
    fake = ne(FakeNE(orders))
    ops = [{"order_id": oid, "tracking": "4373000%d" % i} for i, oid in enumerate(orders)]
    out = w.job_ship({"ops": ops}, w.Budget(400))
    assert out["ok"], out
    assert w.BULK_SHIPPED_EP not in fake.calls       # 出荷確定し直さない
    assert all(r.get("number_only") for r in out["results"])


def test_ship_partial_failure(ne):
    orders = _orders(4, status="40")
    fake = ne(FakeNE(orders, fail_ids={"200001"}))
    # 番号の書き込みでも 200001 が落ちる（fail_ids は両方に効く）→ 出荷確定まで進むのは残り3件
    ops = [{"order_id": oid, "tracking": "4373000%d" % i} for i, oid in enumerate(orders)]
    out = w.job_ship({"ops": ops}, w.Budget(400))
    bad = [r["order_id"] for r in out["results"] if not r["ok"]]
    assert bad == ["200001"]
    assert fake.o["200000"]["receive_order_order_status_id"] == "50"


def test_ship_falls_back_when_bulk_rejected(ne):
    orders = _orders(3, status="40")
    fake = ne(FakeNE(orders, bulk_broken=True))
    ops = [{"order_id": oid, "tracking": "4373000%d" % i} for i, oid in enumerate(orders)]
    out = w.job_ship({"ops": ops}, w.Budget(400))
    assert out["ok"], out
    assert fake.calls.count(w.SHIPPED_EP) == 3 and fake.calls.count(w.UPDATE_EP) == 3


def test_bulk_xml_escapes_and_rejects():
    xml = w.bulk_update_xml([{"order_id": "1", "last_modified": "2026-11-22 09:00:00",
                              "fields": {"receive_order_worker_text": "<a>&\""}}])
    assert '<receiveorder receive_order_id="1" receive_order_last_modified_date="2026-11-22 09:00:00">' in xml
    assert "&lt;a&gt;&amp;" in xml
    with pytest.raises(ValueError):
        w.bulk_update_xml([{"order_id": "1", "last_modified": "x", "fields": {"receive_order_consignee_name": "x"}}])


def test_apply_falls_back_when_every_order_is_named(ne):
    """全件が名指しで失敗したら（送り方の読み違いの疑い）、1件ずつに戻す。朝の締めを全件エラーで止めない。"""
    orders = _orders(3)
    fake = ne(FakeNE(orders, fail_ids=set(orders)))
    fake.fail_ids = set()          # 1件ずつなら通る

    real = fake.call

    def call(ep, params=None):
        if ep == w.BULK_UPDATE_EP:
            fake.calls.append(ep)
            raise client.NEError("NE APIエラー: 020500 [" + ",".join(
                '{"receive_order_id":"%s","code":"020001"}' % o for o in orders) + "]")
        return real(ep, params)
    w.client.call = call
    out = w.job_apply({"ops": _apply_ops(orders)}, w.Budget(400))
    assert out["ok"], out
    assert all(r.get("way") == "one" for r in out["results"])
