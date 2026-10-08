"""TeamEC新体系の副作用のない計算エンジン。

単価は料金の版（VERSIONS）を正本にする。料金表の取得版（source_rates.json）は根拠として残し、
版と取得版が食い違ったらテストで落とす。説明文から単価を正規表現で拾わない。
元データの未取得と実績ゼロはUIで別々に扱う。
"""
from __future__ import annotations

import calendar
import json
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import jpholiday

from . import APPLIES_FROM, CLIENT as CLIENT_NAME

SOURCE = json.loads(Path(__file__).with_name("source_rates.json").read_text(encoding="utf-8"))
ROWS = {row[1]: row for row in SOURCE["values"] if len(row) >= 4}
MANAGEMENT_RATE = Decimal("0.05")
TAX_RATE = Decimal("0.1")
STOCK_RESTORE = "在庫調整（在庫戻し／一時在庫へ移動）"

# 料金の版。改定時は既存の版を書き換えず、新しい版を末尾に追記する（請求済みの根拠を残す）。
# 料金表には「2026年9〜11月は暫定運用、12月に初回検証のうえ単価確定」とある。
VERSIONS = [{
    "id": "TE-2026-09",
    "applies_from": (2026, 9),
    "source": f"新料金単価表 {SOURCE['checked_at']}取得版＋2026-10-02本人回答",
    # 料金表の単価。複数あるものは (基本, 割引/加算...) の順。
    "prices": {
        "日次締め処理": (700,),
        "日次運用費": (1150,),
        "入庫：パレット納品": (500,),
        "入庫：ケース納品": (80,),
        "入庫：ピース納品": (40,),
        "配送種別・個口数の変更": (650,),
        "入庫差異の報告・照会": (550,),
        "新商品 初期設定": (700, 450, 350),
        STOCK_RESTORE: (400,),
        "追加便対応料（3便目以降）": (2000, 5500),
        "車両受入費": (4000, 3000),
        "FBA対応費": (2000,),
        "随時連絡対応（起票・調査）": (1200,),
        "返品処理": (950,),
        "汎用作業料": (2100,),
    },
    # 保管カウントページの種別名 → (料金表の項目, 月額単価)
    "storage": {
        "保管料：パレット": ("保管料（パレット）", 1000),
        "保管料：中量棚": ("保管料（中量棚）", 300),
        "保管料：当社指定ロケーション": ("保管料（当社指定ロケーション）", 600),
    },
    # 料金表との明示的な差分（本人回答）。テストで料金表との差を許す対象はここだけ。
    "overrides": {
        "出荷指示作成料": {"price": 1200, "weekday": 2, "holiday": 1,
                           "source": "2026-10-02 本人回答：1,200円/回。出荷した稼働日のみ、平日2回・土日祝1回で固定"},
    },
    # 料金表にない月限定の項目。管理費の対象外。
    "extras": [
        {"品名": "協力費", "金額": 20000, "months": ["2026-09"], "note": "2026年9月分",   # note＝請求書に載る文
         "source": "2026-10-02 本人回答：9月までの協力費は合意済みのため請求"},
    ],
    # 丸め・控除（2026-10-02 本人回答）
    "management_rounding": "四捨五入",
    "support": "最後に控除（管理費の計算対象は減らさない）",
}]


def version(year: int, month: int) -> dict:
    ym = (int(year), int(month))
    if ym < APPLIES_FROM:
        raise ValueError("新体系は2026年9月作業分からです。8月以前は旧体系で計算します")
    return [v for v in VERSIONS if v["applies_from"] <= ym][-1]


def price(name: str, index=0, ver=None) -> Decimal:
    return Decimal((ver or VERSIONS[-1])["prices"][name][index])


def dispatch(ver=None) -> dict:
    return (ver or VERSIONS[-1])["overrides"]["出荷指示作成料"]


def amount(value, name="値", *, integer=False) -> Decimal:
    """空欄・非数・負数を0にしない。UIの明示的な0だけを受理する。"""
    try:
        n = Decimal(str(value).strip().replace(",", ""))
    except Exception as exc:
        raise ValueError(f"{name}は数値を入力してください") from exc
    if not n.is_finite() or n < 0 or (integer and n != n.to_integral_value()):
        raise ValueError(f"{name}は0以上の{'整数' if integer else '数値'}を入力してください")
    return n


def yen(value) -> int:
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def calendar_rows(year: int, month: int) -> list[dict]:
    d_ = dispatch(version(year, month))
    result = []
    for day in range(1, calendar.monthrange(year, month)[1] + 1):
        d = date(year, month, day)
        holiday = jpholiday.is_holiday_name(d)
        off = bool(holiday or d.weekday() >= 5)
        result.append({"日付": d.isoformat(), "曜日": "月火水木金土日"[d.weekday()],
                       "祝日": holiday or "", "稼働": not off,
                       "定額回数": d_["holiday" if off else "weekday"]})
    return result


