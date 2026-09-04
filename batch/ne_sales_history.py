#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ネクストエンジンの受注データから月次売上（店舗別）を集計する、過去分の棚卸しバッチ。

会計データはEC/非ECを分けていないため、EC単体の売上はNEの受注からしか出せない。
出店は2015年だが楽天RMS/APIで遡れるのは概ね2年のため、2017年以降はNEが唯一の情報源になる。

【NE APIの無料枠を守る】
  NEは月1000回の呼び出しまで無料（lib/ne_api/usage.py）。延命バッチが月60回使う。
  そのため「月ごとに1回」ではなく年単位で範囲検索し、limitを大きく取ってページングを最小化する。
  CALL_BUDGET を超えたら、そこまでの結果を出したうえで**異常終了**する（黙って途中の数字を
  正しい月次として出すと、欠けに気づけないため）。

【0件の意味を区別する】
  「その月に受注が無くて0件」と「取れなくて0件」は別物なので、取得できた年だけを covered として
  出力し、途中でAPIが落ちたらそこで打ち切って終了コードを1にする（欠けた月を0円として出さない）。

【キャンセルを落とさない】
  キャンセル伝票も件数・金額を残したうえで、有効分と別列にする（上書き・削除はしない）。

実行:
    python batch/ne_sales_history.py --from 2017-01 --to 2019-12
    python batch/ne_sales_history.py --from 2017-01 --to 2019-12 --probe   # 項目確認のみ
