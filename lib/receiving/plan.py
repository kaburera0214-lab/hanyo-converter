# -*- coding: utf-8 -*-
"""
入荷登録の判定ロジック（Streamlit非依存・テスト対象）。

入力フォームの行（商品コード・資材ナンバー・ロケーション・配送サイズ）から、
  1. NE商品マスタに書く値（ロケーションコード = 資材ナンバー-ロケーション、項目1 = 配送サイズ）
  2. 項目1が変わる場合のサイズ変更判定（lib/pricing/pipeline.size_change_rows を流用）
     … サイズアップ/ダウン・便種変更（メール便⇔宅配便）・利益チェック・NG時の価格再設定
を組み立てる。実行（API呼び出し）は runner.py、画面は pages/21。
"""
import pandas as pd

from lib.pricing import export as ex, masters, pipeline


def location_code(material_no, location):
    """資材ナンバー＋ロケーション → NEのロケーションコード（例: 100A-TA10B）。"""
    return f"{str(material_no).strip()}-{str(location).strip()}"


def split_location_values(values):
    """NEマスタ「ロケーションコード」列の既存値から、プルダウン選択肢を抽出する。
    最初の「-」で分割: 100A-TA10B → 資材ナンバー=100A / ロケーション=TA10B。
    返り値: (資材ナンバー一覧, ロケーション一覧)（それぞれソート済み・重複なし）"""
    mats, locs = set(), set()
    for v in values:
        s = masters.norm_key(v)
        if not s or s == "nan" or "-" not in s:
            continue
        m, _, loc = s.partition("-")
        if m:
            mats.add(m)
        if loc:
            locs.add(loc)
    return sorted(mats), sorted(locs)


def _empty(size):
    s = masters.norm_key(size)
    return "" if s == "nan" else s


def build_plan(rows, code_info, cost_table, params, cur_prices=None):
    """
    入力行 → 実行プラン行のリスト。
    rows: [{商品コード, 資材ナンバー, ロケーション, 配送サイズ}]
    返り値の各行: 商品コード/商品名/資材ナンバー/ロケーション/ロケーションコード/
      旧項目1/新項目1/区分/旧便種/新便種/配送設定/利益チェック/新利益率/
      現販売価格/新販売価格/NE売価/警告
    区分: 初回登録（旧項目1が空）／変更なし（同値）／サイズアップ／サイズダウン／同等
    """
    cur_prices = cur_prices or {}
    out = []
    for r in rows:
        code = masters.norm_key(r["商品コード"])
        info = code_info.get(code.lower()) or {"商品コード": code, "商品名": "",
                                               "原価": "", "項目1": ""}
        old_size = _empty(info.get("項目1", ""))
        new_size = _empty(r["配送サイズ"])
        row = {
            "商品コード": info["商品コード"], "商品名": info.get("商品名", ""),
            "資材ナンバー": str(r["資材ナンバー"]).strip(),
            "ロケーション": str(r["ロケーション"]).strip(),
            "ロケーションコード": location_code(r["資材ナンバー"], r["ロケーション"]),
            "旧項目1": old_size, "新項目1": new_size, "区分": "",
            "旧便種": "", "新便種": "", "配送設定": "不要",
            "利益チェック": "-", "新利益率": None,
            "現販売価格": None, "新販売価格": None, "NE売価": None, "警告": "",
        }
        if not old_size:
            # 初回登録: 項目1が未設定 → 設定するだけ（サイズ変更判定・モール修正は無し）
            row["区分"] = "初回登録"
        elif new_size == old_size:
            row["区分"] = "変更なし"
        else:
            sc = pipeline.size_change_rows([({"新項目1": new_size}, info)], "新項目1",
                                           None, cost_table, params,
                                           cur_prices=cur_prices)[0]
            for k in ("区分", "旧便種", "新便種", "配送設定", "利益チェック",
                      "新利益率", "現販売価格", "新販売価格", "NE売価", "警告"):
                row[k] = sc[k]
        out.append(row)
    return out