def line(name, unit_price, quantity, category, unit="件", detail=""):
    p = amount(unit_price, name + "単価")
    q = amount(quantity, name + "数量")
    return {"区分": category, "品名": name, "単価": p, "数量": q,
            "単位": unit, "金額": yen(p * q), "詳細": detail}


def daily_lines(year, month, active_days):
    """出荷した稼働日だけを対象に、日次の固定費を計上する。"""
    ver = version(year, month)
    all_days = {r["日付"]: r for r in calendar_rows(year, month)}
    selected = list(active_days)
    if len(set(selected)) != len(selected) or any(d not in all_days for d in selected):
        raise ValueError("稼働日は対象月の重複しない日付にしてください")
    batches = sum(all_days[d]["定額回数"] for d in selected)
    detail = "出荷稼働日：" + "、".join(selected)
    return [line("出荷指示作成料", dispatch(ver)["price"], batches, "第1層", "回", detail),
            line("日次締め処理", price("日次締め処理", ver=ver), len(selected), "第1層", "日", detail),
            line("日次運用費", price("日次運用費", ver=ver), len(selected), "第2層", "日", detail)]


def storage_lines(count_rows, year, month):
    """保管カウントページの明細（期・種別・数量）を、版の単価で2期平均×単価にする。

    版にない種別は0円にせずエラー。期が片方しか無い種別も平均が半分になるため止める。
    """
    ver = version(year, month)
    agg = {}
    for r in count_rows:
        name = str(r.get("種別", "")).strip()
        if not name:
            continue
        if name not in ver["storage"]:
            raise ValueError(f"保管種別「{name}」は新体系の料金にありません")
        period = str(r.get("期", "")).strip()
        if period not in ("第1期", "第2期"):
            raise ValueError(f"保管種別「{name}」の期が不明です")
        a = agg.setdefault(name, {"第1期": Decimal(0), "第2期": Decimal(0)})
        a[period] += amount(r.get("数量", ""), name + "数量")
    periods = {p for r in count_rows if str(r.get("種別", "")).strip() for p in [str(r.get("期", "")).strip()]}
    missing = {"第1期", "第2期"} - periods if agg else set()
    if missing:
        raise ValueError(f"保管カウントの{'・'.join(sorted(missing))}が未入力です（平均が半分になるため計算しません）")
    result = []
    for name, (item, unit_price) in ver["storage"].items():
        if name not in agg:
            continue
        a = agg[name]
        result.append(line(item, unit_price, (a["第1期"] + a["第2期"]) / 2, "保管",
                           "棚" if "中量棚" in name else "PL",
                           f"第1期 {a['第1期']}・第2期 {a['第2期']} の平均（保管カウントページ）"))
    return result


def labor_lines(irregular_rows, year, month):
    """イレギュラー作業ページの記録を汎用作業料にする。15分単位で入力される前提を検証する。"""
    ver = version(year, month)
    hours = Decimal(0)
    for r in irregular_rows:
        label = f"{r.get('日付', '')} {r.get('作業項目', '')}".strip()
        h = amount(r.get("時間数", ""), label + "の時間数")
        people = amount(r.get("人数", ""), label + "の人数", integer=True)
        if h * 4 != (h * 4).to_integral_value():
            raise ValueError(f"{label}：時間数が15分単位ではありません（{h}h）。イレギュラー作業ページで直してください")
        if h and people < 1:
            raise ValueError(f"{label}：人数が0です")
        hours += h * people
    if not hours:
        return []
    return [line("汎用作業料", price("汎用作業料", ver=ver), hours, "第3層", "人時",
                 f"イレギュラー作業ページ {len(irregular_rows)}件の合計人時")]


# 依頼ごとの実績。日次・保管・汎用作業・実費をここに混ぜない。
EVENT_NAMES = ("入庫：パレット納品", "入庫：ケース納品", "入庫：ピース納品", "配送種別・個口数の変更",
               "入庫差異の報告・照会", "新商品 初期設定", STOCK_RESTORE, "追加便対応料（3便目以降）",
               "車両受入費", "FBA対応費", "随時連絡対応（起票・調査）", "返品処理")
LAYER = {name: ROWS[name][0] for name in EVENT_NAMES}


def event_unit(name):
    condition = ROWS[name][3]
    for term, unit in (("1パレット", "PL"), ("1箱", "箱"), ("1点", "pcs"),
                       ("1SKU", "SKU"), ("1台", "台"), ("1依頼", "依頼"), ("1回", "回"), ("1件", "件")):
        if condition.startswith(term):
            return unit
    raise ValueError(f"{name}：料金表の課金単位を確認してください")


