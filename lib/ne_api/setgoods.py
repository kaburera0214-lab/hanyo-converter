# -*- coding: utf-8 -*-
"""
NEセット商品マスタの登録（api_v1_master_setgoods/upload）と参照（search）。

【なぜAPIか】
  ネクストエンジンは利用規約（第1編 第24条1項(17)・2026-10-05改定）で、AIや自動化ツールによる
  画面の操作を禁じている。公式のAPIは対象外。セット商品の登録は画面ではなくここから行う。

【公式の説明で分かっていること】（2026-10-09 確認）
  https://developer.next-engine.com/api/api_v1_master_setgoods/upload
  - アップロードCSVの列名は英語。公式のサンプルにあるのは次の5列:
      set_syohin_code=セット商品コード / set_syohin_name=セット商品名 / set_baika_tnk=セット販売価格
      syohin_code=内訳の商品コード / suryo=数量
  - 1行＝内訳1つ。同じセット商品コードの行を内訳の数だけ並べる
  - 反映は非同期（que_id が返る → api_v1_system_que/search で結果を見る）

【公式の説明に無いこと → 送ったあと必ず確かめる】
  - 代表商品コードの列。商品マスタのCSVと同じ daihyo_syohin_code を付けて送り、
    登録後に search の set_goods_representation_id で入ったかを見る（verify）
  - すでにあるセット商品コードを送ったときの動き（上書きか追加か）。
    分からないので **すでにあるコードは送らない**（plan が「登録済み」に分けて外す）

【セット商品コードの決まり】（NEのマニュアル「セット商品マスタの登録(CSV)」）
  半角英数字と半角ハイフンだけ・30字以内。商品コードと同じ値は使えない。
"""
import csv
import io
import re

from . import client, goods

# 計画CSV（人が読む日本語の見出し）→ NEのアップロード列
JP_COLUMNS = {
    "セット商品コード": "set_syohin_code",
    "セット商品名": "set_syohin_name",
    "セット商品販売価格": "set_baika_tnk",
    "商品コード": "syohin_code",
    "数量": "suryo",
    "代表商品コード": "daihyo_syohin_code",
}
REQUIRED = ["セット商品コード", "セット商品名", "セット商品販売価格", "商品コード", "数量"]
UPLOAD_COLUMNS = ["set_syohin_code", "syohin_code", "suryo", "set_baika_tnk", "set_syohin_name"]  # 公式サンプルの並び
DAIHYO_COLUMN = "daihyo_syohin_code"

SET_CODE = re.compile(r"^[A-Za-z0-9-]{1,30}$")
SEARCH_CHUNK = 300
SEARCH_FIELDS = ("set_goods_id,set_goods_name,set_goods_selling_price,"
                 "set_goods_detail_goods_id,set_goods_detail_quantity,set_goods_representation_id")

# plan の区分
NEW = "登録する"
EXISTS = "登録済み（送らない）"
MISSING_GOODS = "内訳の商品がNEに無い"
CODE_CONFLICT = "商品コードと同じ値は使えない"


class PlanError(ValueError):
    """計画CSVの中身がおかしい（NEへは何も送らない）。"""


def parse_plan(text):
    """計画CSV（日本語の見出し・1行＝内訳1つ）→ {セット商品コード: {name, price, daihyo, parts}}。

    parts は [(商品コード, 数量:int)]。中身に少しでもおかしな所があれば PlanError
    （分かる分だけ登録すると、内訳の欠けたセットができて在庫の計算が狂う）。
    """
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    header = [h.strip() for h in (reader.fieldnames or [])]
    lack = [c for c in REQUIRED if c not in header]
    if lack:
        raise PlanError(f"見出しが足りません: {lack}")
    sets = {}
    for no, raw in enumerate(reader, start=2):
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        if not any(row.values()):
            continue
        for c in REQUIRED:
            if not row.get(c):
                raise PlanError(f"{no}行目: {c} が空です")
        code = row["セット商品コード"]
        if not SET_CODE.match(code):
            raise PlanError(f"{no}行目: セット商品コードは半角英数字とハイフンだけ・30字以内です（{code}）")
        try:
            qty = int(row["数量"])
            price = int(row["セット商品販売価格"])
        except ValueError:
            raise PlanError(f"{no}行目: 数量とセット商品販売価格は整数で入れてください")
        if qty <= 0 or price < 0:
            raise PlanError(f"{no}行目: 数量は1以上、セット商品販売価格は0以上です")
        entry = sets.setdefault(code, {"name": row["セット商品名"], "price": price,
                                       "daihyo": row.get("代表商品コード", ""), "parts": []})
        same = (entry["name"], entry["price"], entry["daihyo"]) == (
            row["セット商品名"], price, row.get("代表商品コード", ""))
        if not same:
            raise PlanError(f"{no}行目: 同じセット商品コード（{code}）で、名前・価格・代表商品コードが行によって違います")
        if row["商品コード"].lower() in {p.lower() for p, _ in entry["parts"]}:
            raise PlanError(f"{no}行目: セット {code} に同じ商品コード（{row['商品コード']}）が2回あります")
        if row["商品コード"].lower() == code.lower():
            raise PlanError(f"{no}行目: セット商品コードと内訳の商品コードが同じです（{code}）")
        entry["parts"].append((row["商品コード"], qty))
    if not sets:
        raise PlanError("登録するセットが1つもありません")
    return sets