"""
import argparse
import csv
import datetime
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from batch import st_shim                              # noqa: E402
st_shim.install()                                      # libのimportより前に差し替える

from lib.ne_api import client, usage                   # noqa: E402

SEARCH_EP = "api_v1_receiveorder_base/search"
INFO_EP = "api_v1_receiveorder_base/info"
SHOP_EP = "api_v1_master_shop/search"

CALL_BUDGET = 120        # このバッチ1回で使ってよいNE API呼び出しの上限（無料枠1000/月に対する保険）
PAGE_LIMIT = 10000       # NE searchの1回あたり取得件数。弾かれたらFALLBACK_LIMITへ落とす
FALLBACK_LIMIT = 1000

# NEの項目名は環境・バージョンで揺れるため、/info で実在を確かめてから使う。
# 先に書いたものを優先し、無ければ次の候補へ。全滅した項目は「取れない」として明示する。
CANDIDATES = {
    "id":     ["receive_order_base_receive_order_id"],
    "date":   ["receive_order_base_date"],
    "total":  ["receive_order_base_total_amount"],
    "goods":  ["receive_order_base_goods_amount"],
    "shop":   ["receive_order_base_shop_id", "receive_order_base_shop_cut_form_id"],
    "cancel": ["receive_order_base_cancel_type_id",
               "receive_order_base_receive_order_cancel_type_id"],
    "delete": ["receive_order_base_deleted_flag", "receive_order_base_delete_flag"],
}


def _collect_field_names(obj, out):
    """/info のレスポンス構造は決め打ちできないので、再帰で項目名らしき文字列を拾う。"""
    if isinstance(obj, dict):
        for v in obj.values():
            _collect_field_names(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_field_names(v, out)
    elif isinstance(obj, str) and obj.startswith("receive_order_base_"):
        out.add(obj)


def resolve_fields(calls):
    """/info で使える項目を確定する。取れなければ第1候補で突っ込む（その旨を出す）。"""
    try:
        info = client.call(INFO_EP)
        calls[0] += 1
    except Exception as e:                                          # noqa: BLE001
        print("[warn] {} が使えません（{}）。項目は第1候補で決め打ちします。".format(INFO_EP, e),
              flush=True)
        return {k: v[0] for k, v in CANDIDATES.items()}, set()

    avail = set()
    _collect_field_names(info, avail)
    print("[info] 受注伝票で使える項目 {} 件".format(len(avail)), flush=True)
    if not avail:                       # /info は通ったが構造が想定外＝判定できていない
        print("[warn] /info から項目名を読み取れませんでした。第1候補で決め打ちします。", flush=True)
        return {k: v[0] for k, v in CANDIDATES.items()}, set()

    chosen, missing = {}, set()
    for key, cands in CANDIDATES.items():
        hit = next((c for c in cands if c in avail), None)
        if hit:
            chosen[key] = hit
        else:
            missing.add(key)
            print("[warn] {} に使える項目がありません（候補: {}）".format(key, ", ".join(cands)),
                  flush=True)
    return chosen, missing


def shop_names(calls):
    """店舗ID→店舗名。権限が無ければ空のまま（IDだけで出す）。"""
    try:
        res = client.call(SHOP_EP, {"fields": "shop_id,shop_name"})
        calls[0] += 1
    except Exception as e:                                          # noqa: BLE001
        print("[warn] 店舗マスタを取得できません（{}）。店舗IDのまま出します。".format(e), flush=True)
        return {}
    return {str(r.get("shop_id", "")): str(r.get("shop_name", ""))
            for r in res.get("data") or []}


def fetch_range(fields, start, end, calls, limit=PAGE_LIMIT):
    """[start, end] の受注を全件取る。件数上限で打ち切らない（古い分を捨てない）。"""
    date_field = fields["date"]
    rows, offset = [], 0
    while True:
        if calls[0] >= CALL_BUDGET:
            raise RuntimeError(
                "NE API呼び出しが上限({}回)に達しました。{}〜{} の途中（{}件）で中断します。"
                "期間を分けて再実行してください。".format(CALL_BUDGET, start, end, len(rows)))
        params = {"fields": ",".join(sorted(set(fields.values()))),
                  date_field + "-gte": start + " 00:00:00",
                  date_field + "-lte": end + " 23:59:59",
                  "limit": limit, "offset": offset}
        try:
            res = client.call(SEARCH_EP, params)
            calls[0] += 1
        except client.NEError as e:
            if limit > FALLBACK_LIMIT and "limit" in str(e).lower():
                print("[warn] limit={} が拒否されたため {} で取り直します。".format(
                    limit, FALLBACK_LIMIT), flush=True)
                return fetch_range(fields, start, end, calls, limit=FALLBACK_LIMIT)
            raise
        data = res.get("data") or []
        rows.extend(data)
        print("[fetch] {}〜{} offset={} → {}件（累計{}）".format(
            start, end, offset, len(data), len(rows)), flush=True)
        if len(data) < limit:
            return rows
        offset += limit


def _num(v):
    try:
        return float(str(v).replace(",", "") or 0)
    except ValueError:
        return 0.0


def aggregate(rows, fields):
    """(年月, 店舗ID) 単位に、キャンセル・削除を別枠で残したまま集計する。"""
    agg = {}
    unknown_date = 0
    for r in rows:
        raw_date = str(r.get(fields.get("date", ""), "") or "")
        if len(raw_date) < 7:
            unknown_date += 1                   # 受注日が読めない伝票は黙って捨てず件数を報告する
            continue
        ym = raw_date[:7]
        shop = str(r.get(fields.get("shop", ""), "") or "")
        total = _num(r.get(fields.get("total", "")))
        goods = _num(r.get(fields.get("goods", "")))
        cancel = str(r.get(fields.get("cancel", ""), "") or "0") not in ("0", "", "None")
        deleted = str(r.get(fields.get("delete", ""), "") or "0") not in ("0", "", "None")
        dead = cancel or deleted

        a = agg.setdefault((ym, shop), dict(n=0, goods=0.0, total=0.0,
                                            n_dead=0, goods_dead=0.0, total_dead=0.0))
        a["n"] += 1
        a["goods"] += goods
        a["total"] += total
        if dead:
            a["n_dead"] += 1
            a["goods_dead"] += goods
            a["total_dead"] += total
    return agg, unknown_date


def to_csv(agg, names):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["年月", "店舗ID", "店舗名", "受注件数", "商品代金計", "受注合計金額計",
                "キャンセル等件数", "キャンセル等商品代金", "キャンセル等受注合計",
                "有効件数", "有効商品代金計", "有効受注合計計"])
    for key in sorted(agg):
        ym, shop = key
        a = agg[key]
        w.writerow([ym, shop, names.get(shop, ""),
                    a["n"], round(a["goods"]), round(a["total"]),
                    a["n_dead"], round(a["goods_dead"]), round(a["total_dead"]),
                    a["n"] - a["n_dead"], round(a["goods"] - a["goods_dead"]),
                    round(a["total"] - a["total_dead"])])
    return buf.getvalue().encode("utf-8-sig")


def print_monthly(agg):
    """ログだけ見れば月次が分かるようにする（成果物が取り出せなかったときの保険）。"""
    months = {}
    for key, a in agg.items():
        m = months.setdefault(key[0], dict(n=0, goods=0.0, total=0.0,
                                           n_dead=0, goods_dead=0.0, total_dead=0.0))
        for k in ("n", "goods", "total", "n_dead", "goods_dead", "total_dead"):
            m[k] += a[k]
    print("", flush=True)
    print("| 年月 | 有効件数 | 有効受注合計 | 有効商品代金 | キャンセル等件数 |", flush=True)
    print("|---|---:|---:|---:|---:|", flush=True)
    for ym in sorted(months):
        m = months[ym]
        print("| {} | {:,} | {:,} | {:,} | {:,} |".format(
            ym, m["n"] - m["n_dead"], round(m["total"] - m["total_dead"]),
            round(m["goods"] - m["goods_dead"]), m["n_dead"]), flush=True)


def _month_end(ym):
    y, m = int(ym[:4]), int(ym[5:7])
    return (datetime.date(y + (m == 12), (m % 12) + 1, 1) - datetime.timedelta(days=1))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="frm", required=True, help="開始年月 YYYY-MM")
    p.add_argument("--to", dest="to", required=True, help="終了年月 YYYY-MM")
    p.add_argument("--probe", action="store_true", help="項目の確認だけして終了（API 1回）")
    p.add_argument("--no-drive", action="store_true", help="Driveへ保存しない（ログ出力のみ）")
    args = p.parse_args()

    calls = [0]
    fields, missing = resolve_fields(calls)
    print("[info] 使う項目: " + json.dumps(fields, ensure_ascii=False), flush=True)
    if "date" not in fields or "total" not in fields:
        print("[error] 受注日か金額の項目が特定できないため集計できません。", file=sys.stderr, flush=True)
        return 1
    if args.probe:
        print("[info] --probe のためここで終了します。", flush=True)
        return 0

    names = shop_names(calls)

    y0, y1 = int(args.frm[:4]), int(args.to[:4])
    rows, covered, failed = [], [], []
    for y in range(y0, y1 + 1):
        start = args.frm + "-01" if y == y0 else "{}-01-01".format(y)
        last = args.to if y == y1 else "{}-12".format(y)
        try:
            got = fetch_range(fields, start, _month_end(last).isoformat(), calls)
        except Exception as e:                                      # noqa: BLE001
            print("[error] {}年を取得できませんでした: {}".format(y, e), file=sys.stderr, flush=True)
            failed.append(str(y))
            break                       # 欠けたまま先に進んで「0件の月」を作らない
        rows.extend(got)
        covered.append(str(y))

    try:
        usage.flush()
    except Exception:                                               # noqa: BLE001
        pass

    agg, unknown_date = aggregate(rows, fields)
    print("", flush=True)
    print("[result] 取得 {:,}件 / API呼び出し {}回 / 取得できた年: {}".format(
        len(rows), calls[0], ", ".join(covered) or "なし"), flush=True)
    if unknown_date:
        print("[warn] 受注日を読めない伝票 {}件は集計から除外しました。".format(unknown_date), flush=True)
    if missing:
        print("[warn] 取れなかった項目: {}（該当列は0で出ます）".format(", ".join(sorted(missing))),
              flush=True)
    print_monthly(agg)

    blob = to_csv(agg, names)
    name = "ne_monthly_sales_{}_{}.csv".format(args.frm, args.to)
    out_dir = os.path.join(ROOT, "output")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, name), "wb") as f:
        f.write(blob)
    print("[out] " + os.path.join(out_dir, name), flush=True)

    if not args.no_drive:
        try:
            from lib.invoice import drive_master
            from lib import master_store
            drive_master.upload_or_replace(blob, name, master_store.folder_id(),
                                           mimetype="text/csv")
            print("[out] Google Drive に {} を保存しました。".format(name), flush=True)
        except Exception as e:                                      # noqa: BLE001
            print("[warn] Driveへの保存に失敗しました（{}）。"
                  "ログの表とArtifactを使ってください。".format(e), flush=True)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