def event_lines(rows, year: int, month: int):
    ver = version(year, month)
    result, seen = [], set()
    for row in rows:
        name = str(row.get("作業", "")).strip()
        request = str(row.get("依頼ID", "")).strip()
        if not name and not request:
            if any(str(v).strip() not in ("", "0", "0.0", "False", "None", "nan")
                   for k, v in row.items() if k not in ("作業", "依頼ID")):
                raise ValueError("作業が未選択の行に入力があります")
            continue
        if name not in EVENT_NAMES or not request:
            raise ValueError("各作業に作業種別と依頼IDを入力してください")
        try:
            performed = date.fromisoformat(str(row["実施日"])[:10])
        except Exception as exc:
            raise ValueError(f"{request}：実施日を入力してください") from exc
        if (performed.year, performed.month) != (year, month):
            raise ValueError(f"{request}：実施日が対象月外です")
        identity = (request, name)
        if identity in seen:
            raise ValueError(f"{request}：同じ依頼・作業が重複しています。数量を1行にまとめてください")
        if (name == "返品処理" and (request, STOCK_RESTORE) in seen or
                name == STOCK_RESTORE and (request, "返品処理") in seen):
            raise ValueError(f"{request}：返品由来の在庫戻しは返品処理に含まれます。重複計上を確認してください")
        seen.add(identity)
        q = amount(row.get("数量", ""), request + "数量", integer=True)
        p = price(name, ver=ver)
        details = [f"依頼 {request}", str(performed), str(row.get("根拠・備考", "") or "")]
        if name in ("新商品 初期設定", "追加便対応料（3便目以降）"):
            notice = str(row.get("通知日", "") or "").strip()
            early = False
            if notice:
                try:
                    notified = date.fromisoformat(notice[:10])
                except Exception as exc:
                    raise ValueError(f"{request}：通知日が不正です") from exc
                if notified > performed:
                    raise ValueError(f"{request}：通知日が実施日より後です")
                early = (performed - notified).days >= 14
            if name == "新商品 初期設定":
                p = price(name, 2 if q >= 10 else 1, ver) if early and q >= 5 else price(name, 0, ver)
            else:
                if q > 1:
                    raise ValueError(f"{request}：3便を超える追加便は事前協議が必要です")
                p = price(name, 0 if early else 1, ver)
            details.append(f"通知日 {notice or '未記入（割引なし）'}")
        if name == "FBA対応費" and q not in (0, 1):
            raise ValueError(f"{request}：FBAは1依頼につき1件です。箱数を入力しないでください")
        if name == "車両受入費" and bool(row.get("時間外再手配", False)):
            p += price(name, 1, ver)
            details.append("時間帯外到着による段取り変更あり。待機・当社都合の前倒しは対象外")
        item = line(name, p, q, LAYER[name], event_unit(name), "／".join(d for d in details if d))
        item["要約"] = f"{request}（{performed}）"
        result.append(item)
    return result


def extra_lines(year, month):
    ver = version(year, month)
    ym = f"{int(year)}-{int(month):02}"
    return [line(x["品名"], x["金額"], 1, "その他", "式", x.get("note", x["source"]))
            for x in ver["extras"] if ym in x["months"]]


def totals(lines, support=0, storage_discount=0):
    """管理費は第1層×5%（四捨五入）。応援控除・保管費値引きは最後に差し引き、管理費には影響させない。"""
    output = [dict(x) for x in lines if x["数量"] != 0]
    base = sum(x["金額"] for x in output if x["区分"] == "第1層")
    storage = sum(x["金額"] for x in output if x["区分"] == "保管")
    management = yen(Decimal(base) * MANAGEMENT_RATE)
    output.append(line("管理費", management, 1, "管理費", "式", f"第1層{base:,}円 × 5%（四捨五入）"))
    before = sum(x["金額"] for x in output)
    support = amount(support, "応援控除", integer=True)
    discount = amount(storage_discount, "保管費値引き", integer=True)
    if discount > storage:
        raise ValueError("保管費値引きは保管費の合計以下にしてください")
    if support + discount > before:
        raise ValueError("控除額の合計が請求額を超えています")
    for name, value in (("応援控除", support), ("保管費値引き", discount)):
        if value:
            output.append({**line(name, value, 1, "控除", "式"), "金額": -int(value), "単価": -value})
    subtotal = sum(x["金額"] for x in output)
    tax = yen(Decimal(subtotal) * TAX_RATE)
    return {"items": output, "management_base": base, "subtotal": subtotal,
            "tax": tax, "total": subtotal + tax}


# ---- Eシス・B2・ヤマトの照合結果（teamec-billing-fetch が作る JSON）----
RESULT_KEYS = ("対象月", "出荷作業料", "出荷作業料_サイズ別", "資材費", "資材費_サイズ別", "送料合計",
               "出荷稼働日", "FBA依頼", "入庫_ピース数", "要確認")


def check_result(result: dict, year: int, month: int) -> dict:
    """照合結果の形と対象月を確かめる。欠けていれば読み込まない（0円として扱わない）。"""
    missing = [k for k in RESULT_KEYS if k not in result]
    if missing:
        raise ValueError("照合結果に必要な項目がありません：" + "、".join(missing))
    if result["対象月"] != f"{int(year)}-{int(month):02}":
        raise ValueError(f"照合結果の対象月（{result['対象月']}）が請求の対象月と違います")
    return result


