# -*- coding: utf-8 -*-
"""マーカー方式の説明文合成器。

スマホ用商品説明文を「自社作成部分」と「自動生成ゾーン（上/下）」に分離する。
ゾーンはHTMLコメントのマーカーで囲む（EC-UPと同じ方式）:

    <!--AP_TOP_START-->（パンくず等）<!--AP_TOP_END-->自社作成部分<!--AP_BTM_START-->（スコア等）<!--AP_BTM_END-->

- 楽天のバイト数は全角=2byte換算（cp932エンコード長）で数える
- 上限を超える場合は優先度の低いブロックから間引く（EC-UPの「オートリッチ」相当）
- strip_generated() で自動生成ゾーンを完全撤去でき、自社作成部分は無傷で残る
"""
import re

TOP_START = "<!--AP_TOP_START-->"
TOP_END = "<!--AP_TOP_END-->"
BTM_START = "<!--AP_BTM_START-->"
BTM_END = "<!--AP_BTM_END-->"

_ZONE_RE = re.compile(
    re.escape(TOP_START) + ".*?" + re.escape(TOP_END) + "|" +
    re.escape(BTM_START) + ".*?" + re.escape(BTM_END),
    re.S,
)
# 万一片割れだけ残ったマーカーの掃除用
_ORPHAN_RE = re.compile(r"<!--AP_(?:TOP|BTM)_(?:START|END)-->")


def rakuten_len(text):
    """楽天の文字数カウント（全角=2byte）。cp932でエンコードできない文字は1byte扱い。"""
    return len(str(text or "").encode("cp932", errors="replace"))


def strip_generated(text):
    """自動生成ゾーンを取り除き、自社作成部分だけを返す。"""
    text = _ZONE_RE.sub("", str(text or ""))
    return _ORPHAN_RE.sub("", text)


def compose(own_text, blocks, byte_limit, byte_reserve):
    """自社作成部分と生成ブロックを合成する。

    blocks: [{"system": str, "zone": "top"|"bottom", "html": str}] 優先度の高い順。
    バイト上限に収まらない場合は末尾（優先度最低）のブロックから外す。

    戻り値: (合成後テキスト, 採用したsystemリスト, 間引いたsystemリスト)
    """
    own_text = str(own_text or "")
    budget = int(byte_limit) - int(byte_reserve) - rakuten_len(own_text)
    marker_cost = rakuten_len(TOP_START + TOP_END + BTM_START + BTM_END)

    kept = list(blocks)
    dropped = []
    while kept:
        total = sum(rakuten_len(b["html"]) for b in kept) + marker_cost
        if total <= budget:
            break
        dropped.insert(0, kept.pop()["system"])
    if not kept:
        # 1つも入らない場合はマーカー自体入れない
        return own_text, [], dropped + []

    top_html = "".join(b["html"] for b in kept if b["zone"] == "top")
    btm_html = "".join(b["html"] for b in kept if b["zone"] == "bottom")
    out = own_text
    if top_html:
        out = f"{TOP_START}{top_html}{TOP_END}{out}"
    if btm_html:
        out = f"{out}{BTM_START}{btm_html}{BTM_END}"
    return out, [b["system"] for b in kept], dropped
