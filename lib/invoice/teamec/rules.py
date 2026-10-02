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
        {"品名": "協力費", "金額": 20000, "months": ["2026-09"],
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
        result.append(line(name, p, q, LAYER[name], event_unit(name), "／".join(d for d in details if d)))
    return result


def extra_lines(year, month):
    ver = version(year, month)
    ym = f"{int(year)}-{int(month):02}"
    return [line(x["品名"], x["金額"], 1, "その他", "式", x["source"])
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
