# -*- coding: utf-8 -*-
"""
NE商品マスタの更新（api_v1_master_goods/upload）と参照（search）。

アップロードCSVの列名は**英語フィールド名**（NE画面DLの日本語ヘッダとは別物）:
  syohin_code=商品コード / location=ロケーション / org1=項目1 / baika_tnk=売価 / genka_tnk=原価
部分更新OK: syohin_code＋更新したい列だけのCSVでよい。
空値の挙動は未定義のため「空値を送らない」（全行同一キー・値必須をここで強制する）。

uploadは非同期でque_idが返る → api_v1_system_que/search で完了(-1=失敗/2=完了)を確認する。
"""
import csv
import io
import time

from . import client


def build_csv(rows):
    """rows: [{フィールド名: 値}] → NEアップロード用CSV文字列。
    全行が同一のキー集合であること・空値が無いことを検証する（空値を送らない設計）。"""
    if not rows:
        raise ValueError("アップロードする行がありません")
    fields = list(rows[0].keys())
    if "syohin_code" not in fields:
        raise ValueError("syohin_code（商品コード）が必要です")
    for r in rows:
        if list(r.keys()) != fields:
            raise ValueError(f"行ごとに列が違います: {list(r.keys())} != {fields}")
        for k, v in r.items():
            if str(v).strip() == "":
                raise ValueError(f"空値は送れません（{r.get('syohin_code')} の {k}）")
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, lineterminator="\r\n")
    w.writeheader()
    w.writerows({k: str(v) for k, v in r.items()} for r in rows)
    return buf.getvalue()


def upload_goods(rows):
    """商品マスタの部分更新CSVをアップロードし、que_id を返す（反映は非同期）。"""
    data = build_csv(rows)
    result = client.call("api_v1_master_goods/upload",
                         {"data_type": "csv", "data": data, "wait_flag": "1"})
    que_id = str(result.get("que_id", "")).strip()
    if not que_id:
        raise client.NEError(f"que_id が取得できませんでした: {result}")
    return que_id


def que_status(que_id):
    """アップロードキューの状態を返す: (status_id:int|None, message:str)。"""
    result = client.call("api_v1_system_que/search",
                         {"que_id-eq": str(que_id),
                          "fields": "que_id,que_status_id,que_message"})
    data = result.get("data") or []
    if not data:
        return None, f"キュー {que_id} が見つかりません"
    row = data[0]
    try:
        status = int(row.get("que_status_id"))
    except (TypeError, ValueError):
        status = None
    return status, str(row.get("que_message") or "")


def wait_que(que_id, timeout=120, interval=3):
    """キュー完了までポーリングする。返り値: (成功:bool, メッセージ:str)。
    que_status_id: 0=待機 / 1=処理中 / 2=完了 / -1=失敗（理由は que_message）"""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        status, message = que_status(que_id)
        last = (status, message)
        if status == 2:
            return True, message or "完了"
        if status == -1:
            return False, message or "NE側で処理が失敗しました"
        time.sleep(interval)
    return False, (f"タイムアウト（{timeout}秒）: 状態={last}。"
                   f"NE側で処理中の可能性があります（キューID {que_id} をNE画面で確認してください）")


def search_goods(codes, fields="goods_id,goods_name"):
    """商品コードで商品マスタを検索する（接続テスト・更新後検証用）。"""
    result = client.call("api_v1_master_goods/search",
                         {"goods_id-in": ",".join(str(c) for c in codes),
                          "fields": fields, "limit": str(max(len(codes), 1))})
    return result.get("data") or []


def find_existing(codes):
    """商品コード群がNEに存在するか確認し、{入力コード小文字: NEの正確な商品コード} を返す。

    商品マスタuploadは「商品コードが既存と一致すれば更新（商品コードのみ必須）／
    一致しなければ新規登録（売価・原価等が必須）」で動く。マスタ(Drive)とNEで
    商品コードの大文字小文字や表記がずれていると新規登録扱いになりエラーになるため、
    事前にNEへ問い合わせて存在確認し、NEが実際に持つ正確なコードへ置き換える。
    """
    uniq = []
    seen = set()
    for c in codes:
        s = str(c).strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            uniq.append(s)
    found = {}
    for i in range(0, len(uniq), 100):     # NEのsearchは一括取得できる（100件ずつ）
        chunk = uniq[i:i + 100]
        result = client.call("api_v1_master_goods/search",
                             {"goods_id-in": ",".join(chunk),
                              "fields": "goods_id", "limit": str(len(chunk))})
        for row in (result.get("data") or []):
            gid = str(row.get("goods_id", "")).strip()
            if gid:
                found[gid.lower()] = gid
    return found
