# -*- coding: utf-8 -*-
"""
NE API の月の枠の割り振り（正本）。

NE API の無料枠（月1000回・3GB）は、このアプリ（汎用コンバーター）を通して NE API を呼ぶ
**全部のツールで分け合う1つの枠**。ツール単体の見込みではなく、まず全体の合計と空きを見る
（2026-11-22 ユーザー指摘：「単体で考えても意味がない。まず全体が一番」）。

- ROUTINE：毎日・毎週と決まって動くもの。月の見込み回数を書く。合計＋単発の枠が無料枠を超えたら
  tests/test_ne_budget_plan.py が落ちる＝新しいツールを足すときに、設計の時点で止まる。
- 単発（試験・ユーザーからの依頼・期間限定）は、ルーティンの見込みから外して ONEOFF_RESERVE の枠で見る
  （2026-11-22 ユーザー指示）。呼び出し元で oneoff=True を付けるか、ONEOFF に名前を書く。
  手で起動した定期バッチ（GitHub Actions の workflow_dispatch）も単発に数える。
- どちらにも無い呼び出し元は「未登録」として通知に出す（黙ってどちらかに丸めない）。

呼び出し元の名前は lib/ne_api/usage.py が決める（set_caller で付けた名前、無ければ
pages/ ・ batch/ のファイル名）。「土日祝出荷:apply」のように「:」の後ろは処理の種類で、割り振りは前の部分で見る。

数字の根拠（2026-11-22 時点・仮）：内訳の記録はこの日から。それまでは合計しか無く、
11月の土日祝出荷を除いた合計 406回/22日（月 約550回）とコードの作りから割った見込み。
**2026-12 の内訳の実績で見直すこと**（見直したら measured に日付を書く）。
"""

FREE_LIMIT = 1000          # NE料金表：1〜1000回/月・〜3GB は0円（developer.next-engine.com /guides/start/ 2026-11-22 確認）
FREE_BYTES = 3 * 1024 ** 3

# 試験・依頼・期間限定に取っておく枠
ONEOFF_RESERVE = 150

# ルーティン：{名前: {"monthly": 月の見込み回数, "why": 根拠, "measured": 実績で確かめた日 or ""}}
ROUTINE = {
    "auth_keepalive": {"monthly": 60, "measured": "",
                       "why": "認可の延命。毎日2回（.github/workflows/auth-keepalive.yml）×最大1回"},
    "ne_order_rows": {"monthly": 60, "measured": "",
                      "why": "受注明細。毎日1回、差分を1〜2回で取る（ログの実績 1〜2回）"},
    "ne_stock_io": {"monthly": 60, "measured": "",
                    "why": "入出庫の履歴。毎日1回、1〜2回で取る（ログの実績 1〜2回）"},
    "ne_master_sync": {"monthly": 40, "measured": "",
                       "why": "商品マスタ。毎週日曜1回×10回ほど（件数＋ページ送り）"},
    "21_📥_入荷登録": {"monthly": 150, "measured": "",
                     "why": "入荷登録の画面。人が使う回数しだい。11月の合計からの残りを割った仮の値"},
    "20_💰_価格改定": {"monthly": 100, "measured": "",
                     "why": "価格改定の画面。人が使う回数しだい。11月の合計からの残りを割った仮の値"},
    "土日祝出荷": {"monthly": 250, "measured": "",
                 "why": "受注 土日祝出荷（AIworkspace/weekend-shipping）。まとめて送る作り（2026-11-22〜）で"
                        "1日 約25回 × 土日祝 月10日ほど。平日にも広げるなら見直す"},
}

# 単発として扱う呼び出し元（名前、または「土日祝出荷:inspect」のような処理の種類まで）
ONEOFF = {
    "ne_sales_history": "販売実績の取り出し。依頼されたときだけ",
    "ne_placeorder_rows": "発注明細の取り出し。依頼されたときだけ",
    "ne_setgoods_upload": "セット商品の登録。依頼されたときだけ",
    "ne_keepalive": "旧・延命バッチ（2026-08-31 に auth_keepalive へ移行）",
    "土日祝出荷:inspect": "項目の確かめ（試験）",
    "土日祝出荷:fields": "項目の確かめ（試験）",
    "土日祝出荷:probe": "項目の確かめ（試験）",
    "土日祝出荷:confirm_stats": "確認待ちの調査（試験）",
    "土日祝出荷:order_prep": "イレギュラー受注の起票の下準備（依頼）",
    "土日祝出荷:order_upload": "イレギュラー受注の起票（依頼）",
}


def tool_of(caller):
    """「土日祝出荷:apply」→「土日祝出荷」。"""
    return str(caller).split(":", 1)[0]


def classify(caller, oneoff=False):
    """呼び出し元を routine / oneoff / unknown に分ける。"""
    if oneoff:
        return "oneoff"
    c = str(caller)
    if c in ONEOFF or tool_of(c) in ONEOFF:
        return "oneoff"
    if tool_of(c) in ROUTINE:
        return "routine"
    return "unknown"


def routine_total():
    return sum(v["monthly"] for v in ROUTINE.values())


def headroom():
    """無料枠 −（ルーティンの見込み＋単発の枠）。マイナスなら設計で止める。"""
    return FREE_LIMIT - routine_total() - ONEOFF_RESERVE
