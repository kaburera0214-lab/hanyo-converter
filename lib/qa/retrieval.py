# -*- coding: utf-8 -*-
"""
過去事例の類似度ランキング。

AIドラフト生成は蓄積の全件をプロンプトに入れられない（600件超・トークンが持たない）ため
25件に絞る。これまでの絞り方は「同じタグ → 質問日時の新しい順」だったので、
同じ形の質問が連続して立つと直近がそれで埋まり、AIがその文脈でしか答えられなくなっていた。
（2026年8月時点で、発注タグの新規質問に対して選ばれる25件のうち19件が夏季休業の確認だった）

ここでは「質問文が近い順」に並べ替える。方式は文字バイグラムのIDF重みつきコサイン類似度。
日本語は分かち書きが無いので単語分割を必要としない文字バイグラムが素直に効き、
外部APIも追加依存も要らない（600件×数百文字なら1回のランキングで数十ミリ秒）。

    ranked = rank_cases(question, cases, top_n=25)
    for score, case in ranked: ...
"""
import math
import re
from collections import Counter

# 記号は類似判定のノイズになるので空白に落とす（■【】は記入ルール由来で全質問に出る）
_NOISE = re.compile(r"[■●○◇◆★☆【】\[\]（）()「」『』〈〉、。，．・:：;；/／\\｜|＋+\-—―ー~〜…‥!！?？\"'`*#＃>＞<＜=＝%％&＆@＠]+")
_SPACE = re.compile(r"[\s　]+")
# 数字だけ・英数1文字のトークンは効かないので落とす
_DIGITS = re.compile(r"^[0-9０-９]+$")

# 同じタグの事例へのボーナス（加算）。本文の近さを主役にしたいので小さく持つ。
SAME_TAG_BONUS = 0.05


def normalize(text):
    """比較用に正規化する。小文字化・記号の除去・空白の圧縮。"""
    t = (text or "").lower()
    t = _NOISE.sub(" ", t)
    return _SPACE.sub(" ", t).strip()


def bigrams(text):
    """文字バイグラムの出現数。トークン境界をまたぐ組は作らない。"""
    grams = Counter()
    for token in normalize(text).split(" "):
        if not token or _DIGITS.match(token):
            continue
        if len(token) == 1:
            grams[token] += 1
            continue
        for i in range(len(token) - 1):
            grams[token[i:i + 2]] += 1
    return grams


def case_text(case):
    """事例から類似判定に使う文字列を取り出す。

    タイトルは要約なので効きを強くしたく、2回入れて重みを倍にする。
    回答本文は含めない（探したいのは「近い質問」なので、質問文で照合する）。
    """
    title = case.get("タイトル") or case.get("質問タイトル") or ""
    body = case.get("質問本文") or ""
    return f"{title} {title} {body}"


def build_idf(texts):
    """コーパスからIDFを作る。未知のバイグラムに使う既定値も返す。

    戻り値: (idf辞書, 未知語のidf)
    """
    n = len(texts)
    if n == 0:
        return {}, 1.0
    df = Counter()
    for t in texts:
        df.update(set(bigrams(t)))
    idf = {g: math.log(1.0 + n / c) for g, c in df.items()}
    # 1件にしか出ないバイグラムと同じ重み＝珍しい語として扱う
    return idf, math.log(1.0 + n / 1.0)


def _weigh(grams, idf, unknown_idf):
    return {g: c * idf.get(g, unknown_idf) for g, c in grams.items()}


def _cosine(a, b):
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    dot = 0.0
    for g, v in a.items():
        w = b.get(g)
        if w:
            dot += v * w
    if dot == 0.0:
        return 0.0
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def rank_cases(question, cases, top_n=25, same_tag_bonus=SAME_TAG_BONUS):
    """質問に近い順に事例を並べ、上位 top_n を返す。

    question / cases は画面側が組み立てている辞書（タイトル・質問本文・タグ）をそのまま渡せる。
    戻り値は [(スコア, 事例), ...] のスコア降順。
    スコアが同じときは cases の並び順を保つので、呼び出し側が質問日時の降順で
    渡しておけば「同点なら新しい方」になる。
    """
    if not cases:
        return []
    texts = [case_text(c) for c in cases]
    idf, unknown = build_idf(texts)

    q_vec = _weigh(bigrams(case_text(question)), idf, unknown)
    q_tags = set(question.get("タグ") or [])

    scored = []
    for case, text in zip(cases, texts):
        score = _cosine(q_vec, _weigh(bigrams(text), idf, unknown))
        if q_tags and q_tags & set(case.get("タグ") or []):
            score += same_tag_bonus
        scored.append((score, case))

    # sortedは安定なので、同点は元の並び（＝質問日時の降順）が残る
    scored.sort(key=lambda pair: -pair[0])
    return scored[:top_n]
