# -*- coding: utf-8 -*-
"""
同じ楽天商品ページで、価格改定の対象にならなかったSKUの洗い出し（取り残しSKU）。

【なぜ必要か】
価格改定は「入力CSVに載った商品コード」のSKUだけを更新する。メーカーの改定リストは
単品で来るので、その単品を組み込んだセットのSKU（同じページのバリエーション）は
CSVに載らず、黙って旧価格のまま残る。

  実例（2026-10-26 #1295）: メーカーの値上げで nowo0017（トーマス本体）の単品SKUだけが
  23,590円に上がり、同じページの「A：アニー」など12SKUは旧価格のまま。セットのほうが
  単品より安く、新しい下代では赤字（A：アニー 21,170円は利益率 −4.8%）で売っていた。

色違い・サイズ違いはメーカーのリストにまとめて載るので、ページの一部だけが改定されるのは
ほぼ「別の商品と組み合わせたSKU」。そこで、ページの一部だけが改定されるときは
残りのSKUを人に判断してもらう（2026-10-26 ユーザー決定）。

【決まり】
- 全体の反映は止めない。1件の引っかかりでメーカー全体を止めた結果、3週間旧価格で
  売り続けた（#1281）。判断が要るのは取り残しSKUだけ
- 判断しなかったSKUは「あとで決める」として控え（lib/pending_retry）に入れる。
  控えは翌日の点検で赤になるので、忘れられない
- 据え置くなら理由を書く（なぜ上げないのかが残らないと、次の改定でまた同じ確認になる）

【提案価格の出し方】
1. NEのセット商品マスタで中身が分かり、中に今回改定した商品が入っている
   → 今の価格 ＋ Σ 数量 ×（改定した商品の新価格 − 旧価格）
   （例: A：アニー 21,170円 ＋ 本体の値上げ幅。加算額の考え方を保ったまま上げる）
2. 中身が分からない・中に改定した商品が無い
   → 今の価格 × 同じページで改定したSKUの値上げ率（複数あれば最も高い率）
"""
from . import calc, masters

# 判断の選択肢（画面の表示と保存の両方で使う正本）
SUGGESTED = "提案価格で反映"
FIXED = "指定価格で反映"
KEEP = "据え置き（理由を書く）"
LATER = "あとで決める"
DECISIONS = [LATER, SUGGESTED, FIXED, KEEP]

BY_SET = "セットの中身から"
BY_RATIO = "同じページの値上げ率から"


def _norm(value):
    return masters.norm_key(value or "").lower()


def _selector_text(variant):
    """SKUの選択肢の表示（「A：アニー(+3190円)」など）。複数軸は「／」でつなぐ。"""
    values = (variant or {}).get("selectorValues") or {}
    if isinstance(values, dict):
        return "／".join(str(v) for v in values.values() if v)
    return ""


def _variant_price(variant, item_price=None):
    for key in ("standardPrice", "price", "salesPrice"):
        p = calc.to_number((variant or {}).get(key))
        if p:
            return int(p)
    return int(item_price) if item_price else None


def leftovers(pages, targets):
    """
    改定の対象SKUを含むページで、対象にならなかったSKUを返す。

    pages:   {商品管理番号: {"variants": {SKU管理番号: variant}, "item_price": 価格}}
             （楽天から現在価格を取ったときに読んだページ。対象外のページが混ざっていてよい）
    targets: {(商品管理番号, SKU管理番号)}（今回の入力で価格が決まるSKU）
    返り値:  [{商品管理番号, SKU管理番号, 連携SKU番号, 選択肢, 現販売価格, 非表示}]
             ページ内の並び（SKU管理番号順）で返す
    """
    want = {(_norm(mn), _norm(sku)) for mn, sku in targets}
    pages_with_target = {mn for mn, _ in want}
    rows = []
    for mn, page in pages.items():
        key = _norm(mn)
        if key not in pages_with_target:
            continue
        variants = (page or {}).get("variants") or {}
        item_price = (page or {}).get("item_price")
        for sku in sorted(variants, key=lambda s: str(s)):
            if (key, _norm(sku)) in want:
                continue
            v = variants[sku] if isinstance(variants[sku], dict) else {}
            renkei = masters.norm_key(v.get("merchantDefinedSkuId") or "")
            rows.append({
                "商品管理番号": key,
                "SKU管理番号": str(sku),
                "連携SKU番号": "" if renkei == "nan" else renkei,
                "選択肢": _selector_text(v),
                "現販売価格": _variant_price(v, item_price),
                "非表示": bool(v.get("hidden")),
            })
    return rows


def page_ratios(revised):
    """
    ページごとの値上げ率（同じページで改定したSKUの 新価格÷旧価格 の最大）。
    revised: {商品コード小文字: {"商品管理番号", "旧価格", "新価格"}}
    """
    ratios = {}
    for r in revised.values():
        old, new = calc.to_number(r.get("旧価格")), calc.to_number(r.get("新価格"))
        if not old or not new:
            continue
        mn = _norm(r.get("商品管理番号"))
        ratios[mn] = max(ratios.get(mn, 0), new / old)
    return ratios