def result_lines(result: dict, ver=None) -> list[dict]:
    """照合結果から、出荷作業料・ピース入庫（第1層）と資材費・送料（実費）の明細を作る。"""
    sizes = "・".join(f"{s}×{n:,}" for s, n in result["出荷作業料_サイズ別"].items() if n)
    mats = "・".join(f"{r['サイズ']} {r['個口数']:,}個口×{r['単価']}" for r in result["資材費_サイズ別"])
    lines = [line("出荷作業料", result["出荷作業料"], 1, "第1層", "式", f"商品サイズ別PCS：{sizes}（Eシス注文明細）"),
             line("資材費", result["資材費"], 1, "実費", "式", f"個口：{mats}（ヤマト実サイズ）"),
             line("送料", result["送料合計"], 1, "実費", "式", "ヤマト運賃情報（税別）×B2発行済TE")]
    pieces = result.get("入庫_ピース数")
    if pieces:
        lines.append(line("入庫：ピース納品", price("入庫：ピース納品", ver=ver), pieces, "第1層", "pcs",
                          "Eシス入庫履歴（ケース区分の記載なし）"))
    return lines


def storage_summary(count_rows, year, month) -> list[dict]:
    """保管カウントページの「カウント状況（2期平均→保管料）」と同じ形の集計（新体系の単価）。"""
    lines = {x["品名"]: x for x in storage_lines(count_rows, year, month)}
    out = []
    for name, (item, unit_price) in version(year, month)["storage"].items():
        if item not in lines:
            continue
        rows = [r for r in count_rows if str(r.get("種別", "")).strip() == name]
        first = sum(amount(r.get("数量", 0)) for r in rows if r.get("期") == "第1期")
        second = sum(amount(r.get("数量", 0)) for r in rows if r.get("期") == "第2期")
        out.append({"種別": name, "第1期合計": float(first), "第2期合計": float(second),
                    "平均": float((first + second) / 2), "単価": unit_price, "金額": lines[item]["金額"]})
    return out


# ---- 共有シート（随時連絡・返品交換・配送変更依頼・依頼台帳）と新商品（棚番の設定）----
AUTO_MARKS = ("【シート】", "【Eシス】")   # 自動で入れた行の根拠の先頭。人が直した行と見分ける
# 依頼台帳のたたき台で選べる品目 → (区分, 単位, 料金表の項目名。None は実費で単価を手入力)
LEDGER_ITEMS = {"汎用作業料": ("第3層", "人時", "汎用作業料"),
                "トラックチャーター（実費）": ("実費", "式", None),
                "資材（実費）": ("実費", "式", None),
                "その他（実費）": ("実費", "式", None)}
LEDGER_ITEMS.update({n: (LAYER[n], event_unit(n), n) for n in (
    STOCK_RESTORE, "随時連絡対応（起票・調査）", "入庫差異の報告・照会", "配送種別・個口数の変更")})
# 依頼の文面 → 品目の候補（当たったものを全部たたき台に出す。1つの依頼が複数行になってよい）。
# あくまで「こうかな？」の候補。人が確認して、追加・削除・変更する（2026-10-08 本人指示）
LEDGER_RULES = (
    (r"ラベル|シール|張替|貼り替|貼付|タグ付", "汎用作業料", "ラベル・シールの作業"),
    (r"セット.{0,6}(作成|組|変換)|組換|組み替|入れ替え|詰め替|バラ", "汎用作業料", "セット組み・入れ替えの作業"),
    (r"検品|仕分|梱包|袋詰|同梱", "汎用作業料", "検品・梱包の作業"),
    (r"チャーター|トラック|配車", "トラックチャーター（実費）", "チャーター便"),
    (r"資材.{0,4}(依頼|使用|手配)|手書き", "資材（実費）", "資材・手書き伝票"),
    (r"在庫.{0,3}(移動|戻)|在庫調整", STOCK_RESTORE, "在庫の移動・戻し"),
    (r"在庫.{0,6}(ずれ|差異|合わ)|原因調査", "入庫差異の報告・照会", "在庫差異の調査"),
)
LEDGER_FREE = ("通常内", "無償")


def ledger_state(result) -> str:
    """共有シートを読めているか。空文字なら読めている。読めていない理由を返す（0件と区別する）。"""
    if result is None:
        return "照合結果を読めていません"
    if "台帳" not in result:
        return "この照合結果は共有シートに対応する前のものです"
    if result["台帳"] is None:
        return "共有シートを取得できていません"
    return ""


def _on(row) -> bool:
    return row.get("計上") is True or row.get("計上") == 1


def _md(day) -> str:
    """2026-09-07 → 9/7（表の名称用）。"""
    try:
        d = date.fromisoformat(str(day)[:10])
        return f"{d.month}/{d.day}"
    except Exception:
        return str(day or "")


