# -*- coding: utf-8 -*-
"""RMS Item API 2.0 / Category API のラッパ。

既存の lib/event/rms_api.py（ESA認証・エラー変換）を流用する。
カテゴリ系APIはレスポンス形状が環境で異なる可能性があるため、
パースは防御的に行い、診断用に生JSONも返せるようにしている。
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
def get_item_category_raw(manage_number):
    """商品→カテゴリのマッピング生JSONを返す（診断用にも使う）。"""
    return rms_api.get(
        f"/es/2.0/categories/item-mappings/manage-numbers/{manage_number}")


def _walk_category_entries(node, found):
    """レスポンス形状の揺れに耐えるため、再帰的にcategoryId/名前の組を拾う。"""
    if isinstance(node, dict):
        cid = node.get("categoryId") or node.get("id")
        name = (node.get("title") or node.get("name")
                or node.get("categoryName") or "")
        if cid is not None or name:
            found.append({"id": cid, "name": str(name), "raw": node})
        for v in node.values():
            _walk_category_entries(v, found)
    elif isinstance(node, list):
        for v in node:
            _walk_category_entries(v, found)


def parse_item_categories(raw):
    """マッピング生JSONから カテゴリ候補リスト[{id, name}] を返す。

    breadcrumb（階層パス）が入っている場合はそれを優先。
    名前が取れない場合は空リスト（パンくずはスキップされる）。
    """
    # よくある形: {"itemMapping": {"categories": [{"categoryId":..., "breadcrumbs":[...]}]}}
    candidates = []
    _walk_category_entries(raw, candidates)
    # 名前を持つものだけ・重複除去（出現順維持）
    seen, out = set(), []
    for c in candidates:
        name = c["name"].strip()
        if not name:
            continue
        key = (str(c["id"]), name)
        if key in seen:
            continue
        seen.add(key)
        out.append({"id": c["id"], "name": name})
    return out


def select_breadcrumb_path(categories, position="last"):
    """カテゴリ候補から使用する1系統を選ぶ。

    現状は平坦なリストしか取れないため、first=先頭 / last=末尾 の1件を返す。
    （EC-UPの「1番目/最終番号」設定に対応。階層パスが取れる形状なら
    parse_item_categoriesが順序を保持しているのでそのまま複数段になる。）
    """
    if not categories:
        return []
    if position == "first":
        return [categories[0]]
    return [categories[-1]]


def diagnostics(manage_number):
    """疎通診断。各エンドポイントの生レスポンス（or エラー）を返す。"""
    results = {}
    for label, fn in [
        ("item_get", lambda: rms_api.get(
            f"/es/2.0/items/manage-numbers/{manage_number}")),
        ("category_mapping", lambda: get_item_category_raw(manage_number)),
    ]:
        try:
            results[label] = {"ok": True, "data": fn()}
        except Exception as e:  # noqa: BLE001
            results[label] = {"ok": False, "error": str(e)}
    return results