def ne_rows_from_plan(plan):
    """プラン → NE商品マスタ更新の2バッチ（英語フィールド名・空値なし）。
    返り値: (①ロケーション＋項目1の行list, ②価格再設定の売価行list)
    ①は全行（項目1は「変更なし」でも現値と同値を送る＝列を混在させない・空値を送らない）。
    ②は利益NGで販売価格を再設定した行のみ（原価は不変のため genka_tnk は送らない）。"""
    main_rows, price_rows = [], []
    for r in plan:
        main_rows.append({"syohin_code": r["商品コード"],
                          "location": r["ロケーションコード"],
                          "org1": r["新項目1"]})
        if r.get("NE売価"):
            price_rows.append({"syohin_code": r["商品コード"],
                               "baika_tnk": int(r["NE売価"])})
    return main_rows, price_rows


def delivery_rows(plan, sku_table):
    """便種変更（配送設定=要修正）の商品を親（商品管理番号）単位にまとめる。
    pages/20の梱包サイズ変更タブと同じ規則: SKU対応表→枝番除去→コード自身。"""
    dv_rows, seen = [], set()
    for r in plan:
        if not str(r.get("配送設定", "")).startswith("要修正"):
            continue
        code = str(r["商品コード"]).lower()
        hit = sku_table.get(code)
        parent = (hit[0] if hit else masters.parent_code(code))
        if parent in seen:
            continue
        seen.add(parent)
        dv_rows.append({"商品管理番号": parent, "商品コード": r["商品コード"],
                        "旧便種": r["旧便種"], "新便種": r["新便種"]})
    return dv_rows


def price_tasks(plan, code_info, sku_table):
    """価格再設定行 → 楽天PATCH用に親（商品管理番号）単位へまとめる。
    返り値: (tasks=[{商品管理番号, sku_prices{SKU:価格}, 対象コード}], missing=[SKU不明コード])"""
    groups, order, missing = {}, [], []
    for r in plan:
        if not r.get("新販売価格"):
            continue
        code = masters.norm_key(r["商品コード"])
        hit = sku_table.get(code.lower())
        if hit:
            parent, sku_no = hit[0], hit[1]
        elif code == masters.parent_code(code):
            parent, sku_no = code, code   # 枝番なし＝単品SKU（SKU番号=商品コード）
        else:
            missing.append(code)
            continue
        key = parent.lower()
        if key not in groups:
            groups[key] = {"商品管理番号": parent, "sku_prices": {}, "対象コード": []}
            order.append(key)
        groups[key]["sku_prices"][sku_no] = int(r["新販売価格"])
        groups[key]["対象コード"].append(code)
    return [groups[k] for k in order], missing


def evidence_files(plan, dv_rows, code_info, sku_table):
    """証跡CSV一式 {ファイル名: bytes}（Drive「価格改定履歴」へ保存する用）。
    形式は価格改定と同じ export.py を流用し、手動アップにも使える内容にする。"""
    from lib.ne_api import goods

    files = {}
    main_rows, ne_price_rows = ne_rows_from_plan(plan)
    # NE APIに送るCSVそのもの（部分更新・英語フィールド名）
    files["ne_location_update.csv"] = goods.build_csv(main_rows).encode("cp932", errors="replace")
    if dv_rows:
        files["rakuten_delivery_update.csv"] = ex.rakuten_delivery_csv(dv_rows)
        files["yahoo_delivery_update.csv"] = ex.yahoo_delivery_csv(dv_rows)
    ng = [r for r in plan if r.get("新販売価格")]
    if ng:
        mall = [{"商品コード": r["商品コード"], "楽天販売価格": r["新販売価格"],
                 "Yahoo販売価格": r["新販売価格"]} for r in ng]
        rak_records, _ = ex.rakuten_rows(mall, sku_table)
        yah_records, _ = ex.yahoo_rows(mall, sku_table)
        files["normal-item.csv"] = ex.rakuten_csv(rak_records)
        files["yahoo_data.csv"] = ex.yahoo_csv(yah_records)
        files["ne_price_update.csv"] = ex.ne_csv(
            [{"商品コード": r["商品コード"], "NE売価": r["NE売価"],
              "NE原価": code_info.get(str(r["商品コード"]).lower(), {}).get("原価", "")}
             for r in ng])
    files["receiving_detail.csv"] = ex.detail_csv(pd.DataFrame(plan))
    return files
