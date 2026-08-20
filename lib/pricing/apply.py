# -*- coding: utf-8 -*-
"""
価格改定の本番反映（楽天RMS・Yahoo!ショッピング・ネクストエンジンへAPIで直接書き込む）。

確定した新価格を、CSVを手でアップロードする代わりにそのままAPIで反映する（2026-08-20新設）。
CSV出力は廃止せず証跡・フォールバックとして残す＝APIが落ちてもその日の価格改定を
取りこぼさない。

対象と順序（NE＝社内の正本を先に、モールは後）:
  ① NE    api_v1_master_goods/upload   … baika_tnk（税抜売価）・genka_tnk（原価＝新下代）
  ② 楽天  Item API 2.0 PATCH            … SKUごとの standardPrice / referencePrice
  ③ Yahoo updateItems ＋ reservePublish … 親コード単位の price

「どこまで進んだか」を行単位の結果で返し、失敗した分だけ再実行できるようにする
（入荷登録 lib/receiving/runner.py と同じ考え方）。認証切れは同じステップの以降も
必ず失敗するため、そのステップを打ち切って残りをスキップ記録する。

※直送タブの「送料無料フラグ」はAPI側の該当フィールドが未確定のため、ここでは
  **価格しか更新しない**。送料設定はCSV（normal-item.csv / data.csv）またはモール画面で
  別途反映する（build_tasks の注記も参照）。

tasks:
  ne_price:      [{syohin_code, baika_tnk, genka_tnk}]
  rakuten_price: [{商品管理番号, sku_prices, 対象コード}]
  yahoo_price:   {親コード: 価格}
返り値: (results, failed)
  results: [{ステップ, 対象, 状態(成功/失敗/スキップ), メッセージ}]
  failed:  失敗した分だけの同形式tasks（「失敗した処理だけ再実行」に使う）
"""
from lib.event import rms_api
from lib.ne_api import client as ne_client, goods

from . import calc, export as ex, rakuten_price

STEP_NE = "① NE 売価・原価"
STEP_RAKUTEN = "② 楽天 販売価格"
STEP_YAHOO = "③ Yahoo 販売価格"

ALL_SYSTEMS = ("ne", "rakuten", "yahoo")


# ══ タスク組み立て（Streamlit非依存・純関数） ══════════════

def split_targets(result_df, include_unchanged=False):
    """結果表 → (NE対象, モール対象)。

    NE対象   … 計算できた全行（原価も更新するため、価格が変わらない行も含める）
    モール対象 … 価格が変わる行だけ（include_unchanged=Trueなら変わらない行も含める）
    ※出力CSV（pages/20 の build_output_files）と同じ判定を使う。
    """
    ok = result_df[result_df["新販売価格"].notna() & (result_df["新販売価格"] > 0)]
    changed = ok if include_unchanged else ok[ok["新販売価格"] != ok["現販売価格"]]
    return ok, changed


def mall_rows_of(changed):
    """モール対象の行 → export の期待する [{商品コード, 楽天販売価格, Yahoo販売価格}]。"""
    return [{"商品コード": r["商品コード"], "楽天販売価格": r["新販売価格"],
             "Yahoo販売価格": r["新販売価格"]} for _, r in changed.iterrows()]


