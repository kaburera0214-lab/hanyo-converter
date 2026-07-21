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
# 実測済み（2026-07-21 edin0033）: item-mappings は {"categoryIds": ["149","1029"], ...} と
# IDのみ返す。名前・階層はカテゴリツリーAPIから解決する。
def get_item_category_raw(manage_number):
    """商品→カテゴリのマッピング生JSONを返す（診断用にも使う）。"""
    return rms_api.get(
        f"/es/2.0/categories/item-mappings/manage-numbers/{manage_number}")


def get_item_category_ids(manage_number):
    """商品に設定されたカテゴリID一覧（RMS表示先カテゴリの順序どおり）。"""
    raw = get_item_category_raw(manage_number)
    ids = raw.get("categoryIds")
    if isinstance(ids, list):
        return [str(i) for i in ids if str(i).strip()]
    # 形状が違う場合のフォールバック（再帰的にcategoryIdを拾う）
    found = []
    _walk_category_entries(raw, found)
    return [str(c["id"]) for c in found if c.get("id") is not None]


_TREE_CACHE = {"map": None}


def _walk_tree(node, ancestors, out):
    """カテゴリツリーを再帰的に辿り {categoryId: [{id,name}...]（ルート→当該）} を作る。
    形状のブレ（category入れ子/children名）に耐える防御的実装。"""
    if isinstance(node, list):
        for v in node:
            _walk_tree(v, ancestors, out)
        return
    if not isinstance(node, dict):
        return
    inner = node.get("category") if isinstance(node.get("category"), dict) else None
    src = inner or node
    cid = src.get("categoryId", src.get("id"))
    title = (src.get("title") or src.get("name") or src.get("categoryName") or "")
    if cid is not None and str(title).strip():
        path = ancestors + [{"id": str(cid), "name": str(title).strip()}]
        out[str(cid)] = path
        children = (node.get("children") or node.get("childNodes")
                    or node.get("categories") or [])
        _walk_tree(children, path, out)
    else:
        for v in node.values():
            _walk_tree(v, ancestors, out)


def _find_tree_ids(node, found):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "categoryTreeId" and v is not None:
                found.append(str(v))
            else:
                _find_tree_ids(v, found)
    elif isinstance(node, list):
        for v in node:
            _find_tree_ids(v, found)


def get_category_path_map(force=False):
    """全ショップカテゴリの {categoryId: 階層パス} を返す（プロセス内キャッシュ）。
    ツリーAPIが利用できない場合は空dictを返す（呼び出し側が個別取得にフォールバック）。"""
    if _TREE_CACHE["map"] is not None and not force:
        return _TREE_CACHE["map"]
    out = {}
    try:
        data = rms_api.get("/es/2.0/categories/category-trees")
        _walk_tree(data, [], out)
        if not out:
            tree_ids = []
            _find_tree_ids(data, tree_ids)
            for tid in dict.fromkeys(tree_ids):
                detail = rms_api.get(f"/es/2.0/categories/category-trees/{tid}")
                _walk_tree(detail, [], out)
    except rms_api.RMSError:
        pass  # ID無しツリー一覧は404（実測）。個別取得フォールバックに任せる
    _TREE_CACHE["map"] = out
    return out


def get_shop_category(category_id):
    """ショップカテゴリ単体を取得（GET shop-categories/{id}）。"""
    return rms_api.get(f"/es/2.0/categories/shop-categories/{category_id}")


def resolve_item_breadcrumb(manage_number, position="last"):
    """商品のパンくず階層 [{id,name}...]（ルート→リーフ）を返す。解決不能なら[]。"""
    ids = get_item_category_ids(manage_number)
    if not ids:
        return []
    cid = ids[0] if position == "first" else ids[-1]
    path = get_category_path_map().get(str(cid), [])
    if path:
        return path
    # フォールバック: カテゴリ単体取得から名前（あればbreadcrumb階層）を拾う
    try:
        raw = get_shop_category(cid)
    except rms_api.RMSError:
        return []
    found = []
    _walk_category_entries(raw, found)
    seen, out = set(), []
    for c in found:
        name = c["name"].strip()
        if not name:
            continue
        key = (str(c["id"]), name)
        if key in seen:
            continue
        seen.add(key)
        out.append({"id": c["id"], "name": name})
    # 末尾要素のIDが当該カテゴリでない場合でも、階層らしき並びをそのまま使う
    return out


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


def diagnostics(manage_number, extra_paths=None):
    """疎通診断。各エンドポイントの生レスポンス（or エラー）を返す。

    実測結果（2026-07-21）:
    - item-mappings → {"categoryIds": [...]} でIDのみ
    - GET categories/shop-categories → 405（ID指定が必要）
    - GET categories/category-trees（ID無し） → 404
    extra_paths で任意のGETパスを試せる（形状特定の反復をデプロイ無しで行うため）。
    """
    probes = [
        ("category_mapping", lambda: get_item_category_raw(manage_number)),
        ("shop_category_149", lambda: rms_api.get(
            "/es/2.0/categories/shop-categories/149")),
        ("shop_category_1029", lambda: rms_api.get(
            "/es/2.0/categories/shop-categories/1029")),
        ("category_tree_0", lambda: rms_api.get(
            "/es/2.0/categories/category-trees/0")),
        ("category_tree_1", lambda: rms_api.get(
            "/es/2.0/categories/category-trees/1")),
        ("breadcrumb_resolved", lambda: {
            "category_ids": get_item_category_ids(manage_number),
            "path_last": resolve_item_breadcrumb(manage_number, "last"),
            "path_first": resolve_item_breadcrumb(manage_number, "first"),
            "tree_map_size": len(get_category_path_map(force=True)),
        }),
    ]
    for path in (extra_paths or []):
        path = str(path).strip()
        if path:
            probes.append((f"GET {path}",
                           lambda p=path: rms_api.get(p)))
    results = {}
    for label, fn in probes:
        try:
            results[label] = {"ok": True, "data": fn()}
        except Exception as e:  # noqa: BLE001
            results[label] = {"ok": False, "error": str(e)}
    return results