def pick(sets, only):
    """only（セット商品コードの並び）で絞る。空なら全部。無いコードを指定したら PlanError。"""
    if not only:
        return dict(sets)
    wanted = [c.strip() for c in only if c.strip()]
    unknown = [c for c in wanted if c not in sets]
    if unknown:
        raise PlanError(f"計画CSVに無いセット商品コードが指定されています: {unknown}")
    return {c: sets[c] for c in wanted}


def plan(sets, existing_sets, existing_goods):
    """登録の計画を立てる（純関数）。{セット商品コード: (区分, 理由)}。

    existing_sets:  NEにすでにあるセット商品コード（小文字）の集合
    existing_goods: NEにある商品コード（小文字）の集合。セット商品コードと内訳の両方を引いた結果
    """
    out = {}
    for code, entry in sets.items():
        if code.lower() in existing_sets:
            out[code] = (EXISTS, "NEに同じセット商品コードがある")
        elif code.lower() in existing_goods:
            out[code] = (CODE_CONFLICT, "NEの商品マスタに同じコードがある")
        else:
            lack = [p for p, _ in entry["parts"] if p.lower() not in existing_goods]
            out[code] = (MISSING_GOODS, "無い商品コード: " + " ".join(lack)) if lack else (NEW, "")
    return out


def build_csv(sets, with_daihyo=True):
    """NEアップロード用の (CSV文字列, 代表商品コードの列を付けたか)。
    代表商品コードの列は、全セットに値があるときだけ付ける。"""
    if not sets:
        raise PlanError("アップロードするセットがありません")
    use_daihyo = with_daihyo and all(e["daihyo"] for e in sets.values())
    if with_daihyo and not use_daihyo and any(e["daihyo"] for e in sets.values()):
        raise PlanError("代表商品コードが入っているセットと空のセットが混ざっています")
    columns = UPLOAD_COLUMNS + ([DAIHYO_COLUMN] if use_daihyo else [])
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, lineterminator="\r\n")
    writer.writeheader()
    for code, entry in sets.items():
        for goods, qty in entry["parts"]:
            row = {"set_syohin_code": code, "syohin_code": goods, "suryo": str(qty),
                   "set_baika_tnk": str(entry["price"]), "set_syohin_name": entry["name"]}
            if use_daihyo:
                row[DAIHYO_COLUMN] = entry["daihyo"]
            writer.writerow(row)
    return buf.getvalue(), use_daihyo


def upload(csv_text):
    """セット商品マスタCSVをアップロードし、que_id を返す（反映は非同期）。"""
    result = client.call("api_v1_master_setgoods/upload",
                         {"data_type": "csv", "data": csv_text, "wait_flag": "1"})
    que_id = str(result.get("que_id", "")).strip()
    if not que_id:
        raise client.NEError("que_id が取得できませんでした")
    return que_id


def search_sets(codes):
    """セット商品コードでセット商品マスタを引く。{コード小文字: {code, name, price, daihyo, parts{商品コード小文字: 数量}}}。"""
    found = {}
    uniq = list(dict.fromkeys(str(c).strip() for c in codes if str(c).strip()))
    for i in range(0, len(uniq), SEARCH_CHUNK):
        chunk = uniq[i:i + SEARCH_CHUNK]
        rows = client.call("api_v1_master_setgoods/search",
                           {"set_goods_id-in": ",".join(chunk), "fields": SEARCH_FIELDS,
                            "limit": "10000"}).get("data") or []
        for row in rows:
            code = str(row.get("set_goods_id", "")).strip()
            if not code:
                continue
            entry = found.setdefault(code.lower(), {
                "code": code, "name": str(row.get("set_goods_name") or ""),
                "price": _to_int(row.get("set_goods_selling_price")),
                "daihyo": str(row.get("set_goods_representation_id") or "").strip(), "parts": {}})
            goods = str(row.get("set_goods_detail_goods_id") or "").strip()
            if goods:
                entry["parts"][goods.lower()] = _to_int(row.get("set_goods_detail_quantity"))
    return found


