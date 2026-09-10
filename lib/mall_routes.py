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

    # 2026-09-10: 手動CSV → API へ変更。
    # updateItems には配送グループの項目が無く、editItem は省略項目をデフォルト値で
    # 上書きするため使えない——ここまでは正しかったが、**uploadItemFile（商品アップロード
    # API）の type=4「項目指定」** を見落としていた。これは画面の
    # 「商品データアップロード → アップロードタイプ＝項目指定」と同じもので、
    # CSVに書いた列だけを更新する。つまり画面でできることはAPIでもできる。
    #
    # CSVの形（2026-09-09に実アップロードで確定）: 半角フィールド名 code, postage-set。
    # 値は配送グループの「No」。見出しを日本語にしたり NT/NM のような名前を書くと
    # U-004-0020「フィールド名に誤りがあるか…」で全件弾かれる。
    #
    # 反映は非同期（uploadItemFileは受付まで／reservePublishで反映予約）。
    # 「送った」を「反映された」とみなさず、getItemで読み直して確認する。
    Route("Yahoo", "delivery_group", "配送グループ",
          API, "uploadItemFile（項目指定）で自動更新し、reservePublishで反映予約します"
               "（反映は非同期なので、getItemで読み直して確認します）。",
          api_symbol="lib.yahoo_api.item_upload:upload_field_specified",
          checked="2026-09-10",
          source="https://developer.yahoo.co.jp/webapi/shopping/uploadItemFile.html"),
]

_BY_KEY = {r.key: r for r in ROUTES}

# 便種 → Yahoo配送グループNo（2026-09-09にユーザー確定。上の Route のコメント参照）。
# 現場に手入力させると打ち間違いがそのまま送料設定になるので、未設定のときだけ
# アプリが自動で保存する（保存済みの値は上書きしない＝画面から変更できる）。
YAHOO_GROUP_NO_DEFAULT = {"宅配便": "1", "メール便": "2"}
# Drive の pricing_settings.json 上のキー名
YAHOO_GROUP_SETTING_KEY = {"宅配便": "yahoo_group_takuhai", "メール便": "yahoo_group_mail"}

# 配送グループNo → 便種（2026-09-09 ストアクリエイターProの配送グループ設定より）。
#   1 デフォルト設定（宅配便NT） / 3 宅配便(YT) / 4 予約用（宅配便・予約商品） / 6 宅配便(TF)
#   2 メール便(NM) / 5 メール便(YM)
# 「いま宅配便のグループにいる商品を、宅配便へ変えようとしている」＝変更不要、の判定に使う。
# 表に無いNoは推測せず「要確認」にする（勝手にキャリアを変えないため）。
YAHOO_GROUP_BINS_DEFAULT = {"1": "宅配便", "2": "メール便", "3": "宅配便",
                            "4": "宅配便", "5": "メール便", "6": "宅配便"}
YAHOO_GROUP_BINS_KEY = "yahoo_group_bins"


def seed_yahoo_group_no(settings):
    """settings に配送グループNoが無ければ確定値を入れる。入れたキーのdictを返す。

    既にある値は絶対に上書きしない（店舗側でグループ番号を組み替えたときに、
    画面で直した値をアプリが勝手に戻してしまうのを防ぐ）。
    """
    seeded = {}
    for bin_name, key in YAHOO_GROUP_SETTING_KEY.items():
        if not str(settings.get(key, "")).strip():
            seeded[key] = YAHOO_GROUP_NO_DEFAULT[bin_name]
    if not settings.get(YAHOO_GROUP_BINS_KEY):
        seeded[YAHOO_GROUP_BINS_KEY] = dict(YAHOO_GROUP_BINS_DEFAULT)
    settings.update(seeded)
    return seeded


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
