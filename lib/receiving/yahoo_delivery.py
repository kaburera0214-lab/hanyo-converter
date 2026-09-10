# -*- coding: utf-8 -*-
"""
Yahoo配送グループの反映判定（「本当に変える必要があるものだけ」を選ぶ）。

入荷登録が決めるのは**便種**（メール便⇔宅配便）だけ。どの配送グループ（＝キャリア）で
売るかは人が決めている。この店舗には宅配便のグループが複数あるので、便種だけを見て
一律に既定のグループを書き込むと、**便種は合っているのにキャリアだけ変わる**。

実例（2026-09-09）: kira0001 は Yahoo側が No.3「宅配便(YT)」だった。便種はすでに
宅配便なので変更不要なのに、既定の No.1「宅配便(NT)」を書き込むところだった。

そこで、送る前に getItem で現在のグループNoを読み、
  - 現在のグループが**すでに目的の便種**   → 対象外（人の設定を尊重してキャリアを保つ）
  - Yahooに商品が無い                      → 「未登録」。反映は不可能なので待たせず報告する
  - グループNoの便種が分からない            → 「要確認」。推測で書き換えない
  - 現在の便種が目的と違う                  → これだけを更新対象にする
と振り分ける。
"""


class Plan:
    """振り分け結果。それぞれ [{code, 便種, ...}] のlist。"""

    def __init__(self):
        self.to_update = []      # 更新する（{code, 便種, no, 現在No}）
        self.already = []        # すでに目的の便種（{code, 便種, 現在No}）
        self.not_found = []      # Yahoo未登録（{code, 便種}）
        self.unknown = []        # 判定できない（{code, 便種, 現在No, 理由}）

    def __len__(self):
        return len(self.to_update)


def classify(rows, current, group_no, group_bins):
    """rows=[{code, 便種}] を振り分ける。

    current   … lib.yahoo_api.items.get_item_status の返り値
    group_no  … {便種: 書き込むNo}
    group_bins… {No: 便種}（この店舗の配送グループ設定）
    """
    from lib.yahoo_api import items as yitems

    plan = Plan()
    for row in rows:
        code, bin_name = str(row["code"]).strip(), row["便種"]
        info = (current or {}).get(code) or {
            "state": yitems.STATE_ERROR, "postage_set": None,
            "product_category": None, "message": "現在値を取得していません"}
        target_no = str((group_no or {}).get(bin_name, "")).strip()

        if info["state"] == yitems.STATE_NOT_FOUND:
            plan.not_found.append({"code": code, "便種": bin_name})
            continue
        if info["state"] != yitems.STATE_OK:
            plan.unknown.append({"code": code, "便種": bin_name, "現在No": "",
                                 "理由": info.get("message") or "現在値を読めませんでした"})
            continue
        if not target_no:
            plan.unknown.append({"code": code, "便種": bin_name,
                                 "現在No": info["postage_set"],
                                 "理由": f"{bin_name}の配送グループNoが未設定です"})
            continue

        cur_no = str(info["postage_set"] or "").strip()
        cur_cat = str(info.get("product_category") or "").strip()
        if not cur_no:
            # 現在グループ未設定なら、目的の便種のグループを入れる（奪うキャリアが無い）
            plan.to_update.append({"code": code, "便種": bin_name, "no": target_no,
                                   "現在No": "", "カテゴリ": cur_cat})
            continue
        cur_bin = (group_bins or {}).get(cur_no)
        if cur_bin is None:
            plan.unknown.append({"code": code, "便種": bin_name, "現在No": cur_no,
                                 "理由": f"配送グループNo {cur_no} がどの便種か分かりません。"
                                         "配送グループ設定を確認してください"})
        elif cur_bin == bin_name:
            plan.already.append({"code": code, "便種": bin_name, "現在No": cur_no})
        else:
            plan.to_update.append({"code": code, "便種": bin_name, "no": target_no,
                                   "現在No": cur_no, "カテゴリ": cur_cat})
    return plan


def has_category(value):
    """プロダクトカテゴリが設定済みか。

    **Yahooは未設定を "0" で返す。**空文字だけを未設定とみなすと、0の商品を
    「設定済み」と判断して補完が起動しない（2026-09-10 artc4168 で発生）。
    価格改定側（lib/yahoo_api/items.py）と同じく数値で判定すること。
    """
    try:
        return int(str(value).strip() or 0) > 0
    except ValueError:
        return False


def needs_category(to_update, force=False):
    """プロダクトカテゴリが未設定で、このままでは弾かれる商品コード。

    カテゴリが無い商品は、配送グループだけのCSVを送っても
    U-001-0363「プロダクトカテゴリが存在しません」で行ごと落ちる
    （2026-09-09 artc4168 で確認）。

    force=True は「値は入っているがYahoo側に存在しないカテゴリID」の救済用。
    getItemからは有効性が分からないので、人が選んだときだけ既存値を無視する。
    """
    if force:
        return [r["code"] for r in to_update]
    return [r["code"] for r in to_update if not has_category(r.get("カテゴリ"))]


def upload_batches(to_update, categories=None):
    """更新対象 → アップロードするCSVの束 [{csv, codes, with_category}]。

    カテゴリを補う行と補わない行は**別のCSVに分ける**。項目指定アップロードは
    空欄を送ると値が消えるので、1枚に混ぜて片方だけ空にすることはできない。
    カテゴリが必要なのに推定できなかった行は、ここには含めない（呼び出し側が報告する）。
    """
    from lib.pricing import export as ex

    categories = {str(k).lower(): str(v).strip()
                  for k, v in (categories or {}).items() if str(v).strip()}
    plain, with_cat = [], []
    for r in to_update:
        if str(r.get("カテゴリ") or "").strip():
            plain.append(r)
        elif r["code"] in categories:
            with_cat.append(r)
        # それ以外（カテゴリ未設定かつ推定できず）は送らない

    batches = []
    for rows, cats in ((plain, None), (with_cat, categories)):
        if not rows:
            continue
        csv_rows = [{"商品管理番号": r["code"], "新便種": r["便種"]} for r in rows]
        group_no = {r["便種"]: r["no"] for r in rows}
        batches.append({"csv": ex.yahoo_delivery_csv(csv_rows, group_no, cats),
                        "codes": [r["code"] for r in rows],
                        "with_category": cats is not None})
    return batches


def upload_csv(to_update, categories=None):
    """互換用: 1枚にまとまる場合のCSV（テスト・手動リカバリー用）。"""
    batches = upload_batches(to_update, categories)
    return batches[0]["csv"] if batches else b""
