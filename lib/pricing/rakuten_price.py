# -*- coding: utf-8 -*-
"""
楽天の現在販売価格とSKU情報の自動取得（RMS Item API 2.0）。

楽天販売価格は楽天側でしか管理していないため、価格改定の「現販売価格」は
RMSからSKU単位で取得する（クライアントはイベントLPモジュールの rms_api を流用。
Secrets: RMS_SERVICE_SECRET / RMS_LICENSE_KEY）。

同じレスポンス（variants）に SKU管理番号・システム連携用SKU番号 が含まれるため、
楽天CSV出力に必要なSKU対応表もここで同時に構築する（CSVアップロード不要）。

商品管理番号の当たりの付け方（上から順に試す）:
  1. 保存済みSKU対応表にあればその商品管理番号
  2. 同じ親コードを持つ**兄弟コード**の商品管理番号（枝番違いは同じ商品にぶら下がる）
  3. 末尾の「-数字」枝番をすべて除去したコード等の推定（masters.manage_number_candidates）
  4. この実行中に取得済みの商品のvariantsを総当たりで再照合（追加のAPI呼び出しなし）
  5. **商品検索API**で システム連携用SKU番号／商品番号 から引く

  5が必要なのは、商品管理番号がコードから導けない商品があるため。
  実例(2026-09-04 #1256): NEコード maru0542-01〜09 の楽天商品管理番号は **maru0260**、
  maru0542 の方は「商品番号」だった。推定(1〜3)では maru0542 しか候補に出せず、
  RMSに登録済みなのに「楽天未登録」と誤って報告していた。
"""
from lib.event import rms_api

from . import masters

ITEM_SEARCH_PATH = "/es/2.0/items/search"


def is_configured():
    return rms_api.is_configured()


def _variant_price(v):
    for key in ("standardPrice", "price", "salesPrice"):
        p = v.get(key)
        if p is not None:
            try:
                return int(float(p))
            except (TypeError, ValueError):
                pass
    return None


def _get_item(manage_number):
    """RMS Item API 2.0 から商品1件を取得する。"""
    data = rms_api.get(f"/es/2.0/items/manage-numbers/{manage_number}")
    return data.get("item", data)


def _get_variants(manage_number):
    """RMS Item API 2.0 から variants dict {SKU管理番号: 変数dict} を取得する。"""
    variants = _get_item(manage_number).get("variants") or {}
    return variants if isinstance(variants, dict) else {}


def _get_variants_and_price(manage_number):
    """(variants, 商品単位の販売価格) を1回のGETで返す。
    バリエーションがあっても価格を全SKU一律で持つ商品では、SKU側に価格が入らず
    商品側にだけ入っていることがあるため、商品単位の価格もフォールバック用に拾う。"""
    item = _get_item(manage_number)
    variants = item.get("variants") or {}
    if not isinstance(variants, dict):
        variants = {}
    return variants, _variant_price(item)


def match_variants(codes, parent, variants, item_price=None):
    """
    1商品のvariantsから、対象NEコードごとに (SKU管理番号, システム連携用SKU番号, 価格) を探す。
    照合順: システム連携用SKU番号一致 → SKU管理番号一致 → 単一SKUかつコード=親。
    SKU側に価格が無い場合は item_price（商品単位の販売価格）で補う。
    返り値: {NEコード小文字: {"parent","sku","renkei","price"}}
    """
    parent = masters.norm_key(parent).lower()
    norm_variants = []
    for sku_no, v in variants.items():
        if not isinstance(v, dict):
            continue
        renkei = masters.norm_key(v.get("merchantDefinedSkuId") or "")
        if renkei == "nan":
            renkei = ""
        norm_variants.append((masters.norm_key(sku_no), renkei, _variant_price(v)))

    out = {}
    for code in codes:
        key = masters.norm_key(code).lower()
        hit = None
        for sku_no, renkei, price in norm_variants:
            if renkei and renkei.lower() == key:
                hit = (sku_no, renkei, price)
                break
        if hit is None:
            for sku_no, renkei, price in norm_variants:
                if sku_no.lower() == key:
                    hit = (sku_no, renkei, price)
                    break
        if hit is None and len(norm_variants) == 1 and key == parent:
            hit = norm_variants[0]
        if hit:
            price = hit[2] if hit[2] is not None else item_price
            out[key] = {"parent": parent, "sku": hit[0], "renkei": hit[1], "price": price}
    return out


# ── 商品管理番号の解決 ─────────────────────────────────────

def candidate_manage_numbers(code, sku_table):
    """コードからの商品管理番号候補（優先順・重複なし・API呼び出しなし）。
    保存済みSKU対応表 → 兄弟コードの管理番号 → コードからの推定 の順。
    兄弟を見るのは、枝番違いが同じ楽天商品にぶら下がるため。1つでも解決済みなら、
    コードから導けない管理番号（maru0542-xx→maru0260）でも当てられる。"""
    key = masters.norm_key(code).lower()
    cands = []

    def _add(value):
        value = masters.norm_key(value or "").lower()
        if value and value not in cands:
            cands.append(value)

    hit = sku_table.get(key)
    if hit and hit[0]:
        _add(hit[0])
    base = masters.parent_code(key).lower()
    for other, v in sku_table.items():
        if other != key and v and v[0] and masters.parent_code(other).lower() == base:
            _add(v[0])
    for c in masters.manage_number_candidates(key):
        _add(c)
    return cands


