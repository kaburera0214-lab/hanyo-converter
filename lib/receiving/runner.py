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
  yahoo_delivery:   {"rows": [{code, 便種}], "group_no": {便種: No},
                     "group_bins": {No: 便種}}                … 配送グループ（項目指定アップロード）

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
STEP_YAHOO_DELIVERY = "⑥ Yahoo 配送グループ"


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
        timeout, interval = goods.wait_policy(len(rows))   # 大量アップは長めに待つ
        ok, message = goods.wait_que(que_id, timeout=timeout, interval=interval)
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
    _yahoo_delivery(tasks.get("yahoo_delivery") or {}, results, failed, on_step)

    return results, failed


def _yahoo_prices(price_by_code, results, failed, on_step):
    """Yahoo価格を updateItems で更新し、reservePublish で店頭反映する。
    商品未登録（it-02002）だけは対象外として明示し、登録済み商品は除外後に再送する。"""
    if not price_by_code:
        return
    target = f"{len(price_by_code)}件"
    try:
        from lib.yahoo_api import client as yclient, items as yitems
        if on_step:
            on_step("⑤ Yahoo: トークン確認中…")
        yclient.access_token()            # 期限切れ間近なら自動リフレッシュ
        if on_step:
            on_step("⑤ Yahoo: 価格更新API(updateItems)を呼び出し中…")
        category_repairs = {}
        ok, errs, missing = yitems.update_prices_checked(
            price_by_code,
            on_category_repair=lambda code, detail: category_repairs.__setitem__(code, detail))
        missing_set = set(missing)
        update_map = {code: price for code, price in price_by_code.items()
                      if code not in missing_set}
        if missing:
            shown = "、".join(missing[:10]) + (" …" if len(missing) > 10 else "")
            results.append({"ステップ": STEP_YAHOO_PRICE,
                            "対象": f"{len(missing)}件（{shown}）", "状態": "スキップ",
                            "メッセージ": "Yahoo API it-02002（指定された商品は存在しません）のため"
                                         "価格更新の対象外にしました。Yahooの商品登録状況を確認してください。"})
        if errs:
            results.append({"ステップ": STEP_YAHOO_PRICE, "対象": f"{len(update_map)}件", "状態": "失敗",
                            "メッセージ": "／".join(errs[:5])})
            if update_map:
                failed["yahoo_price"] = update_map
            return
        if not update_map:
            return
        if on_step:
            on_step("⑤ Yahoo: 反映予約API(reservePublish)を呼び出し中…")
        perr = yitems.reserve_publish()   # 更新は自動反映されないので反映予約を1回
        if perr:
            results.append({"ステップ": STEP_YAHOO_PRICE, "対象": f"{len(update_map)}件", "状態": "失敗",
                            "メッセージ": "更新OKだが反映予約に失敗: " + "／".join(perr[:5])})
            failed["yahoo_price"] = update_map
        else:
            message = "更新＋反映予約 完了"
            if category_repairs:
                shown = "、".join(
                    f"{code}→{detail['category_id']}({detail.get('category_name') or '名称不明'})"
                    for code, detail in list(category_repairs.items())[:10])
                message += f"／プロダクトカテゴリ自動設定 {len(category_repairs)}件: {shown}"
            results.append({"ステップ": STEP_YAHOO_PRICE, "対象": f"{ok}件", "状態": "成功",
                            "メッセージ": message})
    except Exception as e:  # noqa: BLE001（認可切れ等もここで拾う。has_auth_errorが文言で判定）
        results.append({"ステップ": STEP_YAHOO_PRICE, "対象": target, "状態": "失敗",
                        "メッセージ": str(e)})
        failed["yahoo_price"] = price_by_code


def has_auth_error(results):
    """結果に認証切れ（要再認可）が含まれるか（NE/RMSどちらか）。"""
    return any("認証" in str(r.get("メッセージ", "")) or "認可" in str(r.get("メッセージ", ""))
               for r in results if r.get("状態") == "失敗")