def sheet_tables(result, ver=None) -> dict:
    """確認する元ごとの確認表（1つの元＝1表）の元データ。1列は1つの意味だけを持つ（列をまとめない）。

    画面の列順は ui.SHEET_COLUMNS（名称 → 金額 → 計上 → 理由）。各行の「計上」を外すと請求から外れる。
    """
    led = (result or {}).get("台帳") or {}

    def yen_(name):
        return int(price(name, ver=ver))

    fba = [{"受注番号": f["注文番号"], "金額": yen_("FBA対応費"), "計上": True, "発送日": f["発送日"], "注文ID": f["注文ID"]}
           for f in (result or {}).get("FBA依頼", [])]
    contacts = [{"発生内容": r["発生内容"], "金額": yen_("随時連絡対応（起票・調査）"), "計上": True, "発生日": r["発生日"],
                 "受注番号": r["受注番号"], "ステータス": r["ステータス"], "送り状番号": r.get("送り状番号", "")}
                for r in led.get("随時連絡", [])]
    returns = []
    for r in led.get("返品", []):
        cod = "着払" in str(r.get("送料負担方法", ""))
        confirmed, noted = r.get("着払い送料"), _yen_or_none(r.get("パピー記入欄"))
        state = "元払い" if not cod else "確定" if confirmed is not None else "仮"
        returns.append({"送り状番号": r["送り状番号"], "返品処理料": yen_("返品処理"),
                        "計上する送料": (confirmed if confirmed is not None else noted) if cod else 0, "計上": True,
                        "送料の状態": state, "ヤマト請求額": confirmed, "パピー記入欄": r.get("パピー記入欄", ""),
                        "運送会社": r.get("運送会社", ""), "送料負担": r.get("送料負担方法", ""), "到着日": r["日付"],
                        "注文番号": r.get("注文番号", ""), "運送会社の根拠": r.get("運送会社の根拠", "")})
    changes = [{"注文ID": r.get("注文ID") or r["伝票番号"], "金額": yen_("配送種別・個口数の変更"), "計上": True,
                "日付": r["日付"], "変更前": r["変更前"], "変更後": r["変更後"], "伝票番号欄": r["伝票番号"]}
               for r in led.get("配送変更", [])]
    new = [{"表示用コード": x.get("表示用コード") or x["JANコード"], "計上": True, "14日前通知": False, "商品名": x.get("商品名", ""),
            "棚番": x.get("棚番", ""), "確認日": x["確認日"], "根拠": x.get("根拠", "棚番が未設定→設定"), "JANコード": x["JANコード"]}
           for x in ((result or {}).get("新商品") or {}).get("明細", [])]
    return {"FBA": fba, "随時連絡": contacts, "返品": returns, "配送変更": changes, "新商品": new}


def sheet_event_rows(tables, target_ym) -> list[dict]:
    """確認表で「計上」になっている行を、依頼ごとの作業の行にする。

    新商品は行ごとの「14日前通知」で分ける（同じ月に通知があるもの・無いものが混ざるため。2026-10-08 本人指示）。
    通知ありのSKU数が5以上・10以上なら単価が下がる。通知なしは通常の単価。
    """
    from datetime import timedelta

    def row(day, request, name, qty, basis, notified=""):
        return {"実施日": day, "依頼ID": request, "作業": name, "数量": qty, "通知日": notified,
                "時間外再手配": False, "根拠・備考": basis}

    rows, used = [], set()

    def unique(text):
        base, n = text, 1
        while text in used:
            n += 1
            text = f"{base}-{n}"
        used.add(text)
        return text

    for r in tables.get("FBA", []):
        if _on(r):
            rows.append(row(r["発送日"], unique(r["受注番号"]), "FBA対応費", 1, f"Eシス注文ID {r['注文ID']}"))
    for noticed in (True, False):
        new = [r for r in tables.get("新商品", [])
               if _on(r) and (r.get("14日前通知") is True or r.get("14日前通知") == 1) == noticed]
        if new:
            day = max(r["確認日"] for r in new)
            notified = (date.fromisoformat(day[:10]) - timedelta(days=14)).isoformat() if noticed else ""
            codes = "、".join(r["表示用コード"] for r in new)
            rows.append(row(day, f"新商品{target_ym}{'・14日前通知あり' if noticed else ''}", "新商品 初期設定", len(new),
                            f"【Eシス】新商品：{codes}"[:300], notified))
    for r in tables.get("随時連絡", []):
        if _on(r):
            rows.append(row(r["発生日"], unique(f"{r['受注番号'] or '随時連絡'}／{r['発生内容']}"), "随時連絡対応（起票・調査）",
                            1, f"【シート】随時連絡：{r['発生内容']}（{r['ステータス']}）"))
    for r in tables.get("返品", []):
        if _on(r):
            rows.append(row(r["到着日"], unique(r["送り状番号"] or r.get("注文番号") or f"返品{r['到着日']}"), "返品処理", 1,
                            f"【シート】返品交換：{r['送料負担'] or '送料負担の記載なし'}・{r['運送会社']}"))
    for r in tables.get("配送変更", []):
        if _on(r):
            rows.append(row(r["日付"], unique(f"注文ID {r['注文ID']}"), "配送種別・個口数の変更", 1,
                            f"【シート】配送変更依頼：{r['変更前']}→{r['変更後']}（伝票番号欄 {r['伝票番号欄']}）"))
    return rows


