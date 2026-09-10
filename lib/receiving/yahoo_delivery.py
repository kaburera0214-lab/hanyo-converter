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

    current   … lib.yahoo_api.items.get_postage_sets の返り値
    group_no  … {便種: 書き込むNo}
    group_bins… {No: 便種}（この店舗の配送グループ設定）
    """
    from lib.yahoo_api import items as yitems

    plan = Plan()
    for row in rows:
        code, bin_name = str(row["code"]).strip(), row["便種"]
        info = (current or {}).get(code) or {"state": yitems.STATE_ERROR,
                                             "value": None, "message": "現在値を取得していません"}
        target_no = str((group_no or {}).get(bin_name, "")).strip()

        if info["state"] == yitems.STATE_NOT_FOUND:
            plan.not_found.append({"code": code, "便種": bin_name})
            continue
        if info["state"] != yitems.STATE_OK:
            plan.unknown.append({"code": code, "便種": bin_name, "現在No": "",
                                 "理由": info.get("message") or "現在値を読めませんでした"})
            continue
        if not target_no:
            plan.unknown.append({"code": code, "便種": bin_name, "現在No": info["value"],
                                 "理由": f"{bin_name}の配送グループNoが未設定です"})
            continue

        cur_no = str(info["value"] or "").strip()
        if not cur_no:
            # 現在グループ未設定なら、目的の便種のグループを入れる（奪うキャリアが無い）
            plan.to_update.append({"code": code, "便種": bin_name,
                                   "no": target_no, "現在No": ""})
            continue
        cur_bin = (group_bins or {}).get(cur_no)
        if cur_bin is None:
            plan.unknown.append({"code": code, "便種": bin_name, "現在No": cur_no,
                                 "理由": f"配送グループNo {cur_no} がどの便種か分かりません。"
                                         "配送グループ設定を確認してください"})
        elif cur_bin == bin_name:
            plan.already.append({"code": code, "便種": bin_name, "現在No": cur_no})
        else:
            plan.to_update.append({"code": code, "便種": bin_name,
                                   "no": target_no, "現在No": cur_no})
    return plan


def upload_csv(to_update):
    """更新対象 → Yahoo項目指定アップロード用CSV（code, postage-set）のbytes。"""
    from lib.pricing import export as ex

    rows = [{"商品管理番号": r["code"], "新便種": r["便種"]} for r in to_update]
    group_no = {r["便種"]: r["no"] for r in to_update}
    return ex.yahoo_delivery_csv(rows, group_no)
