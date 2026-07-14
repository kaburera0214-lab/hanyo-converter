# -*- coding: utf-8 -*-
"""
楽天の現在販売価格の自動取得（RMS Item API 2.0）。

楽天販売価格は楽天側でしか管理していないため、価格改定の「現販売価格」は
RMSからSKU単位で取得する（クライアントはイベントLPモジュールの rms_api を流用。
Secrets: RMS_SERVICE_SECRET / RMS_LICENSE_KEY）。

Item API 2.0 の variants は {SKU管理番号: {...standardPrice...}} の形なので、
SKU対応表（masters.sku_lookup）で NE商品コード→(商品管理番号, SKU管理番号) に
変換してから引く。取得できなかった商品は NE売価×1.1 にフォールバックする（呼び出し側）。
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


def fetch_sku_prices(manage_numbers, on_progress=None):
    """
    商品管理番号ごとにRMSからSKU別販売価格を取得する。
    返り値: ({(商品管理番号小文字, SKU管理番号): 価格}, errors={管理番号: メッセージ}, warnings=[str])
    on_progress: f(done, total) 進捗コールバック（Streamlitのprogress用）。
    """
    prices, errors, warnings = {}, {}, []
    if not rms_api.is_configured():
        warnings.append("Secrets に RMS_SERVICE_SECRET / RMS_LICENSE_KEY が未設定のため、"
                        "楽天価格は取得できません（NE売価×1.1で計算します）。")
        return prices, errors, warnings
    mns = [str(m).strip() for m in manage_numbers if str(m).strip()]
    for i, mn in enumerate(dict.fromkeys(mns)):  # 順序保持で重複除去
        try:
            data = rms_api.get(f"/es/2.0/items/manage-numbers/{mn}")
            item = data.get("item", data)
            variants = item.get("variants") or {}
            if isinstance(variants, dict):
                for sku_no, v in variants.items():
                    if isinstance(v, dict):
                        p = _variant_price(v)
                        if p is not None:
                            prices[(mn.lower(), str(sku_no))] = p
        except rms_api.RMSAuthError as e:
            warnings.append(str(e))
            break  # 認証切れは以降も失敗するので打ち切り
        except rms_api.RMSError as e:
            errors[mn] = str(e)
        except Exception as e:  # noqa: BLE001
            errors[mn] = f"取得失敗: {e}"
        if on_progress:
            on_progress(i + 1, len(set(mns)))
    return prices, errors, warnings


def resolve_pairs(codes, sku_table):
    """
    NE商品コードのリスト → {NEコード小文字: (商品管理番号小文字, SKU管理番号)}。
    SKU対応表にあればそれを、無ければ「枝番を落とした親＋コード自身がSKU」とみなす。
    """
    pairs = {}
    for code in codes:
        key = masters.norm_key(code).lower()
        hit = sku_table.get(key)
        if hit:
            pairs[key] = (hit[0].lower(), str(hit[1]))
        else:
            pairs[key] = (masters.parent_code(key).lower(), masters.norm_key(code))
    return pairs


def prices_by_code(codes, sku_table, sku_prices):
    """fetch_sku_prices の結果を {NEコード小文字: 価格} に変換する。"""
    pairs = resolve_pairs(codes, sku_table)
    out = {}
    for code, pair in pairs.items():
        p = sku_prices.get(pair)
        if p is not None:
            out[code] = p
    return out