def _collect_values(node, key):
    """入れ子のJSONから key の値をすべて拾う（レスポンスの包み方に依存しないため）。"""
    out = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key and isinstance(v, (str, int)):
                out.append(str(v))
            else:
                out.extend(_collect_values(v, key))
    elif isinstance(node, list):
        for v in node:
            out.extend(_collect_values(v, key))
    return out


def search_manage_numbers(**params):
    """商品検索APIで条件に合う商品管理番号を返す（該当なしは空リスト）。
    params例: merchantDefinedSkuId="maru0542-06" / itemNumber="maru0542"。"""
    params.setdefault("hits", 10)
    data = rms_api.get(ITEM_SEARCH_PATH, params=params)
    out = []
    for mn in _collect_values(data, "manageNumber"):
        if mn and mn not in out:
            out.append(mn)
    return out


class ManageNumberSearch:
    """商品検索APIでの管理番号解決。

    この店舗のRMSライセンスで商品検索APIが使えないこともあるため、1度でも失敗したら
    以降は呼ばずに従来の推定だけで動く（使えなくても既存の挙動より悪くはならない）。
    使えなかったことは reason に残し、呼び出し側が「楽天に無い」と
    「こちらが確かめられていない」を区別できるようにする。"""

    def __init__(self):
        self.available = True
        self.reason = ""
        self._cache = {}

    def find(self, code):
        """NEコードから商品管理番号の候補を返す（優先順）。"""
        if not self.available:
            return []
        key = masters.norm_key(code)
        base = key.split("-", 1)[0]
        queries = [{"merchantDefinedSkuId": key}, {"itemNumber": key}]
        if base and base != key:
            queries.append({"itemNumber": base})
        found = []
        for q in queries:
            ck = tuple(sorted(q.items()))
            if ck not in self._cache:
                try:
                    self._cache[ck] = search_manage_numbers(**q)
                except Exception as e:  # noqa: BLE001
                    # 認証切れ・機能未許可・エンドポイント差異のいずれでも検索は諦める。
                    # 認証切れなら後続の商品取得側で改めて検出され、そちらで打ち切られる。
                    self.available = False
                    self.reason = str(e)
                    return found
            for mn in self._cache[ck]:
                if mn not in found:
                    found.append(mn)
            if found:
                break
        return found


def fetch_for_codes(codes, sku_table, on_progress=None):
    """
    NE商品コード群の現在価格とSKU情報をまとめて取得する。
    sku_table: 保存済みSKU対応表 {code小文字: (商品管理番号, SKU管理番号, 連携番号)}（商品管理番号の当たり付けに使用）
    返り値: (info={code小文字: {parent, sku, renkei, price}}, errors={対象: メッセージ}, warnings=[str])

    errorsは「楽天に無い」と「こちらが見つけられなかった」を書き分ける。価格改定は
    取れなかった商品を対象から外すため、両者を同じ文言にすると登録済みの商品が
    黙って改定漏れになる（2026-09-04 #1256 の障害）。
    """
    info, errors, warnings = {}, {}, []
    if not rms_api.is_configured():
        warnings.append("Secrets に RMS_SERVICE_SECRET / RMS_LICENSE_KEY が未設定のため、"
                        "楽天価格は取得できません。")
        return info, errors, warnings

    items_cache = {}   # 商品管理番号 → (variants, 商品単位価格)。同じ親への重複GETを避ける
    state = {"aborted": False}

    def _try_manage_number(key, mn):
        """管理番号mnにkeyのSKUがあるか試す。→ (照合できたか, その商品が楽天に存在したか)"""
        if mn not in items_cache:
            try:
                items_cache[mn] = _get_variants_and_price(mn)
            except rms_api.RMSAuthError as e:
                warnings.append(str(e))
                state["aborted"] = True   # 認証切れは以降も失敗するので打ち切り
                return False, False
            except rms_api.RMSError:
                return False, False       # その管理番号は存在しない → 次の候補へ
            except Exception:  # noqa: BLE001
                return False, False
        variants, item_price = items_cache[mn]
        got = match_variants([key], mn, variants, item_price)
        if got:
            info.update(got)
            return True, True
        return False, True

    def _match_in_cache(key):
        """取得済み商品のvariantsを総当たりで再照合する（追加のAPI呼び出しなし）。"""
        for mn, (variants, item_price) in items_cache.items():
            got = match_variants([key], mn, variants, item_price)
            if got:
                info.update(got)
                return True
        return False

    keys = []
    for code in codes:
        k = masters.norm_key(code).lower()
        if k and k not in keys:
            keys.append(k)

    total = max(len(keys), 1)
    unresolved, parent_only = [], {}

    # ① 保存済み表・兄弟コード・コードからの推定で当てる
    for done, key in enumerate(keys, start=1):
        matched, existed = False, None
        for mn in candidate_manage_numbers(key, sku_table):
            matched, exists = _try_manage_number(key, mn)
            if state["aborted"] or matched:
                break
            if exists:
                existed = mn
        if state["aborted"]:
            break
        if not matched:
            unresolved.append(key)
            if existed:
                parent_only[key] = existed
        if on_progress:
            on_progress(done, total)

    # ② 取得済みvariantsへの総当たり → ③ 商品検索APIで管理番号を引く
    searcher = ManageNumberSearch()
    if not state["aborted"] and unresolved:
        remaining = []
        for key in unresolved:
            if _match_in_cache(key):
                parent_only.pop(key, None)
                continue
            matched = False
            for mn in searcher.find(key):
                matched, _ = _try_manage_number(key, mn)
                if state["aborted"] or matched:
                    break
            if state["aborted"]:
                remaining.append(key)
                break
            if matched:
                parent_only.pop(key, None)
            else:
                remaining.append(key)
        unresolved = remaining

    if searcher.reason:
        warnings.append("商品検索APIが使えなかったため、商品管理番号は推定でしか探せていません"
                        f"（{searcher.reason[:200]}）。"
                        "「見つかりません」が楽天未登録とは限りません。")

    for key in unresolved:
        if key in parent_only:
            errors[key] = (f"楽天に商品「{parent_only[key]}」はありますが、この商品コードに"
                           "一致するSKUがありません（システム連携用SKU番号を確認してください）")
        elif not searcher.available:
            errors[key] = ("商品管理番号を特定できず、商品検索APIも使えないため確認できません"
                           "（楽天未登録とは限りません）")
        else:
            errors[key] = ("楽天に該当商品が見つかりません"
                           "（商品管理番号・商品番号・システム連携用SKU番号のいずれでも一致なし）")

    # SKUは見つかったのに価格だけ取れない場合を「未登録」に混ぜない
    for key, d in info.items():
        if d.get("price") is None:
            errors[key] = "楽天に商品・SKUはありますが、販売価格を取得できませんでした"

    if on_progress:
        on_progress(total, total)
    return info, errors, warnings


