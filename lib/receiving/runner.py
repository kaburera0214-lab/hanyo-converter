# -*- coding: utf-8 -*-
"""
入荷登録の実行オーケストレーション（NE→楽天の順にAPIを呼ぶ）。

順序の意図: NE（ロケーション＝現場の入荷作業に必須）を最優先。モール系は後段で、
失敗しても入荷作業自体は止まらない。各ステップ・各対象を個別にtry/exceptで捕捉し、
「どこまで進んだか」を行単位の結果リストで返す。認証切れ（NEAuthError/RMSAuthError）
は同じステップの以降も必ず失敗するため、そのステップを打ち切って残りをスキップ記録する。

tasks（page 21 が組み立てる）:
  ne_main:          [{syohin_code, location, org1}]      … NE一括更新①（全行・1回のupload）
  ne_price:         [{syohin_code, baika_tnk}]           … NE一括更新②（価格再設定行のみ）
  rakuten_delivery: [{商品管理番号, 旧便種, 新便種, group_id}] … 配送方法セットPATCH
  rakuten_price:    [{商品管理番号, sku_prices, 対象コード}]    … 価格PATCH

返り値: (results, failed)
  results: [{ステップ, 対象, 状態(成功/失敗/スキップ), メッセージ}]
  failed:  失敗した分だけの同形式tasks（「失敗した処理だけ再実行」に使う）
"""
from lib.event import rms_api
from lib.ne_api import client as ne_client, goods
from lib.pricing import rakuten_price
from lib.receiving import plan as rp

STEP_NE_MAIN = "① NEロケーション・項目1"
STEP_NE_PRICE = "② NE売価（価格再設定）"
STEP_RAKUTEN_DELIVERY = "③ 楽天 配送方法セット"
STEP_RAKUTEN_PRICE = "④ 楽天 販売価格"
STEP_YAHOO_PRICE = "⑤ Yahoo 販売価格"


def _ne_batch(step, rows, results, failed, key, on_step):
    """NE商品マスタの一括更新（upload→キュー完了待ち）。バッチ全体で成功/失敗を記録する。"""
    if not rows:
        return
    target = f"{len(rows)}件（{'、'.join(r['syohin_code'] for r in rows[:5])}"
    target += " …）" if len(rows) > 5 else "）"
    if on_step:
        on_step(f"{step} を更新中…")
    try:
        que_id = goods.upload_goods(rows)
        ok, message = goods.wait_que(que_id)
        if ok:
            results.append({"ステップ": step, "対象": target, "状態": "成功",
                            "メッセージ": f"キュー{que_id} 完了"})
        else:
            results.append({"ステップ": step, "対象": target, "状態": "失敗",
                            "メッセージ": message})
            failed[key] = failed.get(key, []) + rows
    except Exception as e:  # noqa: BLE001
        results.append({"ステップ": step, "対象": target, "状態": "失敗",
                        "メッセージ": str(e)})
        failed[key] = failed.get(key, []) + rows


def _rakuten_each(step, items, results, failed, key, on_step, fn, describe):
    """楽天系: 1商品ずつ実行し、認証切れが出たら残りをスキップ記録して打ち切る。"""
    auth_dead = None
    pending = []
    for i, item in enumerate(items):
        target = describe(item)
        if auth_dead:
            results.append({"ステップ": step, "対象": target, "状態": "スキップ",
                            "メッセージ": "認証切れのため中断"})
            pending.append(item)
            continue
        if on_step:
            on_step(f"{step} {i + 1}/{len(items)}: {target}")
        try:
            fn(item)
            results.append({"ステップ": step, "対象": target, "状態": "成功",
                            "メッセージ": ""})
        except rms_api.RMSAuthError as e:
            auth_dead = str(e)
            results.append({"ステップ": step, "対象": target, "状態": "失敗",
                            "メッセージ": auth_dead})
            pending.append(item)
        except Exception as e:  # noqa: BLE001
            results.append({"ステップ": step, "対象": target, "状態": "失敗",
                            "メッセージ": str(e)})
            pending.append(item)
    if pending:
        failed[key] = pending


