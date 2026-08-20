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


def drop_blank_rows(df):
    """全セルが空の行を落とす。返り値: (落とした後のdf, 落とした行数)

    Excelで作ったCSVは末尾に「,」だけの行が残ることがある（2026-08-20の実障害）。
    これはデータではないので突合の対象にしない。放置すると「NE商品マスタに存在しない行が
    1件あります」と警告に出てしまい、マスタが古いのかと調べる手間が発生する。

    ※「一部だけ空」の行は落とさない。JANが空で下代だけあるような行は入力ミスなので、
      未マッチとして見せる必要がある（黙って捨てると気づけない）。
    """
    if df is None or len(df) == 0:
        return df, 0
    blank = df.apply(
        lambda row: all(str(v).strip() in ("", "nan", "None", "NaT") for v in row), axis=1)
    return df[~blank].reset_index(drop=True), int(blank.sum())


def match_input(df, c_code, c_jan, jan_map, code_info):
    """入力CSVの各行を商品コードに解決する。(matched=[(入力行, info)], unmatched=[識別子])
    JAN列に商品コードが入っていても救済する（JAN→ダメなら商品コードとして照合）。
    ※空行は drop_blank_rows で先に落としておくこと（ここでは未マッチ扱いになる）。"""
    matched, unmatched = [], []
    for _, r in df.iterrows():
        code = masters.norm_key(r[c_code]) if c_code else ""
        if (not code or code == "nan") and c_jan:
            jan = masters.norm_key(r[c_jan])
            code = jan_map.get(jan, "")
            if not code and jan.lower() in code_info:
                code = code_info[jan.lower()]["商品コード"]  # JAN列の値が商品コードだった場合
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
    """
    梱包サイズ変更のチェック＋必要な対応の判定（2026-07-17ユーザー確定フロー）:
      1. サイズアップかダウンか（新旧サイズの送料+資材の比較で判定）
      2. 便種変更（メール便⇔宅配便）があれば「モール配送設定の修正」対象
      3. サイズアップは利益チェック（警告ライン基準・新サイズのコストと新便種で判定）
      4. 利益NGなら納品価格変更と同じ計算で販売価格を再設定（新販売価格・NE売価を埋める）
    現販売価格はCSVの楽天販売価格列＞楽天から取得した価格（cur_prices）の順。
    """
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

        row = {"商品コード": code, "商品名": info.get("商品名", ""), "警告": "",
               "旧項目1": old_size, "新項目1": new_size, "区分": "",
               "現販売価格": cur_price,
               "旧便種": old[2] if old else "", "新便種": new[2] if new else "",
               "配送設定": "", "利益チェック": "", "新利益率": None,
               "新販売価格": None, "NE売価": None}
        warn = []
        if new is None or new[0] is None:
            warn.append(f"新サイズ「{new_size}」が送料マスタに無い")
        if old is None or old[0] is None:
            warn.append(f"旧サイズ「{old_size or '(空)'}」が送料マスタに無い（区分を判定できない）")

        if not warn:
            old_total = old[0] + (old[1] or 0)
            new_total = new[0] + (new[1] or 0)
            row["区分"] = ("サイズアップ" if new_total > old_total
                           else "サイズダウン" if new_total < old_total else "同等")
            row["配送設定"] = (f"要修正（{old[2]}→{new[2]}）" if old[2] != new[2] else "不要")

            if row["区分"] == "サイズアップ":
                if not cur_price:
                    row["利益チェック"] = "-"
                    warn.append("現販売価格が未取得 → 📡「楽天から現在価格を取得」を押してください")
                else:
                    # 新サイズのコスト・新便種（修正後の状態）で現価格の利益率を判定
                    _, margin = calc.simulate_price(cur_price, cost, new[0], new[1] or 0.0,
                                                    new[2], params)
                    ok = margin is not None and margin >= params["margin_warn"]
                    row["利益チェック"] = "〇" if ok else "×"
                    row["新利益率"] = margin
                    if not ok:
                        # 価格の決定は価格改定(rules.decide_price)に一元化する。
                        # サイズ変更は下代変更が無いので「値上げ率価格=現価格」の床は使わず
                        # （旧下代/新下代を渡さない）、目標利益率価格に着地させる。
                        new_price, _rule = rules.decide_price({
                            "現販売価格": cur_price,
                            "目標利益率価格": calc.target_price(cost, new[0], new[1] or 0.0,
                                                              new[2], params),
                            "配送種別": new[2], "mode": "normal",
                        }, params)
                        row["新販売価格"] = new_price
                        row["NE売価"] = calc.excel_round(new_price / (1 + params["tax_rate"]))
                        _, row["新利益率"] = calc.simulate_price(new_price, cost, new[0],
                                                                 new[1] or 0.0, new[2], params)
            else:
                row["利益チェック"] = "-"

        row["警告"] = "／".join(warn)
        rows.append(row)
    return rows