def auto_event_rows(result) -> list[dict]:
    """共有シートとEシスの棚番から作る、依頼ごとの作業の行（確認表をそのまま全部計上した場合）。"""
    return sheet_event_rows(sheet_tables(result), (result or {}).get("対象月", ""))


def _yen_or_none(text):
    t = str(text if text is not None else "").replace(",", "").replace("円", "").strip()
    return int(t) if t.isdigit() else None


def return_freight_rows(result) -> list[dict]:
    """返品交換の確認表（請求で確かめた送料と、シートの「パピー記入欄」＝仮を別の列に分ける）。"""
    return sheet_tables(result)["返品"]


def return_freight(rows) -> tuple[int, str]:
    """返品交換の確認表（人が直したあと）から、着払い送料の合計と内訳。計上する着払いの金額が空なら止める。"""
    total, parts = 0, []
    for r in rows:
        if not _on(r):
            continue
        value = r.get("計上する送料")
        if value in ("", None) or pd_isna(value):
            if "着払" in str(r.get("送料負担", "")):
                raise ValueError(f"返品 {r.get('到着日', '')}（送り状 {r.get('送り状番号') or 'なし'}）：着払い送料が空です。"
                                 "運送会社の請求を確認して「計上する送料」に入れてください（請求しないなら0）")
            continue
        n = int(amount(value, "返品の着払い送料", integer=True))
        if n:
            total += n
            parts.append(f"{r.get('到着日', '')}着 {n:,}円（{r.get('運送会社', '')}）")
    return total, "、".join(parts)


def ledger_requests(result) -> list[dict]:
    """依頼台帳のその月の依頼（見るだけの一覧）。"""
    return [{"依頼No": r["依頼No"], "日付": r["日付"], "区分": r.get("区分", ""), "内容": r["内容"],
             "対応状況": r["対応状況"], "作業費（台帳）": r["作業費"], "パピー記入": r.get("パピー記入", "")}
            for r in ((result or {}).get("台帳") or {}).get("依頼台帳", [])]


def ledger_rows(result) -> list[dict]:
    """依頼台帳の請求のたたき台（依頼×品目の行）。依頼の内容そのものは ledger_requests で別に見せる。

    文面に当たった品目を全部候補に出す（1つの依頼が複数行になってよい）。当たらなければ汎用作業料を1行。
    台帳で「通常内」「対応なし」の依頼は、品目を空にして1行だけ出す（＝請求しない。必要なら人が品目を入れる）。
    数量（人時）や実費の金額は台帳に無いので空欄のまま。人が入れる・消す・足す。
    """
    import re
    rows = []
    for r in ((result or {}).get("台帳") or {}).get("依頼台帳", []):
        info = {"依頼No": r["依頼No"], "日付": r["日付"], "内容": r["内容"]}
        if any(w in str(r.get("作業費", "")) for w in LEDGER_FREE) or r.get("対応状況") == "対応なし":
            rows.append({**info, "品目": None, "数量": None, "単価": None, "メモ": "台帳で通常内・対応なしのため請求なし"})
            continue
        found = {}
        for pattern, item, why in LEDGER_RULES:
            if re.search(pattern, r["内容"]):
                found.setdefault(item, []).append(why)
        if not found:
            found = {"汎用作業料": ["文面から品目を決められません（仮に汎用作業）"]}
        for item, whys in found.items():
            fixed = LEDGER_ITEMS[item][2]
            rows.append({**info, "品目": item, "数量": 1 if fixed and fixed != "汎用作業料" else None, "単価": None,
                         "メモ": "候補：" + "・".join(whys)})
    return rows


def ledger_lines(rows, year, month):
    """依頼台帳のたたき台の行を明細にする。数量・単価が空の行は0円にせず止める（消すか入れるかを人が決める）。"""
    ver = version(year, month)
    result = []
    for r in rows:
        name = str(r.get("品目") or "").strip()
        no = str(r.get("依頼No") or "").strip()
        if not name:
            continue   # 品目が空の行は請求しない（依頼の内容を見せるためだけの行）
        label = f"依頼No.{no or '（空欄）'}"
        if not no:
            raise ValueError(f"依頼台帳のたたき台：「{name}」の行に依頼Noを入れてください")
        if name not in LEDGER_ITEMS:
            raise ValueError(f"{label}：品目を選んでください")
        layer, unit, priced = LEDGER_ITEMS[name]
        if r.get("数量") in ("", None) or pd_isna(r.get("数量")):
            raise ValueError(f"{label}「{name}」：数量（{unit}）を入れるか、請求しないなら品目を空にしてください")
        q = amount(r["数量"], label + "の数量")
        if not q:
            raise ValueError(f"{label}「{name}」：数量が0です。請求しないなら品目を空にしてください")
        if priced == "汎用作業料" and q * 4 != (q * 4).to_integral_value():
            raise ValueError(f"{label}：人時は15分単位（0.25刻み）で入れてください")
        if priced:
            p_ = price(priced, ver=ver)
        else:
            if r.get("単価") in ("", None) or pd_isna(r.get("単価")):
                raise ValueError(f"{label}「{name}」：実費の単価（税抜・円）を入れてください")
            p_ = amount(r["単価"], label + "の単価")
        memo = str(r.get("メモ") or "")
        item = line(name, p_, q, layer, unit, f"依頼台帳 No.{no}（{r.get('日付') or ''}）{'' if memo.startswith('候補：') else memo}"[:120])
        item["要約"] = f"依頼台帳No.{no}"
        result.append(item)
    return result


