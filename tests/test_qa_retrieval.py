# -*- coding: utf-8 -*-
"""
過去事例の類似度ランキング（lib/qa/retrieval）のテスト。

「直近25件」だった選び方を「近い順25件」に変えたのが目的なので、
テストも“同じ形の質問が大量にあっても、関係ない質問はそれに埋もれない”を見る。
実行: hanyo-converter直下で  python tests/test_qa_retrieval.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.qa import retrieval as R  # noqa: E402


def case(title, body, tags):
    return {"タイトル": title, "質問本文": body, "タグ": tags}


# 実データに出てきたパターンを模す：夏季休業の確認がずらりと並んだ蓄積
夏季休業 = [
    case(f"発注・メーカー夏季休業日がW発注（{name}）に及ぼす影響について",
         f"{name}様より夏季休業のご連絡をいただきました。発注予定日と重ならないため影響はないと判断いたします。",
         ["発注"])
    for name in ["カワダ", "セラミック藍", "ワンステップ", "テライ", "丸眞", "ism", "キララ", "セルタン"]
]
その他 = [
    case("発注・アーテック単価相違の対応について",
         "アーテックの単価がメーカー請求とNE登録で相違しています。NE側の原価を修正してよろしいでしょうか。", ["発注"]),
    case("CS・ラッピング可否確認",
         "お客様よりラッピングのご希望がありました。対応可否をご確認ください。", ["CS"]),
    case("受注・ラインギフトエラー",
         "LINEギフトの注文でエラーが出ています。取り込み方法をご教示ください。", ["受注"]),
]
CASES = 夏季休業 + その他


def test_単価相違の質問は夏季休業に埋もれない():
    q = case("発注・カワダ単価相違の対応について",
             "カワダの単価がメーカー請求とNE登録で相違しています。どちらに合わせますか。", ["発注"])
    ranked = R.rank_cases(q, CASES, top_n=3)
    top_titles = [c["タイトル"] for _s, c in ranked]
    assert "発注・アーテック単価相違の対応について" == top_titles[0], top_titles


def test_タグ違いでも本文が近ければ上位に来る():
    q = case("ギフト対応・LINEギフトの取り込み", "LINEギフトの注文がエラーになります。", ["CS"])
    ranked = R.rank_cases(q, CASES, top_n=2)
    assert "受注・ラインギフトエラー" in [c["タイトル"] for _s, c in ranked]


def test_夏季休業の質問には夏季休業の事例が並ぶ():
    q = case("発注・メーカー夏季休業日がD発注（ノアファミリー）に及ぼす影響について",
             "ノアファミリー様の夏季休業について、発注予定日と重ならないため影響はないと判断いたします。", ["発注"])
    ranked = R.rank_cases(q, CASES, top_n=5)
    assert all("夏季休業" in c["タイトル"] for _s, c in ranked), [c["タイトル"] for _s, c in ranked]


def test_件数と順序():
    q = case("発注・単価相違", "単価が相違しています", ["発注"])
    ranked = R.rank_cases(q, CASES, top_n=4)
    assert len(ranked) == 4
    scores = [s for s, _c in ranked]
    assert scores == sorted(scores, reverse=True), scores


def test_同点なら渡した順を保つ():
    """呼び出し側が質問日時の降順で渡す前提。同点は新しい方が残る。"""
    same = [case("まったく無関係A", "xyz", []), case("まったく無関係B", "xyz", [])]
    q = case("関係のない質問", "ぜんぜん違う内容です", [])
    ranked = R.rank_cases(q, same, top_n=2)
    assert [c["タイトル"] for _s, c in ranked] == ["まったく無関係A", "まったく無関係B"]


def test_空の入力():
    q = case("なにか", "なにか", ["発注"])
    assert R.rank_cases(q, []) == []
    assert R.rank_cases({}, CASES, top_n=2) != []   # 質問が空でも落ちない


def test_正規化とバイグラム():
    assert R.normalize("■【発注】　単価相違について！！") == "発注 単価相違について"
    grams = R.bigrams("送料無料")
    assert grams["送料"] == 1 and grams["料無"] == 1 and grams["無料"] == 1
    # 数字だけのトークンは落とす
    assert R.bigrams("12345") == {}


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"OK   {name}")
            except AssertionError as e:
                fails += 1
                print(f"NG   {name}: {e}")
    print("---")
    print("全部通りました" if not fails else f"{fails}件 失敗")
    sys.exit(1 if fails else 0)
