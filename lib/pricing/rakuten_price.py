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

    # 親（商品管理番号）ごとに対象コードをまとめる
    groups = {}
    for code in codes:
        key = masters.norm_key(code).lower()
        hit = sku_table.get(key)
        parent = (hit[0] if hit else masters.parent_code(key)).lower()
        groups.setdefault(parent, []).append(key)

    total = len(groups)
    done = 0
    for parent, member_codes in groups.items():
        try:
            found = match_variants(member_codes, parent, _get_variants(parent))
        except rms_api.RMSAuthError as e:
            warnings.append(str(e))
            break  # 認証切れは以降も失敗するので打ち切り
        except rms_api.RMSError:
            found = {}
        except Exception as e:  # noqa: BLE001
            errors[parent] = f"取得失敗: {e}"
            found = {}
        info.update(found)
        # 見つからなかったコードは、コード自身を商品管理番号として再試行
        for code in member_codes:
            if code in info or code == parent:
                continue
            try:
                info.update(match_variants([code], code, _get_variants(code)))
            except rms_api.RMSAuthError as e:
                warnings.append(str(e))
                break
            except Exception:  # noqa: BLE001
                pass
            if code not in info:
                errors[code] = "楽天に該当商品が見つかりません（商品管理番号を推定できず）"
        done += 1
        if on_progress:
            on_progress(done, total)
    return info, errors, warnings


def probe_item(manage_number):
    """商品1件の生レスポンスを返す（配送方法セット等のフィールド特定・API自動化の調査用）。"""
    return rms_api.get(f"/es/2.0/items/manage-numbers/{manage_number}")


def to_sku_table(info):
    """fetch_for_codes の結果 → SKU対応表形式 {code: (商品管理番号, SKU管理番号, 連携番号)}。"""
    return {code: (d["parent"], d["sku"], d["renkei"]) for code, d in info.items()}


def to_prices(info):
    """fetch_for_codes の結果 → {code: 価格}（価格が取れたものだけ）。"""
    return {code: d["price"] for code, d in info.items() if d.get("price")}