# ---- 依頼ごとの作業の選択肢（単価を名前に入れる。条件で単価が変わるものは選択肢を分ける）----
NOTICE = "・14日前までに通知あり"
OFF_HOURS = "・時間帯外の到着で段取り変更"


def event_choices(ver=None) -> dict:
    """画面の「作業」の選択肢 → (料金表の項目, 事前通知あり, 時間帯外)。単価は版から作る（文言をベタ書きしない）。"""
    def yen_(name, i=0):
        return f"{int(price(name, i, ver)):,}円"

    out = {}
    for name in EVENT_NAMES:
        unit = event_unit(name)
        if name == "新商品 初期設定":
            out[f"{name}（{yen_(name)}/{unit}）"] = (name, False, False)
            out[f"{name}{NOTICE}（5{unit}以上 {yen_(name, 1)}・10{unit}以上 {yen_(name, 2)}）"] = (name, True, False)
        elif name == "追加便対応料（3便目以降）":
            out[f"{name}{NOTICE}（{yen_(name)}/{unit}）"] = (name, True, False)
            out[f"{name}・通知なし（{yen_(name, 1)}/{unit}）"] = (name, False, False)
        elif name == "車両受入費":
            out[f"{name}（{yen_(name)}/{unit}）"] = (name, False, False)
            out[f"{name}{OFF_HOURS}（{int(price(name, 0, ver) + price(name, 1, ver)):,}円/{unit}）"] = (name, False, True)
        else:
            out[f"{name}（{yen_(name)}/{unit}）"] = (name, False, False)
    return out


def choice_label(row, ver=None) -> str:
    """計算用の行（作業・通知日・時間外再手配）や古い下書きの行を、画面の選択肢の名前にする。"""
    choices = event_choices(ver)
    name = str(row.get("作業") or "").strip()
    if name in choices or not name:
        return name
    noticed = bool(str(row.get("通知日") or "").strip())
    off = row.get("時間外再手配") is True or row.get("時間外再手配") == 1
    if name == "追加便対応料（3便目以降）" and not noticed:
        return next(k for k, v in choices.items() if v[0] == name and not v[1])
    for label, (n, notice, off_hours) in choices.items():
        if n == name and notice == (noticed and name != "車両受入費") and off_hours == off:
            return label
    return next((k for k, v in choices.items() if v[0] == name), name)


def choice_rows(rows, ver=None) -> list[dict]:
    """画面の行（作業＝選択肢の名前）を計算用の行に戻す。「通知あり」は実施日の14日前を通知日として渡す。"""
    from datetime import timedelta
    choices = event_choices(ver)
    out = []
    for r in rows:
        label = str(r.get("作業") or "").strip()
        name, notice, off = choices.get(label, (label, False, False))
        notified = ""
        if notice:
            try:
                notified = (date.fromisoformat(str(r.get("実施日"))[:10]) - timedelta(days=14)).isoformat()
            except Exception:
                notified = ""
        out.append({**r, "作業": name, "通知日": notified, "時間外再手配": off})
    return out


def amount_text(lines) -> str:
    """明細を「依頼 作業 単価×数量＝金額」の1行ずつにする（表を増やさずに金額を見せる）。"""
    return "  \n".join(f"{x.get('要約', '')}　{x['品名']}　{int(x['単価']):,}円×{float(x['数量']):g}＝{x['金額']:,}円" for x in lines)


def amount_rows(lines) -> list[dict]:
    """明細を「依頼・作業・単価・数量・金額」の表にする（依頼に対する金額を、その場で見せるため）。"""
    return [{"依頼": x.get("要約", ""), "作業": x["品名"], "単価": float(x["単価"]), "数量": float(x["数量"]),
             "金額": x["金額"]} for x in lines]


def pd_isna(value) -> bool:
    return isinstance(value, float) and value != value


def merge_lines(lines):
    """品名・単価・区分・単位が同じ明細を1行にまとめる（FBA対応費が依頼の数だけ並ぶのを防ぐ）。"""
    merged, order = {}, []
    for x in lines:
        key = (x["区分"], x["品名"], x["単価"], x["単位"])
        if key not in merged:
            merged[key] = {"line": dict(x), "parts": []}
            order.append(key)
        else:
            merged[key]["line"]["数量"] += x["数量"]
        merged[key]["parts"].append(x)
    out = []
    for key in order:
        m, parts = merged[key]["line"], merged[key]["parts"]
        if len(parts) > 1:
            m["金額"] = yen(m["単価"] * m["数量"])
            m["詳細"] = f"{len(parts)}件：" + "、".join(str(p.get("要約") or p["詳細"]) for p in parts)
        m.pop("要約", None)
        out.append(m)
    return out


