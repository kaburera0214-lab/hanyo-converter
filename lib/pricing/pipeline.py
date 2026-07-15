# -*- coding: utf-8 -*-
"""
入力CSV → 計算結果行 のパイプライン（Streamlit非依存）。

ページ（pages/20_価格改定.py）とテストの両方から使う。
列名ゆらぎの解決・NEマスタ突合・ルール適用までを担当し、
UI（表示・手修正・ダウンロード）はページ側が担当する。
"""
import unicodedata

from . import calc, masters, rules


def pick_col(df, *cands):
    """列名のゆらぎを吸収して列を探す（完全一致→部分一致）。無ければNone。"""
    cols = {unicodedata.normalize("NFKC", str(c)).strip(): c for c in df.columns}
    for c in cands:
        if c in cols:
            return cols[c]
    for key, orig in cols.items():
        if any(c in key for c in cands):
            return orig
    return None


def match_input(df, c_code, c_jan, jan_map, code_info):
    """入力CSVの各行を商品コードに解決する。(matched=[(入力行, info)], unmatched=[識別子])"""
    matched, unmatched = [], []
    for _, r in df.iterrows():
        code = masters.norm_key(r[c_code]) if c_code else ""
        if (not code or code == "nan") and c_jan:
            jan = masters.norm_key(r[c_jan])
            code = jan_map.get(jan, "")
            if not code:
                unmatched.append(f"JAN {jan}")
                continue
        if not code or code == "nan":
            unmatched.append("(商品コード・JANとも空)")
            continue
        info = code_info.get(code.lower())
        if info is None:
            unmatched.append(f"商品コード {code}")
            continue
        matched.append((r, info))
    return matched, unmatched


def build_price_rows(matched, c_cost, cost_table, params, mode="normal",
                     c_fixed=None, c_pct=None, c_ship=None, c_size=None,
                     overrides=None, cur_prices=None):
    """
    入力行×NEマスタ → 計算結果の行リスト。
    mode: "normal"=納品価格変更 / "direct"=直送（送料手入力・資材0・込み換算なし）
    overrides: {商品コード: 手修正価格}
    cur_prices: {商品コード(小文字): 楽天から取得した現在販売価格}。
                販売価格は楽天でのみ管理しているため、これが唯一の現販売価格の源
                （未取得の商品は計算不可になる。NE売価での代用はしない）
    """
    overrides = overrides or {}
    cur_prices = cur_prices or {}
    rows = []
    for r, info in matched:
        code = info["商品コード"]
        warn = []
        new_cost = calc.to_number(r[c_cost]) if c_cost else None
        old_cost = calc.to_number(info.get("原価"))
        if new_cost is None:
            warn.append("新下代が空")
        rakuten = cur_prices.get(code.lower())
        cur_price = int(rakuten) if rakuten else 0
        if not cur_price:
            warn.append("現販売価格が未取得 → 📡「楽天から現在価格を取得」を押してください")

        # 項目1（サイズ）→ 送料・資材・配送種別（入力CSVの項目1列があれば上書き）
        size = masters.norm_key(r[c_size]) if (c_size and str(r[c_size]).strip()) else info.get("項目1", "")
        if mode == "direct":
            shipping = calc.to_number(r[c_ship]) if c_ship else None
            material, delivery = 0.0, "宅配便"
            if shipping is None:
                warn.append("新送料が空")
        else:
            ship_mat = cost_table.get(size)
            if ship_mat is None:
                shipping = material = None
                delivery = ""
                warn.append(f"サイズ「{size or '(空)'}」が送料マスタに無い")
            else:
                shipping, material, delivery = ship_mat
                if shipping is None:
                    warn.append(f"サイズ「{size}」の送料が未登録")
                if material is None:
                    material = 0.0
                    warn.append(f"サイズ「{size}」の資材費が未登録（0円で計算）")

        row = {
            "商品コード": code, "商品名": info.get("商品名", ""), "項目1": size,
            "配送種別": delivery, "現販売価格": cur_price,
            "旧下代": old_cost, "新下代": new_cost,
        }
        if new_cost is None or not cur_price or shipping is None:
            row.update({"新販売価格": None, "適用ルール": "計算不可",
                        "新利益額": None, "新利益率": None, "旧利益率": None,
                        "NE売価": None, "警告": "／".join(warn)})
            rows.append(row)
            continue

        base = calc.compute_row(cur_price, new_cost, old_cost, shipping, material,
                                delivery, params, mode=mode)
        ctx = {"現販売価格": cur_price, "利益計算価格": base["利益計算価格"],
               "新下代": new_cost, "旧下代": old_cost,
               "目標利益率価格": base["目標利益率価格"],
               "配送種別": delivery, "mode": mode,
               "指定価格": r[c_fixed] if c_fixed else None,
               "値上げ率": r[c_pct] if c_pct else None}
        new_price, rule_name = rules.decide_price(ctx, params)
        if overrides.get(code):
            new_price, rule_name = int(overrides[code]), "手修正"
        profit, margin = calc.simulate_price(new_price, new_cost, shipping, material,
                                             delivery, params, mode=mode)
        if margin is not None and margin < params["margin_warn"]:
            warn.append(f"利益率{margin:.1%}が警告ライン未満")
        out = calc.output_prices(new_price, new_cost, params)
        row.update({
            "新販売価格": new_price, "適用ルール": rule_name,
            "新利益額": None if profit is None else round(profit),
            "新利益率": margin, "旧利益率": base["旧利益率"],
            "NE売価": out["NE売価"], "警告": "／".join(warn),
        })
        rows.append(row)
    return rows


def size_change_rows(matched, c_size, c_rprice, cost_table, params, cur_prices=None):
    """梱包サイズ変更のチェック行リストを作る。
    現販売価格はCSVの楽天販売価格列＞楽天から取得した価格（cur_prices）の順。NE売価は使わない。"""
    cur_prices = cur_prices or {}
    rows = []
    for r, info in matched:
        code = info["商品コード"]
        old_size = info.get("項目1", "")
        new_size = masters.norm_key(r[c_size])
        cost = calc.to_number(info.get("原価"), 0)
        cur_price = calc.to_number(r[c_rprice]) if c_rprice else None
        if cur_price is None:
            cur_price = cur_prices.get(code.lower(), 0)
        old = cost_table.get(old_size)
        new = cost_table.get(new_size)
        row = {"商品コード": code, "商品名": info.get("商品名", ""),
               "旧項目1": old_size, "新項目1": new_size, "現販売価格": cur_price}
        if new is None or new[0] is None or not cur_price:
            why = (f"新サイズ「{new_size}」が送料マスタに無い" if new is None or new[0] is None
                   else "現販売価格が未取得 → 📡「楽天から現在価格を取得」を押してください")
            row.update({"利益チェック": "-", "配送設定": "-", "新利益率": None, "警告": why})
        else:
            chk = calc.size_change_check(
                cur_price, cost,
                shipping_new=new[0], material_new=(new[1] or 0.0),
                delivery_old=(old[2] if old else "宅配便"), delivery_new=new[2],
                params=params)
            row.update({"利益チェック": chk["利益チェック"],
                        "配送設定": chk["配送設定要修正"],
                        "新利益率": chk["新利益率"], "警告": ""})
        rows.append(row)
    return rows