def suggest(rows, sets, revised):
    """
    取り残しSKUに、理由と提案価格を付ける（rows を書き換えずに新しいリストを返す）。

    sets:    {セット商品コード小文字: {"parts": {商品コード小文字: 数量}}}（NEのセット商品マスタ）
    revised: {商品コード小文字: {"商品管理番号", "旧価格", "新価格"}}（今回改定する単品）
    付ける列: 中身の改定 / 提案の根拠 / 提案価格
    """
    ratios = page_ratios(revised)
    out = []
    for row in rows:
        row = dict(row)
        cur = calc.to_number(row.get("現販売価格"))
        parts = (sets.get(_norm(row.get("連携SKU番号"))) or {}).get("parts") or {}
        hits = []
        delta = 0
        for code, qty in parts.items():
            r = revised.get(_norm(code))
            old, new = (calc.to_number(r.get("旧価格")), calc.to_number(r.get("新価格"))) if r else (None, None)
            if old and new:
                qty = int(qty or 1)
                delta += qty * (new - old)
                hits.append(f"{code}×{qty}（{int(old):,}→{int(new):,}円）")
        if hits and cur:
            row["中身の改定"] = "、".join(hits)
            row["提案の根拠"] = BY_SET
            row["提案価格"] = int(cur + delta)
        else:
            ratio = ratios.get(_norm(row.get("商品管理番号")))
            row["中身の改定"] = ("NEのセットに中身の登録なし" if not parts
                              else "中身に今回の改定商品なし")
            row["提案の根拠"] = BY_RATIO if (ratio and cur) else ""
            row["提案価格"] = calc.excel_round(cur * ratio) if (ratio and cur) else None
        out.append(row)
    return out


def decide(rows):
    """
    画面で選んだ判断を、反映するもの・控えるもの・据え置くものに分ける。

    rows: suggest の結果に「判断」「指定価格」「据え置きの理由」を足したもの
    返り値: (apply_rows, later_rows, keep_rows, problems)
      apply_rows: 今回一緒に反映する [{商品管理番号, SKU管理番号, 連携SKU番号, 価格}]
      later_rows: あとで決める（提案価格で控えに入れる）同じ形
      keep_rows:  据え置く行（理由つき）
      problems:   反映を押す前に直してもらうこと（理由の無い据え置き等）
    """
    apply_rows, later_rows, keep_rows, problems = [], [], [], []
    for row in rows:
        decision = row.get("判断") or LATER
        label = f"{row.get('商品管理番号')} {row.get('選択肢') or row.get('SKU管理番号')}"
        base = {k: row.get(k) for k in ("商品管理番号", "SKU管理番号", "連携SKU番号")}
        if decision == KEEP:
            reason = str(row.get("据え置きの理由") or "").strip()
            if not reason or reason.lower() == "nan":
                problems.append(f"{label}: 据え置く理由を書いてください")
            else:
                keep_rows.append({**base, "選択肢": row.get("選択肢"), "理由": reason})
        elif decision == FIXED:
            price = calc.to_number(row.get("指定価格"))
            if not price or price <= 0:
                problems.append(f"{label}: 指定価格を入れてください")
            else:
                apply_rows.append({**base, "価格": int(price)})
        elif decision == SUGGESTED:
            price = calc.to_number(row.get("提案価格"))
            if not price:
                problems.append(f"{label}: 提案価格が出せていません。指定価格で決めてください")
            else:
                apply_rows.append({**base, "価格": int(price)})
        else:
            price = calc.to_number(row.get("提案価格")) or calc.to_number(row.get("現販売価格"))
            if price:
                later_rows.append({**base, "価格": int(price)})
            else:
                problems.append(f"{label}: 価格が分からないため控えに入れられません。指定価格で決めてください")
    return apply_rows, later_rows, keep_rows, problems


def to_rakuten_tasks(rows):
    """[{商品管理番号, SKU管理番号, 連携SKU番号, 価格}] → apply の rakuten_price タスクの形。"""
    groups, order = {}, []
    for r in rows:
        mn = r["商品管理番号"]
        if mn not in groups:
            groups[mn] = {"商品管理番号": mn, "sku_prices": {}, "対象コード": []}
            order.append(mn)
        groups[mn]["sku_prices"][str(r["SKU管理番号"])] = int(r["価格"])
        groups[mn]["対象コード"].append(r.get("連携SKU番号") or str(r["SKU管理番号"]))
    return [groups[mn] for mn in order]


def merge_rakuten_tasks(tasks, extra):
    """apply の rakuten_price タスクに、取り残しSKUのぶんを同じ商品管理番号へまとめて足す。
    同じ商品へのPATCHを1回にする（別々に送ると、あとの1回が前の1回を上書きしないか気にしなくてよい）。"""
    merged = [dict(t, sku_prices=dict(t["sku_prices"]), 対象コード=list(t["対象コード"]))
              for t in (tasks or [])]
    index = {_norm(t["商品管理番号"]): t for t in merged}
    for t in extra or []:
        hit = index.get(_norm(t["商品管理番号"]))
        if hit is None:
            hit = dict(t, sku_prices=dict(t["sku_prices"]), 対象コード=list(t["対象コード"]))
            merged.append(hit)
            index[_norm(t["商品管理番号"])] = hit
            continue
        hit["sku_prices"].update(t["sku_prices"])
        hit["対象コード"].extend(c for c in t["対象コード"] if c not in hit["対象コード"])
    return merged