def build_tasks(result_df, sku_table, include_unchanged=False, systems=None):
    """確定した結果表 → API実行タスク一式。

    systems: 実行対象 {"ne","rakuten","yahoo"} の部分集合（Noneなら全部）。
    返り値: (tasks, notes)
      notes["rakuten_missing"] … 楽天SKU番号が分からず対象外にした商品コード（CSVと同じ判定）
      notes["yahoo_diff"]      … 同一親でSKU間の価格が割れた親コード（Yahooは最高値を採用）
      notes["ne_skipped"]      … 売価か原価が空でNEに送れない商品コード（NEは空値を送らない）

    ※送料無料フラグ（直送タブ）はAPIでは設定しない。価格のみの更新になる。
    """
    systems = set(systems or ALL_SYSTEMS)
    ok, changed = split_targets(result_df, include_unchanged)
    tasks = {}
    notes = {"rakuten_missing": [], "yahoo_diff": [], "ne_skipped": []}

    if "ne" in systems:
        ne_rows = []
        for _, r in ok.iterrows():
            code = str(r["商品コード"]).strip()
            baika = calc.to_number(r.get("NE売価"))
            genka = calc.to_number(r.get("新下代"))
            if not code or baika is None or genka is None:
                notes["ne_skipped"].append(code or "(商品コードなし)")
                continue
            ne_rows.append({"syohin_code": code, "baika_tnk": int(baika),
                            "genka_tnk": genka})
        tasks["ne_price"] = ne_rows

    mall = mall_rows_of(changed)
    if "rakuten" in systems:
        groups, order, missing = ex.rakuten_groups(mall, sku_table)
        notes["rakuten_missing"] = missing
        tasks["rakuten_price"] = [{
            "商品管理番号": parent,
            "sku_prices": {sku: price for sku, _renkei, price, _code in groups[parent]},
            "対象コード": [code for _sku, _renkei, _price, code in groups[parent]],
        } for parent in order]
    if "yahoo" in systems:
        records, diff = ex.yahoo_rows(mall, sku_table)
        notes["yahoo_diff"] = diff
        tasks["yahoo_price"] = {r["code"]: int(r["price"]) for r in records}
    return tasks, notes


def task_counts(tasks):
    """画面表示用の件数: {ne, rakuten, rakuten_sku, yahoo}。"""
    rk = tasks.get("rakuten_price") or []
    return {
        "ne": len(tasks.get("ne_price") or []),
        "rakuten": len(rk),
        "rakuten_sku": sum(len(t["sku_prices"]) for t in rk),
        "yahoo": len(tasks.get("yahoo_price") or {}),
    }


# ══ 実行 ════════════════════════════════════════════════════

def execute(tasks, on_step=None):
    """tasks を ①NE → ②楽天 → ③Yahoo の順に実行する。返り値: (results, failed)。"""
    results, failed = [], {}
    ne_rows = list(tasks.get("ne_price") or [])

    if ne_rows:
        # NEに無い商品コードを送るとuploadが「新規登録」扱いになり必須項目エラーになる。
        # 事前に存在確認し、NEが実際に持つ正確なコードへ置き換える（大文字小文字ずれの吸収）。
        if on_step:
            on_step("NEで商品コードを確認中…")
        try:
            found = goods.find_existing([r["syohin_code"] for r in ne_rows])
        except ne_client.NEAuthError:
            found = None        # 認証切れは _ne_batch でまとめて失敗として扱う
        except Exception:  # noqa: BLE001
            found = None        # 確認できないだけなら従来どおり送る（誤ブロック回避）
        if found is not None:
            orig = list(ne_rows)
            ne_rows, missing = goods.split_by_existence(ne_rows, found)
            if missing:
                miss = {str(m).strip().lower() for m in missing}
                for code in missing:
                    results.append({
                        "ステップ": STEP_NE, "対象": str(code), "状態": "失敗",
                        "メッセージ": "NEにこの商品コードが見つかりません。商品マスタ（Drive）と"
                                      "NEの商品コードが一致しているか確認してください"
                                      "（大文字小文字の違いも確認）。"})
                failed["ne_price"] = [r for r in orig
                                      if str(r["syohin_code"]).strip().lower() in miss]
        _ne_batch(ne_rows, results, failed, on_step)

    _rakuten_prices(tasks.get("rakuten_price") or [], results, failed, on_step)
    _yahoo_prices(tasks.get("yahoo_price") or {}, results, failed, on_step)
    return results, failed


def _ne_batch(rows, results, failed, on_step):
    """NE商品マスタの一括更新（upload → キュー完了待ち）。バッチ全体で成否を記録する。"""
    if not rows:
        return
    target = f"{len(rows)}件（{'、'.join(r['syohin_code'] for r in rows[:5])}"
    target += " …）" if len(rows) > 5 else "）"
    if on_step:
        on_step(f"{STEP_NE} を更新中…（NE側の処理完了まで待ちます）")
    try:
        que_id = goods.upload_goods(rows)
        timeout, interval = goods.wait_policy(len(rows))   # 大量アップは長めに待つ
        ok, message = goods.wait_que(que_id, timeout=timeout, interval=interval)
        results.append({"ステップ": STEP_NE, "対象": target,
                        "状態": "成功" if ok else "失敗",
                        "メッセージ": f"キュー{que_id} 完了" if ok else message})
        if not ok:
            failed["ne_price"] = failed.get("ne_price", []) + rows
    except Exception as e:  # noqa: BLE001（認証切れもここで拾う）
        results.append({"ステップ": STEP_NE, "対象": target, "状態": "失敗",
                        "メッセージ": str(e)})
        failed["ne_price"] = failed.get("ne_price", []) + rows