def _yahoo_delivery(task, results, failed, on_step):
    """Yahoo配送グループを uploadItemFile（項目指定）で更新する。

    送る前に getItem で現在のグループを読み、**本当に変える必要があるものだけ**に絞る。
    便種が既に合っている商品（別キャリアのグループにいるだけ）は触らない。
    反映は非同期なので、成功＝「送信と反映予約まで完了」であり反映確認ではない
    （確認は batch/yahoo_queue_watch.py が日次で getItem を読んで行う）。
    """
    rows = (task or {}).get("rows") or []
    if not rows:
        return
    target = f"{len(rows)}件"
    try:
        from lib.yahoo_api import client as yclient, item_upload, items as yitems
        from lib.receiving import yahoo_delivery as ydv

        if on_step:
            on_step("⑥ Yahoo: 現在の配送グループを確認中…")
        yclient.access_token()            # 期限切れ間近なら自動リフレッシュ
        current = yitems.get_item_status([r["code"] for r in rows])
        plan = ydv.classify(rows, current, task.get("group_no"), task.get("group_bins"))

        if plan.already:
            # 触らないと決めた商品を控えに残すと、永久に一致せず赤のままになる。
            # 旧仕様で積まれた行もここで自動的に片付く。
            _resolve_delivery(task.get("folder"), [r["code"] for r in plan.already])
            shown = "、".join(f"{r['code']}(No.{r['現在No']})" for r in plan.already[:10])
            results.append({
                "ステップ": STEP_YAHOO_DELIVERY, "対象": f"{len(plan.already)}件（{shown}）",
                "状態": "スキップ",
                "メッセージ": "Yahoo側はすでにこの便種の配送グループです。"
                             "キャリアを勝手に変えないため対象外にしました。"})
        if plan.not_found:
            shown = "、".join(r["code"] for r in plan.not_found[:10])
            results.append({
                "ステップ": STEP_YAHOO_DELIVERY, "対象": f"{len(plan.not_found)}件（{shown}）",
                "状態": "失敗",
                "メッセージ": "Yahooにこの商品が登録されていません。"
                             "商品を登録するまで配送グループは反映できません"
                             "（待っても解消しないので、ここで報告しています）。"})
        for r in plan.unknown:
            results.append({
                "ステップ": STEP_YAHOO_DELIVERY, "対象": r["code"], "状態": "失敗",
                "メッセージ": r["理由"] + "（推測で書き換えず、対象外にしました）"})

        # 送る前にキューへ積む（＝反映確認待ちの控え）。ここで落ちても控えは残る。
        # 反映が確認できた行は日次点検(batch/yahoo_queue_watch.py)が外していく。
        # 「対象外（すでに同じ便種）」は積まない。積むと永久に一致せず赤のままになる。
        _enqueue_delivery(task.get("folder"), plan)

        if not plan.to_update:
            return

        # プロダクトカテゴリ未設定の商品は、配送グループだけ送っても
        # U-001-0363 で行ごと弾かれる。価格改定と同じ推定を使って一緒に埋める。
        categories, cat_failures, cat_detail = {}, {}, {}
        _need = ydv.needs_category(plan.to_update)
        if _need:
            from lib.yahoo_api import category_repair as ycat
            if on_step:
                on_step("⑥ Yahoo: プロダクトカテゴリを推定中…")
            cat_detail, cat_failures = ycat.plan_categories(_need)
            categories = {code: detail["category_id"] for code, detail in cat_detail.items()}
        for code, reason in cat_failures.items():
            results.append({
                "ステップ": STEP_YAHOO_DELIVERY, "対象": code, "状態": "失敗",
                "メッセージ": "プロダクトカテゴリが未設定で、推定もできませんでした。"
                             "この商品はカテゴリを設定しないと配送グループを更新できません: "
                             + str(reason)})

        if cat_detail:
            # 後段（反映予約）が失敗しても、何のカテゴリを入れたかは必ず残す。
            # 成功メッセージにだけ書くと、失敗した回は記録が消える。
            shown_cat = "、".join(
                f"{code}→{d['category_id']}({d.get('category_name') or '名称不明'})"
                for code, d in list(cat_detail.items())[:10])
            results.append({
                "ステップ": STEP_YAHOO_DELIVERY, "対象": f"{len(cat_detail)}件",
                "状態": "成功",
                "メッセージ": f"プロダクトカテゴリを自動設定してCSVに含めました: {shown_cat}"})

        batches = ydv.upload_batches(plan.to_update, categories)
        if not batches:
            failed["yahoo_delivery"] = dict(task, rows=plan.to_update)
            return
        sent = [c for b in batches for c in b["codes"]]
        shown = "、".join(f"{r['code']}→No.{r['no']}" for r in plan.to_update
                         if r["code"] in set(sent))
        if on_step:
            on_step("⑥ Yahoo: 商品アップロードAPI(項目指定)を呼び出し中…")
        for batch in batches:
            ok, errs = item_upload.upload_field_specified(
                batch["csv"], filename="yahoo_delivery.csv")
            if not ok:
                results.append({
                    "ステップ": STEP_YAHOO_DELIVERY,
                    "対象": f"{len(batch['codes'])}件（{'、'.join(batch['codes'][:10])}）",
                    "状態": "失敗", "メッセージ": "／".join(errs[:5])})
                failed["yahoo_delivery"] = dict(task, rows=plan.to_update)
                return
        # アップロード直後はYahooがファイルを処理中で、反映予約は ed-00006 で必ず断られる。
        # これは失敗ではなく順番待ちなので、少し待って試し直す。
        if on_step:
            on_step("⑥ Yahoo: 反映予約API(reservePublish)を呼び出し中…")
        published, busy, perr = yitems.reserve_publish_retry(
            on_wait=lambda n, total, w: on_step(
                f"⑥ Yahoo: アップロード処理中のため待機（{n}/{total}・{w}秒）…")
            if on_step else None)
        if busy:
            results.append({
                "ステップ": STEP_YAHOO_DELIVERY, "対象": f"{len(sent)}件（{shown}）",
                "状態": "スキップ",
                "メッセージ": "アップロードは完了しています。Yahoo側が処理中で反映予約"
                             "(reservePublish)がまだ受け付けられないため、**保留**にしました"
                             "（毎朝の点検が反映を確認し、まだなら予約し直します）。"})
            return
        if not published:
            results.append({
                "ステップ": STEP_YAHOO_DELIVERY, "対象": f"{len(sent)}件（{shown}）",
                "状態": "失敗",
                "メッセージ": "**アップロードは成功しています**が、反映予約(reservePublish)に"
                             "失敗しました。店頭反映が保留のままです: " + "／".join(perr[:5])})
            failed["yahoo_delivery"] = dict(task, rows=plan.to_update)
            return
        message = ("送信＋反映予約 完了（反映は非同期です。実際に反映されたかは"
                   "日次の点検でgetItemを読んで確認します）")
        results.append({
            "ステップ": STEP_YAHOO_DELIVERY, "対象": f"{len(sent)}件（{shown}）",
            "状態": "成功", "メッセージ": message})
    except Exception as e:  # noqa: BLE001（認可切れ等もここで拾う）
        results.append({"ステップ": STEP_YAHOO_DELIVERY, "対象": target, "状態": "失敗",
                        "メッセージ": str(e)})
        failed["yahoo_delivery"] = task


def _enqueue_delivery(folder, plan):
    """反映確認待ちの控えをDriveのキューへ積む（失敗しても本処理は止めない）。"""
    if not folder:
        return
    try:
        from lib.pricing import export as ex
        from lib.receiving import yahoo_queue as yq

        rows = [{"code": r["code"],
                 yq.DELIVERY_VALUE_COLUMN: ex.YAHOO_DELIVERY_VALUE.get(r["便種"], r["便種"])}
                for r in (plan.to_update + plan.not_found + plan.unknown)]
        if rows:
            yq.append_delivery(rows, folder)
    except Exception:  # noqa: BLE001
        pass


def _resolve_delivery(folder, codes):
    """触る必要が無いと分かったコードを、反映確認待ちの控えから外す。"""
    if not folder or not codes:
        return
    try:
        from lib.receiving import yahoo_queue as yq
        yq.resolve_delivery(codes, folder)
    except Exception:  # noqa: BLE001
        pass
