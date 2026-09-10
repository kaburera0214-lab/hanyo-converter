# -*- coding: utf-8 -*-
"""
モール反映経路の正本（どの項目を、どのモールへ、何で反映しているか）。

**画面の説明文・docstring・READMEに経路を直接書かないこと。** ここを唯一の正本にして、
文言はすべて `sentence()` / `summary_line()` / `checked_note()` から組み立てる。

理由: 経路が変わったとき（例: 手動CSV → API）にコードだけ直り、画面の案内文が
古いまま残る事故が実際に起きているため。文言を別の場所にベタ書きすると、
「実際はAPIで入っているのに、画面はCSVを手でアップしろと言い続ける」状態を
誰も検知できない（間違った文言はテストでも落ちない）。

- `mode` は API / CSV / RMS_UI のいずれか。
- `api_symbol` は API経路の実装（"モジュール:関数"）。実在チェックをテストで行う。
- `forbidden_symbols` は CSV経路が「まだAPIで置き換えられていない」ことの見張り。
  ここに書いた関数が実装された瞬間にテストが落ちるので、経路表と文言を直さないと
  マージできない（＝文言だけ古く残るのを構造的に防ぐ）。
- `checked` は経路をモール仕様に照らして最後に確認した日。画面の管理者パネルに出す。

対応するテスト: tests/test_mall_routes.py
"""

API = "api"          # このツールがAPIで自動反映する
CSV = "csv"          # 人がCSVをモール管理画面へアップして反映する
RMS_UI = "rms_ui"    # 人がモールの管理画面で直接直す


class Route:
    def __init__(self, mall, field, label, mode, how, why="",
                 api_symbol=None, forbidden_symbols=(), checked="", source=""):
        self.mall = mall                  # 表示名（ネクストエンジン / 楽天 / Yahoo）
        self.field = field                # 項目キー
        self.label = label                # 項目の表示名
        self.mode = mode
        self.how = how                    # 「どう反映されるか」の一文
        self.why = why                    # CSV/手動が残っている理由（APIなら空でよい）
        self.api_symbol = api_symbol
        self.forbidden_symbols = tuple(forbidden_symbols)
        self.checked = checked
        self.source = source

    @property
    def key(self):
        return (self.mall, self.field)

    def sentence(self):
        """画面用の一文。"""
        if self.mode == API:
            return f"{self.mall}の{self.label}は{self.how}"
        return f"{self.mall}の{self.label}は{self.how}（{self.why}）"


ROUTES = [
    Route("ネクストエンジン", "location_item1", "ロケーション・項目1（配送サイズ）",
          API, "NE APIで自動更新します。",
          api_symbol="lib.ne_api.goods:upload_goods",
          checked="2026-09-09"),

    Route("ネクストエンジン", "price", "売価",
          API, "NE APIで自動更新します。",
          api_symbol="lib.ne_api.goods:upload_goods",
          checked="2026-09-09"),

    Route("楽天", "delivery_set", "配送方法セット（便種の切替）",
          API, "RMS APIで自動更新します。",
          api_symbol="lib.pricing.rakuten_price:set_shipping_method_group",
          checked="2026-09-09"),

    Route("楽天", "price", "販売価格",
          API, "RMS APIで自動更新します。",
          api_symbol="lib.pricing.rakuten_price:set_price",
          checked="2026-09-09"),

    Route("Yahoo", "price", "販売価格",
          API, "updateItems APIで自動更新し、reservePublishで反映まで行います。",
          api_symbol="lib.yahoo_api.items:update_prices_checked",
          checked="2026-09-09"),

    # ここだけが手動。2026-09-09にYahooの公開仕様で再確認済み。
    #   - updateItems の更新可能項目に postage_set は無い（価格・表示のみ）
    #   - editItem には postage_set があるが「省略した項目はデフォルト値で上書き」される
    #     仕様のため、配送グループだけの部分更新には使えない
    # CSVの形は 2026-09-09 の実アップロードで確定した（それまで未検証だった）。
    # 見出し「配送グループ管理番号」／値 NT・NM は U-004-0020
    # 「フィールド名に誤りがあるか、フィールド名が未設定の項目があります」で弾かれる。
    # 正しくは半角フィールド名 postage-set ＋ 配送グループの「No」（1〜20の数字）。
    # Noは店舗ごとの設定なので、入荷登録の⚙️で保存した値を使う。
    #
    # 2026-09-09にユーザー確定した対応（ストア構築→カート設定→配送グループ設定）:
    #   宅配便 → No.1「デフォルト設定」（配送方法: 宅配便（NT））
    #   メール便 → No.2「メール便（NM）」（配送方法: メール便（NM））
    # キューに入っている内部値 NT/NM は、この画面の配送方法名から取られたもの。
    #
    # ⚠️ 既知の制約: この店舗には宅配便のグループが3つ（No.1 NT / No.3 YT / No.6 TF）、
    # メール便が2つ（No.2 NM / No.5 YM）あり、キャリア・契約が分かれている可能性が高い。
    # いまの実装は便種だけを見て一律にNo.1/No.2を書くので、**YT・TF・YMのグループで
    # 運用している商品がサイズ変更されると、便種と一緒にキャリアも変わる**。
    # 変えたくない場合は、getItemで現在のNoを読んでから同じ系統内で切り替える実装が要る
    # （読む口は lib.yahoo_api.items:get_postage_sets として用意済み）。
    Route("Yahoo", "delivery_group", "配送グループ",
          CSV, "待機キューに貯めて、ストアクリエイターProへ『項目指定』でアップします"
               "（CSVは `code, postage-set` の2列。値は配送グループのNo）。",
          why="updateItemsに配送グループの項目が無く、editItemは省略項目を"
              "デフォルト値で上書きしてしまうため",
          forbidden_symbols=("lib.yahoo_api.items:update_delivery_groups",
                             "lib.yahoo_api.items:update_postage_set"),
          checked="2026-09-09",
          source="https://developer.yahoo.co.jp/webapi/shopping/updateItems.html"),
]

_BY_KEY = {r.key: r for r in ROUTES}


def get(mall, field):
    return _BY_KEY[(mall, field)]


def manual_routes():
    """人の作業が残っている経路（＝忘れると永久に反映されない経路）。"""
    return [r for r in ROUTES if r.mode != API]


def api_routes():
    return [r for r in ROUTES if r.mode == API]


def summary_line():
    """画面上部のキャプション用。API自動のモールと、手動が残る項目を1行で。"""
    malls = sorted({r.mall for r in api_routes()})
    manual = manual_routes()
    text = "・".join(malls) + "は自動更新されます"
    if manual:
        text += "（" + "／".join(f"{r.mall}の{r.label}だけ手動" for r in manual) + "）"
    return text + "。"


def checked_note(mall, field):
    r = get(mall, field)
    note = f"この経路の最終確認: {r.checked}"
    if r.source:
        note += f"（根拠: {r.source}）"
    return note
