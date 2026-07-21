# -*- coding: utf-8 -*-
"""各システムのHTMLブロック生成。

楽天スマホ用商品説明文で利用可能なタグのみ使う（style/class属性は不可）:
a, b, br, center, font, hr, img, p, table, td, th, tr など。
各関数は表示条件を満たさない場合 None を返す（ブロックを入れない）。
"""
import html
import re
import urllib.parse

_BRACKET_PAIRS = [
    ("(", ")"), ("（", "）"), ("【", "】"), ("《", "》"),
    ("[", "]"), ("〔", "〕"), ("<", ">"), ("「", "」"),
]


def strip_brackets(name):
    """商品名からカッコとカッコ内を削除（EC-UPの「カッコ削除」再現）。"""
    out = str(name or "")
    for op, cl in _BRACKET_PAIRS:
        out = re.sub(re.escape(op) + "[^" + re.escape(op + cl) + "]*" + re.escape(cl), "", out)
    return re.sub(r"\s+", " ", out).strip()


def is_hidden(name, manage_number, hidden_cfg):
    """非表示商品設定（部分一致）に該当するか。"""
    name = str(name or "")
    mn = str(manage_number or "")
    for kw in (hidden_cfg or {}).get("name_contains", []):
        if kw and kw in name:
            return True
    for kw in (hidden_cfg or {}).get("manage_number_contains", []):
        if kw and kw in mn:
            return True
    return False


def _esc(text):
    return html.escape(str(text or ""), quote=True)


# ---- パンくずリスト ----
def breadcrumb(sys_cfg, shop_code, shop_id, categories):
    """categories: [{"id": str|int|None, "name": str}, ...] ルート→リーフの順。

    link=category: ショップ内カテゴリページへ
    link=search  : ショップ内検索結果ページへ（例外キーワード該当時はカテゴリページ）
    """
    if not categories:
        return None
    size = "2" if sys_cfg.get("font_size") == "small" else "3"
    link_mode = sys_cfg.get("link", "category")
    exceptions = sys_cfg.get("exception_keywords", [])
    shop_top = f"https://item.rakuten.co.jp/{shop_code}/"

    parts = [f'<a href="{shop_top}">ショップトップ</a>']
    for cat in categories:
        name = str(cat.get("name") or "").strip()
        if not name:
            continue
        cat_id = cat.get("id")
        use_search = (link_mode == "search"
                      and not any(kw and kw in name for kw in exceptions))
        if use_search:
            kw = urllib.parse.quote(name)
            url = f"https://search.rakuten.co.jp/search/mall/{kw}/?sid={shop_id}"
        elif cat_id is not None and str(cat_id).strip():
            cid = str(cat_id).strip()
            url = f"https://item.rakuten.co.jp/{shop_code}/c/{cid.zfill(10)}/"
        else:
            parts.append(_esc(name))
            continue
        parts.append(f'<a href="{url}">{_esc(name)}</a>')
    if len(parts) <= 1:
        return None
    return f'<p><font size="{size}">' + " &gt; ".join(parts) + "</font></p>"


# ---- 商品スコア ----
def score(sys_cfg, item_url, review_average, review_count):
    """レビューが閾値以上の場合に星スコアのバッジを返す。"""
    try:
        avg = float(review_average or 0)
        cnt = int(review_count or 0)
    except (TypeError, ValueError):
        return None
    if avg < float(sys_cfg.get("min_average", 4.0)) or cnt < int(sys_cfg.get("min_count", 3)):
        return None
    full = int(round(avg))
    stars = "★" * full + "☆" * (5 - full)
    avg_disp = f"{avg:.2f}".rstrip("0").rstrip(".")
    return (
        '<p align="center">'
        f'<a href="{item_url}#review">'
        f'<font size="4" color="#f39800">{stars}</font> '
        f'<font size="4" color="#e60012"><b>{avg_disp}</b></font>'
        f'<font size="2">（{cnt}件のレビュー）</font>'
        "</a></p>"
    )


# ---- 更新日 ----
def update_date(sys_cfg, date_str):
    if not date_str:
        return None
    align = "left" if sys_cfg.get("align") == "left" else "right"
    return (f'<p align="{align}"><font size="2" color="#999999">'
            f"更新日：{_esc(date_str)}</font></p>")


# ---- 商品グリッド（同時購入・類似商品の共通部品、Phase 3で使用） ----
def item_grid(band_title, items, shop_code, name_cfg):
    """items: [{"manage_number","name","price","image_url"}] を2列テーブルで並べる。"""
    if not items:
        return None
    show_name = (name_cfg or {}).get("show", True)
    do_strip = (name_cfg or {}).get("strip_brackets", True)
    cells = []
    for it in items:
        mn = it.get("manage_number", "")
        url = f"https://item.rakuten.co.jp/{shop_code}/{mn}/"
        name = strip_brackets(it.get("name")) if do_strip else str(it.get("name") or "")
        img = it.get("image_url") or ""
        price = it.get("price")
        inner = ""
        if img:
            inner += f'<img src="{img}" width="100%"><br>'
        if show_name and name:
            inner += f'<font size="2">{_esc(name[:60])}</font><br>'
        if price:
            inner += f'<font size="3" color="#e60012"><b>{int(price):,}円</b></font>'
        cells.append(f'<td width="50%" valign="top"><a href="{url}">{inner}</a></td>')
    rows = []
    for i in range(0, len(cells), 2):
        pair = cells[i:i + 2]
        if len(pair) == 1:
            pair.append('<td width="50%"></td>')
        rows.append("<tr>" + "".join(pair) + "</tr>")
    title_html = ""
    if band_title:
        title_html = (f'<p align="center"><font size="4"><b>{_esc(band_title)}</b>'
                      "</font></p>")
    return title_html + '<table border="0" width="100%">' + "".join(rows) + "</table>"