# ---- 先方に送る内訳明細（Excel）のシート。どのシートも「名称 → 金額 → 理由」の列順（2026-10-08 本人指示）----
def breakdown_sheets(items, result, active_days, counts, irregular_work, request_lines, return_rows, year, month):
    """[(シート名, 行のリスト, 合計を出す列)]。社内の確認用の言葉（仮・要確認・自動など）は載せない。"""
    ver = version(year, month)

    def num(v):
        f = float(v)
        return int(f) if f == int(f) else f

    sheets = [("請求明細", [{"品名": x["品名"], "金額": int(x["金額"]), "単価": num(x["単価"]), "数量": num(x["数量"]),
                             "単位": x.get("単位", ""), "内容": _plain(x.get("詳細", ""))} for x in items], "金額")]
    days = {r["日付"]: r for r in calendar_rows(year, month)}
    d_ = dispatch(ver)
    daily = int(price("日次締め処理", ver=ver) + price("日次運用費", ver=ver))
    sheets.append(("出荷稼働日", [{"日付": d, "金額": days[d]["定額回数"] * d_["price"] + daily,
                                   "出荷指示作成料": days[d]["定額回数"] * d_["price"], "日次締め処理": int(price("日次締め処理", ver=ver)),
                                   "日次運用費": int(price("日次運用費", ver=ver)), "曜日": days[d]["曜日"], "祝日": days[d]["祝日"]}
                                  for d in active_days], "金額"))
    pick = result.get("出荷作業料_明細") or []
    sheets.append(("出荷作業費", [{"注文ID": r["注文ID"], "金額": r["金額"], "単価": r["単価"], "数量": r["数量"], "サイズ": r["サイズ"],
                                   "表示用コード": r.get("表示用コード", ""), "JANコード": r.get("JANコード", ""),
                                   "発送日": r.get("発送日", "")} for r in pick], "金額"))
    sheets.append(("資材費", [{"サイズ": r["サイズ"], "金額": r["金額"], "単価": r["単価"], "個口数": r["個口数"]}
                              for r in result["資材費_サイズ別"]], "金額"))
    sheets.append(("保管費", [{"種別": r["種別"], "金額": r["金額"], "単価": r["単価"], "平均": r["平均"],
                               "第1期合計": r["第1期合計"], "第2期合計": r["第2期合計"]}
                              for r in storage_summary(counts or [], year, month)], "金額"))
    unit = price("汎用作業料", ver=ver)
    sheets.append(("汎用作業費", [{"作業項目": r.get("作業項目", ""), "金額": yen(amount(r["時間数"]) * amount(r["人数"]) * unit),
                                   "単価": int(unit), "時間数": num(r["時間数"]), "人数": num(r["人数"]), "日付": r.get("日付", ""),
                                   "作業詳細": r.get("作業詳細", "")} for r in (irregular_work or [])], "金額"))
    sheets.append(("依頼ごとの作業", [{"作業": x["品名"], "金額": int(x["金額"]), "単価": num(x["単価"]), "数量": num(x["数量"]),
                                       "依頼": x.get("要約", ""), "内容": _plain(x.get("詳細", ""))} for x in request_lines], "金額"))
    sheets.append(("返品の着払い送料", [{"送り状番号": r.get("送り状番号", ""), "金額": int(r["計上する送料"]),
                                         "運送会社": r.get("運送会社", ""), "到着日": r.get("到着日", "")}
                                        for r in return_rows if _on(r) and r.get("計上する送料") not in ("", None)
                                        and not pd_isna(r.get("計上する送料")) and int(r["計上する送料"])], "金額"))
    piece = int(price("入庫：ピース納品", ver=ver))
    sheets.append(("入庫", [{"表示用コード": r.get("表示用コード", ""), "金額": r["数量"] * piece, "単価": piece, "数量": r["数量"],
                             "入庫日": r.get("入庫日", ""), "仕入先": r.get("仕入先", ""), "伝票番号": r.get("伝票番号", "")}
                            for r in result.get("入庫", [])], "金額"))
    return [(name, rows, col) for name, rows, col in sheets if rows]


def _plain(text) -> str:
    """社内向けの印（【シート】【Eシス】（自動）や「依頼 xxx／日付／」の前置き）を落として、先方に見せる文にする。"""
    t = str(text or "")
    parts = t.split("／")
    if len(parts) >= 2 and parts[0].startswith("依頼 "):
        t = f"{parts[0][3:]}（{parts[1]}）" + "／".join(parts[2:])
    for mark in AUTO_MARKS + ("（自動）",):
        t = t.replace(mark, "")
    return t.strip()
