# -*- coding: utf-8 -*-
"""
全銀の金融機関マスタ(zengin_code)を使い、銀行番号・支店番号から
銀行名・支店名を引く。マッチングや表示に使用。

銀行番号は4桁、支店番号は3桁ゼロ埋めで照合する。
"""
import re
import unicodedata

_BANKS = None


def _banks():
    global _BANKS
    if _BANKS is None:
        try:
            from zengin_code import Bank
            _BANKS = Bank.all
        except Exception:  # noqa: BLE001 - データ未導入でも落とさない
            _BANKS = {}
    return _BANKS


def bank_name(code):
    """銀行番号(4桁) -> 銀行名。見つからなければ空。"""
    if not code:
        return ""
    code = str(code).strip().zfill(4)
    bk = _banks().get(code)
    return bk.name if bk else ""


def branch_name(bank_code, branch_code):
    """銀行番号(4桁)+支店番号(3桁) -> 支店名。見つからなければ空。"""
    if not bank_code or not branch_code:
        return ""
    bank_code = str(bank_code).strip().zfill(4)
    branch_code = str(branch_code).strip().zfill(3)
    bk = _banks().get(bank_code)
    if not bk:
        return ""
    br = bk.branches.get(branch_code)
    return br.name if br else ""


def normalize_bank(name):
    """銀行名の表記ゆれ吸収(『銀行』『信用金庫』等や記号を除去)。"""
    if not name:
        return ""
    s = unicodedata.normalize("NFKC", str(name)).strip()
    s = s.replace("株式会社", "")
    s = re.sub(r"(銀行|信用金庫|信用組合|信金|農業協同組合|労働金庫|ＪＡ|JA)", "", s)
    s = re.sub(r"[\s　()（）・]", "", s)
    return s.lower()


def normalize_branch(name):
    """支店名の表記ゆれ吸収(『支店』『出張所』『営業部』等を除去)。"""
    if not name:
        return ""
    s = unicodedata.normalize("NFKC", str(name)).strip()
    s = re.sub(r"(支店|出張所|営業部|営業所|本店)", "", s)
    s = re.sub(r"[\s　()（）・]", "", s)
    return s.lower()


def digits(s):
    return "".join(ch for ch in str(s) if ch.isdigit())
