# -*- coding: utf-8 -*-
"""
イベントLPのHTML生成(Jinja2)。

楽天GOLDの制約に合わせ、外部JSなし・CSS内包・全リンクhttps・UTF-8完結の
1ファイルHTMLを出力する。レスポンシブは1ソース(メディアクエリ)。
"""
import datetime
import os

from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")

DEFAULT_THEME_COLOR = "#bf0000"  # 楽天レッド系


def _yen(value):
    """1234 -> '1,234円'。Noneや不正値は空文字。"""
    try:
        return f"{int(float(value)):,}円"
    except (TypeError, ValueError):
        return ""


def _fmt_dt(text):
    """'2026-09-04 20:00' -> '9/4(木) 20:00'。パースできなければ原文を返す。"""
    s = str(text or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            dt = datetime.datetime.strptime(s, fmt)
            wd = "月火水木金土日"[dt.weekday()]
            if "%H" in fmt:
                return f"{dt.month}/{dt.day}({wd}) {dt.strftime('%H:%M')}"
            return f"{dt.month}/{dt.day}({wd})"
        except ValueError:
            continue
    return s


def _stars(average):
    """レビュー平均 4.3 -> '★★★★☆' (四捨五入)。"""
    try:
        n = int(round(float(average)))
    except (TypeError, ValueError):
        n = 0
    n = max(0, min(5, n))
    return "★" * n + "☆" * (5 - n)


def _env():
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html", "j2"]),
    )
    env.filters["yen"] = _yen
    env.filters["fmt_dt"] = _fmt_dt
    env.filters["stars"] = _stars
    return env


def render_lp(event, items):
    """
    event: {イベント名, キャッチコピー, 期間開始, 期間終了, テーマカラー, セクション(list)}
    items: {管理番号: {name, price, image_url, review_count, review_average, url}}
    戻り値: HTML文字列(UTF-8想定)
    """
    sections = []
    for i, sec in enumerate(event.get("セクション", [])):
        s = dict(sec)
        s["anchor"] = f"sec{i + 1}"
        sections.append(s)
    ctx = {
        "event": {
            "name": event.get("イベント名", ""),
            "catch": event.get("キャッチコピー", ""),
            "start": event.get("期間開始", ""),
            "end": event.get("期間終了", ""),
            "theme_color": (event.get("テーマカラー") or "").strip() or DEFAULT_THEME_COLOR,
        },
        "sections": sections,
        "items": items or {},
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    return _env().get_template("lp_base.html.j2").render(**ctx)
