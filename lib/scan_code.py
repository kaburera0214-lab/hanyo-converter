# -*- coding: utf-8 -*-
"""便種・個口の変更（pages/24）：スキャンした値（または手で打った値）を、どの注文の何番かに直す。

実物のバーコードの中身（2026-11-26 に PDF から読んで確かめた）：
- NEの納品書：NW-7（Codabar）。「D」＋伝票番号を0で埋めた10桁＋「D」（例 D0000201722D → 201722）。
  スキャナーの設定で両端の D が付く／付かないが変わるので、どちらでも読む
- Eシスのピッキングリスト：Code 93。「J81583」＋チェック用の2文字。スキャナーはふつうチェック用の文字を送らないが、
  送る設定のときは計算して合えば落とす

以前スキャンして困ったこと：①桁数（0埋めの10桁が「伝票番号は12文字まで・数字だけ」に合わなかった）
②最後の改行で「確かめる」が押されてしまう（ページ側で、番号の欄をフォームの外に出して直した）
"""
from __future__ import annotations

import re

C93 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-. $/+%"
NE_SCAN = re.compile(r"^[A-D]?0*(\d{1,10})[A-D]?$")
J_SCAN = re.compile(r"^J(\d{4,7})$")


def _c93_check(text: str) -> str:
    """Code 93 のチェック用の2文字（C, K）。英数字以外のシフト文字が要るときは None 相当の空文字。"""
    vals = [C93.index(ch) for ch in text]
    c = sum(v * ((i % 20) + 1) for i, v in enumerate(reversed(vals))) % 47
    k = sum(v * ((i % 15) + 1) for i, v in enumerate(reversed(vals + [c]))) % 47
    if c >= 43 or k >= 43:
        return ""
    return C93[c] + C93[k]


def parse(raw: str) -> tuple[str, str]:
    """("ne" | "te", 番号) を返す。読めなければ ("", 理由)。"""
    s = re.sub(r"\s+", "", raw or "").upper()
    if not s:
        return "", ""
    if s.startswith("J"):
        # チェック用の2文字ごと送るスキャナー：計算して合えば落とす（J8158345 → J81583。数字のチェック文字は
        # 番号の続きに見えるので、先に確かめる）
        if len(s) >= 7 and all(ch in C93 for ch in s) and J_SCAN.match(s[:-2]) and _c93_check(s[:-2]) == s[-2:]:
            return "te", s[:-2]
        if J_SCAN.match(s):
            return "te", s
        return "", "J から始まる番号の形が違います（例 J81583）。ピッキングリストのバーコードを読み直してください"
    m = NE_SCAN.match(s)
    if m and int(m.group(1)) > 0:
        return "ne", str(int(m.group(1)))
    return "", "読めない番号です。NEの納品書か、Eシスのピッキングリストのバーコードを読んでください"
