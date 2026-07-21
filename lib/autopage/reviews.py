# -*- coding: utf-8 -*-
"""楽天市場商品検索API（公開API）でレビュー点数・件数を取得する。

既存 lib/event/item_fetch.py と同じエンドポイントだが、
streamlit非依存（GitHub Actionsから実行可能）にするため独立させている。
"""
import requests

from . import creds

SEARCH_URL = "https://app.rakuten.co.jp/services/api/IchibaItem/Search/20220601"
TIMEOUT = 20


def is_configured():
    return bool(creds.get_secret("RAKUTEN_APP_ID"))


def fetch_review(shop_code, manage_number):
    """{review_count, review_average} を返す。取得不能ならNone。"""
    app_id = creds.get_secret("RAKUTEN_APP_ID")
    if not app_id:
        return None
    params = {
        "applicationId": app_id,
        "itemCode": f"{shop_code}:{manage_number}".lower(),
        "hits": 1,
    }
    resp = requests.get(SEARCH_URL, params=params, timeout=TIMEOUT)
    if resp.status_code == 404:
        return None  # 検索インデックス未反映等
    resp.raise_for_status()
    items = (resp.json() or {}).get("Items") or []
    if not items:
        return None
    it = items[0].get("Item", items[0])
    return {
        "review_count": int(it.get("reviewCount") or 0),
        "review_average": float(it.get("reviewAverage") or 0),
    }
