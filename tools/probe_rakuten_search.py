# -*- coding: utf-8 -*-
"""
楽天の商品検索API（RMS Item API 2.0）がこの店舗のライセンスで使えるかを確かめる。

価格改定の📡取得は、商品管理番号が商品コードから導けない商品（2026-09-04 #1256の
maru0542-xx → maru0260）を商品検索APIで探す。この機能がRMSの「WEB APIサービス」で
有効になっていないと検索できないため、使えるかどうかをここで確認する。

使い方（RMS_SERVICE_SECRET / RMS_LICENSE_KEY を環境変数か .streamlit/secrets.toml に置いて）:
    python tools/probe_rakuten_search.py                  # #1256 の実例で確認
    python tools/probe_rakuten_search.py maru0542-06 ...  # 任意のNE商品コードで確認
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.event import rms_api          # noqa: E402
from lib.pricing import rakuten_price  # noqa: E402


def main(codes):
    if not rms_api.is_configured():
        print("NG: RMS_SERVICE_SECRET / RMS_LICENSE_KEY が未設定です。")
        return 2

    print(f"検索API: GET {rms_api.BASE_URL}{rakuten_price.ITEM_SEARCH_PATH}")
    ok = True
    for code in codes:
        base = code.split("-", 1)[0]
        for params in ({"merchantDefinedSkuId": code}, {"itemNumber": base}):
            label = ", ".join(f"{k}={v}" for k, v in params.items())
            try:
                found = rakuten_price.search_manage_numbers(**params)
            except Exception as e:  # noqa: BLE001
                ok = False
                print(f"  NG  {label}: {e}")
                continue
            print(f"  OK  {label}: 商品管理番号 {found or '（該当なし）'}")

    print()
    info, errors, warnings = rakuten_price.fetch_for_codes(codes, {})
    for w in warnings:
        print(f"警告: {w}")
    print("取得できた価格:", rakuten_price.to_prices(info))
    print("SKU対応表:", rakuten_price.to_sku_table(info))
    for c, r in errors.items():
        print(f"取得できず: {c} … {r}")
    return 0 if ok and not errors else 1


if __name__ == "__main__":
    args = sys.argv[1:] or ["maru0542-06", "maru0542-04", "maru0542-03",
                            "maru0542-01", "maru0542-09"]
    sys.exit(main(args))
