# -*- coding: utf-8 -*-
"""RMS Item API 2.0 / Category API 2.0 のラッパ。

既存の lib/event/rms_api.py（ESA認証・エラー変換）を流用する。

カテゴリAPI 2.0の実パス（2026-07-21 実測+Rakuten.RMS.Apiライブラリで確定）:
- GET /es/2.0/categories/item-mappings/manage-numbers/{mn}
    → {"categoryIds": [...]}（IDのみ）
- GET /es/2.0/categories/item-mappings/manage-numbers/{mn}?breadcrumb=true
    → {"categories": [{"categoryId","title","breadcrumbList":[{categoryId,title}...]}...]}
      breadcrumbListは上位階層（ルート→親）、自身はエントリ本体。パンくずはこれ1発で解決
- GET /es/2.0/categories/shop-categories/category-ids/{id}?breadcrumb=true → カテゴリ単体+上位
- GET /es/2.0/categories/shop-category-trees/category-set-ids/{setId} → ツリー（セット未使用は0）
- GET /es/2.0/categories/shop-category-set-lists → カテゴリセット一覧
※ RMS「WEB APIサービス」の利用機能で category.* の各GETが有効になっている必要がある
"""
from lib.event import rms_api


def get_item(manage_number):
    """商品を取得して主要フィールドを抜き出す。

    戻り値: {manage_number, title, sp_description, pc_description,
             sales_description, hide_item, raw}
    """
    data = rms_api.get(f"/es/2.0/items/manage-numbers/{manage_number}")
    item = data.get("item", data)
    desc = item.get("productDescription") or {}
    return {
        "manage_number": manage_number,
        "title": item.get("title") or "",
        "sp_description": desc.get("sp") or "",
        "pc_description": desc.get("pc") or "",
        "sales_description": item.get("salesDescription") or "",
        "hide_item": bool(item.get("hideItem")),
        "raw": item,
    }


def patch_sp_description(manage_number, sp_text):
    """スマホ用商品説明文のみを更新する（JSON Merge Patchなので他項目は無傷）。"""
    return rms_api.patch(
        f"/es/2.0/items/manage-numbers/{manage_number}",
        {"productDescription": {"sp": sp_text}},
    )


# ---- カテゴリ（パンくず用） ----
def get_item_mappings_with_breadcrumbs(manage_number):
    """商品→カテゴリのマッピングをパンくず付きで取得（生JSON）。"""
    return rms_api.get(
        f"/es/2.0/categories/item-mappings/manage-numbers/{manage_number}",
        params={"breadcrumb": "true"},
    )


def parse_breadcrumb_paths(raw):
    """パンくず付きマッピングJSONから、カテゴリごとの階層パスのリストを返す。

    戻り値: [[{id, name}...（ルート→リーフ）], ...] RMSの表示先カテゴリ順。
    """
    paths = []
    for cat in (raw or {}).get("categories") or []:
        if not isinstance(cat, dict):
            continue
        path = []
        for b in cat.get("breadcrumbList") or []:
            if isinstance(b, dict) and str(b.get("title") or "").strip():
                path.append({"id": b.get("categoryId"),
                             "name": str(b["title"]).strip()})
        if str(cat.get("title") or "").strip():
            path.append({"id": cat.get("categoryId"),
                         "name": str(cat["title"]).strip()})
        if path:
            paths.append(path)
    return paths


def resolve_item_breadcrumb(manage_number, position="last"):
    """商品のパンくず階層 [{id,name}...]（ルート→リーフ）を返す。解決不能なら[]。

    position: first=1番目の表示先カテゴリ / last=最終番号（EC-UPの設定に対応）
    """
    raw = get_item_mappings_with_breadcrumbs(manage_number)
    paths = parse_breadcrumb_paths(raw)
    if not paths:
        return []
    return paths[0] if position == "first" else paths[-1]


def diagnostics(manage_number, extra_paths=None):
    """疎通診断。各エンドポイントの生レスポンス（or エラー）を返す。
    extra_paths で任意のGETパスも試せる。"""
    probes = [
        ("item_mapping_breadcrumb",
         lambda: get_item_mappings_with_breadcrumbs(manage_number)),
        ("breadcrumb_resolved", lambda: {
            "path_last": resolve_item_breadcrumb(manage_number, "last"),
            "path_first": resolve_item_breadcrumb(manage_number, "first"),
        }),
        ("category_set_lists", lambda: rms_api.get(
            "/es/2.0/categories/shop-category-set-lists")),
        ("category_tree_set0", lambda: rms_api.get(
            "/es/2.0/categories/shop-category-trees/category-set-ids/0")),
    ]
    for path in (extra_paths or []):
        path = str(path).strip()
        if path:
            probes.append((f"GET {path}", lambda p=path: rms_api.get(p)))
    results = {}
    for label, fn in probes:
        try:
            results[label] = {"ok": True, "data": fn()}
        except Exception as e:  # noqa: BLE001
            results[label] = {"ok": False, "error": str(e)}
    return results