def execute(tasks, on_step=None):
    """tasks を順に実行する。返り値: (results, failed)。docstring参照。"""
    results = []
    failed = {}

    ne_main = tasks.get("ne_main") or []
    ne_price = tasks.get("ne_price") or []

    # 事前確認: NEに存在する商品か（一致しないと新規登録扱いになり「売価は必須」等でNGになる）。
    # 存在する行はNEの正確な商品コードへ置換し、見つからない商品は明確な失敗として記録する。
    if ne_main:
        if on_step:
            on_step("NEで商品コードを確認中…")
        try:
            found = goods.find_existing([r["syohin_code"] for r in ne_main])
        except ne_client.NEAuthError:
            found = None            # 認証切れは下の_ne_batchでまとめて扱う
        except Exception:  # noqa: BLE001
            found = None            # 確認に失敗したら従来どおりそのまま送る（誤ブロック回避）
        if found is not None:
            orig_main = list(ne_main)
            ne_main, missing = rp.split_by_existence(ne_main, found)
            ne_price, _ = rp.split_by_existence(ne_price, found)
            if missing:
                miss_set = {str(m).strip().lower() for m in missing}
                for code in missing:
                    results.append({
                        "ステップ": STEP_NE_MAIN, "対象": str(code), "状態": "失敗",
                        "メッセージ": "NEにこの商品コードが見つかりません。"
                        "商品マスタ（Drive）とNEの商品コードが一致しているか、"
                        "NEに登録済みかを確認してください（大文字小文字の違いも確認）。"})
                failed["ne_main"] = [r for r in orig_main
                                     if str(r["syohin_code"]).strip().lower() in miss_set]

    try:
        _ne_batch(STEP_NE_MAIN, ne_main, results, failed, "ne_main", on_step)
        _ne_batch(STEP_NE_PRICE, ne_price, results, failed, "ne_price", on_step)
    except ne_client.NEAuthError as e:
        # upload前のトークン読込段階で切れていた場合など（バッチ内でも捕捉するが保険）
        for key, rows in (("ne_main", ne_main), ("ne_price", ne_price)):
            if rows and key not in failed:
                failed[key] = rows
                results.append({"ステップ": "NE更新", "対象": f"{len(rows)}件",
                                "状態": "失敗", "メッセージ": str(e)})

    _rakuten_each(
        STEP_RAKUTEN_DELIVERY, tasks.get("rakuten_delivery") or [],
        results, failed, "rakuten_delivery", on_step,
        fn=lambda d: rakuten_price.set_shipping_method_group(d["商品管理番号"], d["group_id"]),
        describe=lambda d: f"{d['商品管理番号']}（{d['旧便種']}→{d['新便種']}）")

    _rakuten_each(
        STEP_RAKUTEN_PRICE, tasks.get("rakuten_price") or [],
        results, failed, "rakuten_price", on_step,
        fn=lambda p: rakuten_price.set_price(p["商品管理番号"], p["sku_prices"]),
        describe=lambda p: f"{p['商品管理番号']}（{'、'.join(p['対象コード'])}）")

    _yahoo_prices(tasks.get("yahoo_price") or {}, results, failed, on_step)

    return results, failed


def _yahoo_prices(price_by_code, results, failed, on_step):
    """Yahoo価格を updateItems で更新し、reservePublish で店頭反映する（設定済みのときのみ）。
    price_by_code: {Yahoo商品コード(親): 価格}。未設定ならページ側でCSVキューにフォールバック。"""
    if not price_by_code:
        return
    target = f"{len(price_by_code)}件"
    if on_step:
        on_step(f"{STEP_YAHOO_PRICE} を更新中…")
    try:
        from lib.yahoo_api import client as yclient, items as yitems
        ok, errs = yitems.update_prices(price_by_code)
        if errs:
            results.append({"ステップ": STEP_YAHOO_PRICE, "対象": target, "状態": "失敗",
                            "メッセージ": "／".join(errs[:5])})
            failed["yahoo_price"] = price_by_code
            return
        perr = yitems.reserve_publish()   # 更新は自動反映されないので反映予約を1回
        if perr:
            results.append({"ステップ": STEP_YAHOO_PRICE, "対象": target, "状態": "失敗",
                            "メッセージ": "更新OKだが反映予約に失敗: " + "／".join(perr[:5])})
            failed["yahoo_price"] = price_by_code
        else:
            results.append({"ステップ": STEP_YAHOO_PRICE, "対象": f"{ok}件", "状態": "成功",
                            "メッセージ": "更新＋反映予約 完了"})
    except Exception as e:  # noqa: BLE001（認可切れ等もここで拾う。has_auth_errorが文言で判定）
        results.append({"ステップ": STEP_YAHOO_PRICE, "対象": target, "状態": "失敗",
                        "メッセージ": str(e)})
        failed["yahoo_price"] = price_by_code


def has_auth_error(results):
    """結果に認証切れ（要再認可）が含まれるか（NE/RMSどちらか）。"""
    return any("認証" in str(r.get("メッセージ", "")) or "認可" in str(r.get("メッセージ", ""))
               for r in results if r.get("状態") == "失敗")