def search_goods(codes):
    """商品コードがNEの商品マスタにあるかをまとめて引く。ある商品コード（小文字）の集合を返す。"""
    have = set()
    uniq = list(dict.fromkeys(str(c).strip() for c in codes if str(c).strip()))
    for i in range(0, len(uniq), SEARCH_CHUNK):
        chunk = uniq[i:i + SEARCH_CHUNK]
        rows = client.call("api_v1_master_goods/search",
                           {"goods_id-in": ",".join(chunk), "fields": "goods_id",
                            "limit": str(len(chunk))}).get("data") or []
        have.update(str(r.get("goods_id", "")).strip().lower() for r in rows if r.get("goods_id"))
    return have


def verify(sets, found, check_daihyo=True):
    """登録後に引いた中身（search_sets の結果）が、送ったとおりかを見る（純関数）。

    返り値: {セット商品コード: [食い違いの説明]}。空のリストなら一致。
    名前は見ない（NEが全角・半角を直すことがあり、在庫の計算にも関係しない）。
    """
    out = {}
    for code, entry in sets.items():
        got = found.get(code.lower())
        if not got:
            out[code] = ["NEに見つからない"]
            continue
        diff = []
        want = {p.lower(): q for p, q in entry["parts"]}
        if got["parts"] != want:
            lack = sorted(set(want) - set(got["parts"]))
            extra = sorted(set(got["parts"]) - set(want))
            wrong = sorted(p for p in set(want) & set(got["parts"]) if want[p] != got["parts"][p])
            if lack:
                diff.append("内訳に無い: " + " ".join(lack))
            if extra:
                diff.append("内訳に余分: " + " ".join(extra))
            if wrong:
                diff.append("数量が違う: " + " ".join(wrong))
        if got["price"] != entry["price"]:
            diff.append(f"価格が違う（NE {got['price']}／計画 {entry['price']}）")
        if check_daihyo and entry["daihyo"] and got["daihyo"].lower() != entry["daihyo"].lower():
            diff.append(f"代表商品コードが違う（NE「{got['daihyo']}」／計画「{entry['daihyo']}」）")
        out[code] = diff
    return out


def _to_int(value):
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def execute(sets, apply, with_daihyo=True):
    """計画を実行する（NEを引く → 分ける → apply のときだけ送る → 引き直して照合）。

    返り値: {rows, counts, sent, used_daihyo, que_ok, checked, mismatched, blocked, failed}
      rows は {セット商品コード: [セット商品コード, 区分, 結果, 理由]}（ドライブの結果ファイルにそのまま書く）
    """
    existing = search_sets(sets.keys())
    codes = list(sets.keys()) + [p for e in sets.values() for p, _ in e["parts"]]
    decided = plan(sets, set(existing), search_goods(codes))
    counts = {}
    for kind, _ in decided.values():
        counts[kind] = counts.get(kind, 0) + 1

    todo = {c: sets[c] for c, (kind, _) in decided.items() if kind == NEW}
    blocked = [c for c, (kind, _) in decided.items() if kind in (MISSING_GOODS, CODE_CONFLICT)]
    rows = {c: [c, kind, "", reason] for c, (kind, reason) in decided.items()}
    for c in blocked:
        rows[c][2] = "登録できない"

    que_ok, used_daihyo, sent = True, False, 0
    if not apply:
        for c in todo:
            rows[c][2] = "確認だけ（送っていない）"
    elif todo:
        data, used_daihyo = build_csv(todo, with_daihyo=with_daihyo)
        que_id = upload(data)
        sent = len(todo)
        timeout, interval = goods.wait_policy(sum(len(e["parts"]) for e in todo.values()))
        que_ok, message = goods.wait_que(que_id, timeout=timeout, interval=interval)
        for c in todo:
            rows[c][2] = "NEの処理が完了" if que_ok else "NEの処理が失敗"
            if not que_ok:
                rows[c][3] = message

    # 登録済み（今回送った分を含む）の中身が計画どおりかを、NEから引き直して確かめる
    to_check = {c: sets[c] for c, (kind, _) in decided.items()
                if kind == EXISTS or (apply and que_ok and kind == NEW)}
    mismatched = 0
    if to_check:
        after = search_sets(to_check.keys()) if sent else existing
        for c, diff in verify(to_check, after, check_daihyo=with_daihyo).items():
            head = rows[c][2] + "／" if rows[c][2] else ""
            if diff:
                mismatched += 1
                rows[c][2] = head + "中身が計画と違う"
                rows[c][3] = "; ".join(filter(None, [rows[c][3], *diff]))
            else:
                rows[c][2] = head + "中身は計画どおり"
    return {"rows": rows, "counts": counts, "sent": sent, "used_daihyo": used_daihyo, "que_ok": que_ok,
            "checked": len(to_check), "mismatched": mismatched, "blocked": len(blocked),
            "failed": bool(blocked) or not que_ok or mismatched > 0}

