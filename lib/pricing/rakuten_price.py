# -*- coding: utf-8 -*-
"""
楽天の現在販売価格とSKU情報の自動取得（RMS Item API 2.0）。

楽天販売価格は楽天側でしか管理していないため、価格改定の「現販売価格」は
RMSからSKU単位で取得する（クライアントはイベントLPモジュールの rms_api を流用。
Secrets: RMS_SERVICE_SECRET / RMS_LICENSE_KEY）。

同じレスポンス（variants）に SKU管理番号・システム連携用SKU番号 が含まれるため、
楽天CSV出力に必要なSKU対応表もここで同時に構築する（CSVアップロード不要）。

商品管理番号の当たりの付け方（この2つの規則のみ・前方一致検索はしない）:
  1. 保存済みSKU対応表にあればその商品管理番号
  2. 無ければ末尾の「-数字」枝番をすべて除去したコード（gais0020-01-06→gais0020）
  3. それでも見つからなければコード自身を商品管理番号として再試行
"""
from lib.event import rms_api

from . import masters


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


def _get_variants(manage_number):
    """RMS Item API 2.0 から variants dict {SKU管理番号: 変数dict} を取得する。"""
    data = rms_api.get(f"/es/2.0/items/manage-numbers/{manage_number}")
    item = data.get("item", data)
    variants = item.get("variants") or {}
    return variants if isinstance(variants, dict) else {}


def match_variants(codes, parent, variants):
    """
    1商品のvariantsから、対象NEコードごとに (SKU管理番号, システム連携用SKU番号, 価格) を探す。
    照合順: システム連携用SKU番号一致 → SKU管理番号一致 → 単一SKUかつコード=親。
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
            out[key] = {"parent": parent, "sku": hit[0], "renkei": hit[1], "price": hit[2]}
    return out


def fetch_for_codes(codes, sku_table, on_progress=None):
    """
    NE商品コード群の現在価格とSKU情報をまとめて取得する。
    sku_table: 保存済みSKU対応表 {code小文字: (商品管理番号, SKU管理番号, 連携番号)}（商品管理番号の当たり付けに使用）
    返り値: (info={code小文字: {parent, sku, renkei, price}}, errors={対象: メッセージ}, warnings=[str])
    """
    info, errors, warnings = {}, {}, []
    if not rms_api.is_configured():
        warnings.append("Secrets に RMS_SERVICE_SECRET / RMS_LICENSE_KEY が未設定のため、"
                        "楽天価格は取得できません。")
        return info, errors, warnings

    variants_cache = {}   # 商品管理番号 → variants（同じ親への重複GETを避ける）

    def _variants(mn):
        if mn not in variants_cache:
            variants_cache[mn] = _get_variants(mn)
        return variants_cache[mn]

    total = len(codes)
    for done, code in enumerate(codes, start=1):
        key = masters.norm_key(code).lower()
        # 候補の商品管理番号: 保存済みSKU表 → 枝番なし系の候補（masters）の順に、実在するものを採る
        cands = []
        hit = sku_table.get(key)
        if hit and hit[0]:
            cands.append(masters.norm_key(hit[0]).lower())
        for c in masters.manage_number_candidates(key):
            c = c.lower()
            if c not in cands:
                cands.append(c)
        found_this = False
        for mn in cands:
            try:
                got = match_variants([key], mn, _variants(mn))
            except rms_api.RMSAuthError as e:
                warnings.append(str(e))
                if on_progress:
                    on_progress(total, total)
                return info, errors, warnings  # 認証切れは以降も失敗するので打ち切り
            except rms_api.RMSError:
                continue                        # その管理番号は存在しない → 次の候補へ
            except Exception:  # noqa: BLE001
                continue
            if got:
                info.update(got)
                found_this = True
                break
        if not found_this:
            errors[key] = "楽天に該当商品が見つかりません（商品管理番号を推定できず）"
        if on_progress:
            on_progress(done, total)
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
    """指定SKUの standardPrice（販売価格）だけを更新するPATCHボディ。
    ※CSV運用の「表示価格・二重価格文言管理番号」に相当するAPIフィールドは
      probe_item での実物調査後に追加を判断する（当面は販売価格のみ）。"""
    return {"variants": {str(sku): {"standardPrice": str(int(price))}
                         for sku, price in sku_prices.items()}}


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