def probe_item(manage_number):
    """商品1件の生レスポンスを返す（配送方法セット等のフィールド特定・API自動化の調査用）。"""
    return rms_api.get(f"/es/2.0/items/manage-numbers/{manage_number}")


def shipping_group_patch_body(variant_keys, group_id):
    """全SKUの shipping.shippingMethodGroup（配送方法セット管理番号）だけを更新するPATCHボディ。
    2026-07-17のAPI調査で、配送方法セットはSKU単位のこのフィールドにあることを確認済み。"""
    return {"variants": {sku: {"shipping": {"shippingMethodGroup": str(group_id)}}
                         for sku in variant_keys}}


def set_shipping_method_group(manage_number, group_id):
    """商品の全SKUの配送方法セットを group_id に変更する（指定項目のみのPATCH更新）。"""
    variants = _get_variants(manage_number)
    if not variants:
        raise rms_api.RMSError(f"{manage_number}: SKU情報を取得できませんでした")
    body = shipping_group_patch_body(list(variants.keys()), group_id)
    return rms_api.patch(f"/es/2.0/items/manage-numbers/{manage_number}", body)


def price_patch_body(sku_prices):
    """指定SKUの価格を更新するPATCHボディ。
    - standardPrice: 販売価格
    - referencePrice: 表示価格（二重価格）。**販売価格と同額**、文言は当店通常価格(type=1)で固定。
      当店は全SKUに二重価格が設定されており、これを外すと更新できないため必ず一緒に送る
      （2026-07-29ユーザー確定・probe_itemで REFERENCE_PRICE/type:1 を実機確認）。"""
    return {"variants": {str(sku): {
        "standardPrice": str(int(price)),
        "referencePrice": {"displayType": "REFERENCE_PRICE", "type": 1,
                           "value": str(int(price))},
    } for sku, price in sku_prices.items()}}


def set_price(manage_number, sku_prices):
    """商品のSKU価格を変更する（指定SKU・指定項目のみのPATCH更新）。
    sku_prices: {SKU管理番号: 新価格}"""
    if not sku_prices:
        raise rms_api.RMSError(f"{manage_number}: 変更するSKU価格がありません")
    body = price_patch_body(sku_prices)
    return rms_api.patch(f"/es/2.0/items/manage-numbers/{manage_number}", body)


def to_sku_table(info):
    """fetch_for_codes の結果 → SKU対応表形式 {code: (商品管理番号, SKU管理番号, 連携番号)}。"""
    return {code: (d["parent"], d["sku"], d["renkei"]) for code, d in info.items()}


def to_prices(info):
    """fetch_for_codes の結果 → {code: 価格}（価格が取れたものだけ）。"""
    return {code: d["price"] for code, d in info.items() if d.get("price")}
