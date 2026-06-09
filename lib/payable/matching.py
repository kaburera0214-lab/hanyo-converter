# -*- coding: utf-8 -*-
"""
ネクストエンジン発注データと請求書の突合。

NE発注データの列(可変だが代表):
  発注伝票番号, 仕入先cd, 仕入先名, 発行者, 希望納期, 特記事項, 金額, 作成日, 状態
  - 仕入先cd  : 安定キー(n001 等)
  - 仕入先名  : 【Z11/M2】株式会社野中製作所【0】 のように接頭コード・●・株式会社付き
  - 金額      : カンマ付き文字列
  - 作成日    : 発注日(YYYY/MM/DD)。締め=作成日の1日〜末日で月を判定

突合方針(確定):
  - NEは仕入先cd単位で対象月内の金額を合算
  - 請求書(会社名+当月請求額)を、マスタの「会社名/別名/NE仕入先cd」経由でNE合算額に突合
  - 会社名+金額(許容誤差設定可)。一致=突合OK、金額差/発注なし=突合エラー
"""
import re
import unicodedata


def normalize_name(s):
    """会社名の表記ゆれを吸収した突合キー。NE仕入先名の接頭コードも除去。"""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s)).strip()
    # 先頭・末尾の【...】(倉庫コード/個口数など)を除去
    s = re.sub(r"【.*?】", "", s)
    # 装飾記号
    s = re.sub(r"[●○◆★☆▲△■□※]", "", s)
    # 法人格
    s = s.replace("株式会社", "").replace("有限会社", "").replace("合同会社", "")
    s = s.replace("(株)", "").replace("(有)", "")
    # 全角カッコ内の補足・末尾※注記
    s = re.sub(r"[（(].*?[)）]", "", s)
    s = re.sub(r"※.*$", "", s)
    # 空白・記号
    s = re.sub(r"[\s　・．\.,，]", "", s)
    return s.lower()


def parse_amount(v):
    """ '12,600' '950円(税抜)' 等 → int。数字が無ければ0。"""
    if v is None:
        return 0
    s = unicodedata.normalize("NFKC", str(v))
    m = re.findall(r"-?\d+", s.replace(",", ""))
    return int(m[0]) if m else 0


def _ym(date_str):
    """ '2026/05/29' → (2026,5)。失敗時 None。"""
    s = unicodedata.normalize("NFKC", str(date_str))
    m = re.match(r"\s*(\d{4})\D+(\d{1,2})", s)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def read_ne_rows(file_or_bytes):
    """NE発注CSV(cp932/utf-8どちらも許容)を辞書リストで返す。列名は正規化。"""
    import csv
    import io
    if isinstance(file_or_bytes, (bytes, bytearray)):
        raw = bytes(file_or_bytes)
    elif hasattr(file_or_bytes, "read"):
        raw = file_or_bytes.read()
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
    else:
        with open(file_or_bytes, "rb") as fp:
            raw = fp.read()
    text = None
    for enc in ("cp932", "utf-8-sig", "utf-8"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("cp932", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for row in reader:
        rows.append({(k or "").strip(): (v or "").strip() for k, v in row.items()})
    return rows


def _col(row, *candidates):
    """列名ゆらぎ吸収。candidatesのうち最初に見つかった列の値。"""
    for c in candidates:
        if c in row:
            return row[c]
    # 部分一致
    for k in row:
        for c in candidates:
            if c in k:
                return row[k]
    return ""


def aggregate_ne(rows, year, month):
    """
    対象年月(作成日基準)でNE発注を仕入先cd単位に合算。
    戻り値: {仕入先cd: {"合算額":int, "件数":int, "仕入先名":str,
                        "正規名":str, "伝票":[番号...]}}
    仕入先cdが空の行は正規化した仕入先名をキーにする。
    """
    agg = {}
    for row in rows:
        d = _col(row, "作成日")
        ym = _ym(d)
        if not ym or ym != (year, month):
            continue
        cd = _col(row, "仕入先cd", "仕入先CD", "仕入先コード").strip()
        name = _col(row, "仕入先名", "仕入先")
        amt = parse_amount(_col(row, "金額"))
        denpyo = _col(row, "発注伝票番号", "伝票番号")
        key = cd or ("name:" + normalize_name(name))
        a = agg.setdefault(key, {
            "仕入先cd": cd, "合算額": 0, "件数": 0,
            "仕入先名": name, "正規名": normalize_name(name), "伝票": [],
        })
        a["合算額"] += amt
        a["件数"] += 1
        if denpyo:
            a["伝票"].append(denpyo)
        if name and not a["仕入先名"]:
            a["仕入先名"] = name
    return agg


def build_master_lookup(master_rows):
    """
    取引先マスタ(payable_master_seed.csv相当の辞書リスト)から、
    会社名突合用の索引を作る。
    戻り値: {
      "by_cd": {NE仕入先cd: master_row},
      "by_norm": {正規化会社名/別名: master_row},
    }
    """
    by_cd, by_norm = {}, {}
    for m in master_rows:
        name = m.get("会社名", "")
        cd = (m.get("NE仕入先cd", "") or "").strip()
        if cd:
            by_cd[cd] = m
        if name:
            by_norm[normalize_name(name)] = m
        for alias in re.split(r"[;,、/／]", m.get("別名", "") or ""):
            alias = alias.strip()
            if alias:
                by_norm[normalize_name(alias)] = m
    return {"by_cd": by_cd, "by_norm": by_norm}


def match_invoice(company, amount, master_lookup, ne_agg, tolerance=0):
    """
    請求書1件を突合する。
    company   : 請求書から読み取った会社名
    amount    : 当月請求額(int)
    返り値 dict: {
      "状態": "一致"/"金額不一致"/"発注なし"/"マスタ未登録",
      "会社名": マスタ会社名 or 入力,
      "NE合算額": int or None, "差額": int or None,
      "NE件数": int, "NE仕入先cd": str, "突合詳細": str,
    }
    """
    norm = normalize_name(company)
    m = master_lookup["by_norm"].get(norm)
    result = {
        "状態": "マスタ未登録", "会社名": company, "NE合算額": None,
        "差額": None, "NE件数": 0, "NE仕入先cd": "", "突合詳細": "",
    }
    if not m:
        result["突合詳細"] = "会社名がマスタに見つかりません(別名登録で解決可)"
        return result
    result["会社名"] = m.get("会社名", company)
    cd = (m.get("NE仕入先cd", "") or "").strip()
    result["NE仕入先cd"] = cd

    # NE合算を引く: 仕入先cd優先、無ければ正規化名で
    ne = None
    if cd and cd in ne_agg:
        ne = ne_agg[cd]
    else:
        target_norm = normalize_name(m.get("会社名", ""))
        for v in ne_agg.values():
            if v.get("正規名") == target_norm or v.get("仕入先cd") == cd:
                ne = v
                break
    if ne is None:
        result["状態"] = "発注なし"
        result["突合詳細"] = "対象月のNE発注が見つかりません"
        return result

    result["NE合算額"] = ne["合算額"]
    result["NE件数"] = ne["件数"]
    diff = int(amount) - int(ne["合算額"])
    result["差額"] = diff
    if abs(diff) <= int(tolerance):
        result["状態"] = "一致"
        result["突合詳細"] = f"NE{ne['件数']}件合算と一致(差{diff:+,}円)"
    else:
        result["状態"] = "金額不一致"
        result["突合詳細"] = f"請求{int(amount):,}円 - NE合算{ne['合算額']:,}円 = {diff:+,}円"
    return result