def _rakuten_prices(items, results, failed, on_step):
    """楽天は商品（商品管理番号）ごとにPATCH。認証切れが出たら残りは打ち切ってスキップ記録する
    （以降も必ず失敗するため、無駄にAPIを叩かない）。"""
    auth_dead = None
    pending = []
    for i, item in enumerate(items):
        target = f"{item['商品管理番号']}（{'、'.join(item['対象コード'])}）"
        if auth_dead:
            results.append({"ステップ": STEP_RAKUTEN, "対象": target, "状態": "スキップ",
                            "メッセージ": "認証切れのため中断"})
            pending.append(item)
            continue
        if on_step:
            on_step(f"{STEP_RAKUTEN} {i + 1}/{len(items)}: {target}")
        try:
            rakuten_price.set_price(item["商品管理番号"], item["sku_prices"])
            results.append({"ステップ": STEP_RAKUTEN, "対象": target, "状態": "成功",
                            "メッセージ": "、".join(f"{sku}→{price}円" for sku, price
                                                 in item["sku_prices"].items())})
        except rms_api.RMSAuthError as e:
            auth_dead = str(e)
            results.append({"ステップ": STEP_RAKUTEN, "対象": target, "状態": "失敗",
                            "メッセージ": auth_dead})
            pending.append(item)
        except Exception as e:  # noqa: BLE001
            results.append({"ステップ": STEP_RAKUTEN, "対象": target, "状態": "失敗",
                            "メッセージ": str(e)})
            pending.append(item)
    if pending:
        failed["rakuten_price"] = pending


def _yahoo_prices(price_by_code, results, failed, on_step):
    """Yahoo価格を updateItems で更新し、reservePublish で店頭反映する。
    price_by_code: {Yahoo商品コード(親): 価格}。失敗分はページ側でCSVキューへ退避する。"""
    if not price_by_code:
        return
    target = f"{len(price_by_code)}件"
    try:
        from lib.yahoo_api import client as yclient, items as yitems
        if on_step:
            on_step(f"{STEP_YAHOO}: トークン確認中…")
        yclient.access_token()            # 期限切れ間近なら自動リフレッシュ
        if on_step:
            on_step(f"{STEP_YAHOO}: 価格更新API(updateItems)を呼び出し中…")
        ok, errs = yitems.update_prices(price_by_code)
        if errs:
            results.append({"ステップ": STEP_YAHOO, "対象": target, "状態": "失敗",
                            "メッセージ": "／".join(errs[:5])})
            failed["yahoo_price"] = price_by_code
            return
        if on_step:
            on_step(f"{STEP_YAHOO}: 反映予約API(reservePublish)を呼び出し中…")
        perr = yitems.reserve_publish()   # 更新は自動反映されないので反映予約を1回
        if perr:
            results.append({"ステップ": STEP_YAHOO, "対象": target, "状態": "失敗",
                            "メッセージ": "更新OKだが反映予約に失敗: " + "／".join(perr[:5])})
            failed["yahoo_price"] = price_by_code
        else:
            results.append({"ステップ": STEP_YAHOO, "対象": f"{ok}件", "状態": "成功",
                            "メッセージ": "更新＋反映予約 完了"})
    except Exception as e:  # noqa: BLE001（認可切れ等もここで拾う。has_auth_errorが文言で判定）
        results.append({"ステップ": STEP_YAHOO, "対象": target, "状態": "失敗",
                        "メッセージ": str(e)})
        failed["yahoo_price"] = price_by_code


def has_auth_error(results):
    """結果に認証切れ（要再認可）が含まれるか（NE/RMS/Yahooのいずれか）。"""
    return any("認証" in str(r.get("メッセージ", "")) or "認可" in str(r.get("メッセージ", ""))
               for r in results if r.get("状態") == "失敗")


def summarize(results):
    """結果 → (成功数, 失敗数, スキップ数)。"""
    def n(state):
        return sum(1 for r in results if r.get("状態") == state)
    return n("成功"), n("失敗"), n("スキップ")
