"""料金表の保存版を正本にする副作用のない試算エンジン。

未確定の運用はPolicyで明示し、試算結果を正式な請求と混同しない。
元データの未取得と実績ゼロはUIで別々に管理する。
"""
from __future__ import annotations

import calendar
import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from pathlib import Path

import jpholiday

SOURCE = json.loads(Path(__file__).with_name("source_rates.json").read_text(encoding="utf-8"))
ROWS = {row[1]: row for row in SOURCE["values"] if len(row) >= 4}
APPLIES_FROM = (2026, 9)
# 2026-10-02の本人回答2。料金表の40円/件に優先する明示的な差分。
DISPATCH = {"price": 1200, "weekday": 2, "holiday": 1,
            "source": "2026-10-02 本人回答2：平日2回・土日祝1回で一律課金"}
MANAGEMENT_RATE = Decimal("0.05")


def amount(value, name="値", *, integer=False) -> Decimal:
    """空欄・非数・負数を0にしない。UIの明示的な0だけを受理する。"""
    try:
        n = Decimal(str(value).strip().replace(",", ""))
    except Exception as exc:
        raise ValueError(f"{name}は数値を入力してください") from exc
    if not n.is_finite() or n < 0 or (integer and n != n.to_integral_value()):
        raise ValueError(f"{name}は0以上の{'整数' if integer else '数値'}を入力してください")
    return n


def yen(value, rounding="四捨五入") -> int:
    if rounding not in ("四捨五入", "切り捨て"):
        raise ValueError("端数処理を選んでください")
    return int(Decimal(str(value)).quantize(Decimal("1"),
               rounding=ROUND_HALF_UP if rounding == "四捨五入" else ROUND_FLOOR))


def prices(name: str) -> list[Decimal]:
    """円建て単価だけを抽出。5SKU・0.25時間等を誤って単価にしない。"""
    text = ROWS[name][2]
    return [Decimal(s.replace(",", "")) for s in re.findall(r"([\d,]+)円", text)]


def price(name: str, index=0) -> Decimal:
    return prices(name)[index]


def calendar_rows(year: int, month: int) -> list[dict]:
    result = []
    for day in range(1, calendar.monthrange(year, month)[1] + 1):
        d = date(year, month, day)
        holiday = jpholiday.is_holiday_name(d)
        off = bool(holiday or d.weekday() >= 5)
        result.append({"日付": d.isoformat(), "曜日": "月火水木金土日"[d.weekday()],
                       "祝日": holiday or "", "稼働": not off,
                       "定額回数": DISPATCH["holiday" if off else "weekday"]})
    return result


@dataclass(frozen=True)
class Policy:
    labor_rounding: str = "依頼ごとの合計人分"
    management_rounding: str = "四捨五入"
    support_reduces_base: bool = True


def line(name, unit_price, quantity, category, unit="件", detail=""):
    p = amount(unit_price, name + "単価")
    q = amount(quantity, name + "数量")
    return {"区分": category, "品名": name, "単価": p, "数量": q,
            "単位": unit, "金額": yen(p * q), "詳細": detail}


def daily_lines(year, month, active_days):
    if (year, month) < APPLIES_FROM:
        raise ValueError("新体系は2026年9月作業分からです。8月以前は既存のTeam-ECを選んでください")
    all_days = {r["日付"]: r for r in calendar_rows(year, month)}
    selected = list(active_days)
    if len(set(selected)) != len(selected) or any(d not in all_days for d in selected):
        raise ValueError("稼働日は対象月の重複しない日付にしてください")
    batches = sum(all_days[d]["定額回数"] for d in selected)
    detail = "稼働日：" + "、".join(selected)
    return [line("出荷指示作成料", DISPATCH["price"], batches, "第1層", "回", detail),
            line("日次締め処理", price("日次締め処理"), len(selected), "第1層", "日", detail),
            line("日次運用費", price("日次運用費"), len(selected), "第2層", "日", detail)]


STORAGE_NAMES = ("保管料（パレット）", "保管料（中量棚）", "保管料（当社指定ロケーション）")


def storage_lines(rows):
    result = []
    seen = set()
    for row in rows:
        name = row["種別"]
        if name not in STORAGE_NAMES or name in seen:
            raise ValueError("保管種別が不明、または重複しています")
        seen.add(name)
        first = amount(row["15日"], name + "15日数量")
        last = amount(row["月末"], name + "月末数量")
        result.append(line(name, price(name), (first + last) / 2, "保管",
                           "棚" if "中量棚" in name else "PL",
                           f"15日 {first}・月末 {last} の平均。同一場所は1種別にだけ計上"))
    return result


# 依頼ごとの実績。日次・保管・実費をここに混ぜない。
EVENT_NAMES = tuple(name for name, row in ROWS.items()
                    if row[0] in ("第1層", "第3層") and name not in
                    ("出荷作業料", "出荷指示作成料", "日次締め処理"))


def event_unit(name):
    if name == "汎用作業料":
        return "人時"
    condition = ROWS[name][3]
    for term, unit in (("1パレット", "PL"), ("1箱", "箱"), ("1点", "pcs"),
                       ("1SKU", "SKU"), ("1台", "台"), ("1依頼", "依頼"), ("1回", "回"), ("1件", "件")):
        if condition.startswith(term):
            return unit
    raise ValueError(f"{name}：料金表の課金単位を確認してください")


def event_lines(rows, policy: Policy, year: int, month: int):
    result, seen = [], set()
    for row in rows:
        name = str(row.get("作業", "")).strip()
        request = str(row.get("依頼ID", "")).strip()
        if not name and not request:
            if any(str(v).strip() not in ("", "0", "0.0", "False", "None")
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
        return_name = "返品処理"
        stock_name = "在庫調整（在庫戻し／一時在庫へ移動）"
        if (name == return_name and (request, stock_name) in seen or
                name == stock_name and (request, return_name) in seen):
            raise ValueError(f"{request}：返品由来の在庫戻しは返品処理に含まれます。重複計上を確認してください")
        seen.add(identity)
        q = amount(row.get("数量", ""), request + "数量", integer=True)
        p = price(name)
        details = [f"依頼 {request}", str(performed), str(row.get("根拠・備考", ""))]
        if name in ("新商品 初期設定", "追加便対応料（3便目以降）"):
            notice = str(row.get("通知日", "")).strip()
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
                p = prices(name)[2 if q >= 10 else 1] if early and q >= 5 else prices(name)[0]
            else:
                if q > 1:
                    raise ValueError(f"{request}：3便を超える追加便は事前協議が必要です")
                p = prices(name)[0 if early else 1]
            details.append(f"通知日 {notice or '未記入（割引なし）'}")
        if name == "FBA対応費" and q not in (0, 1):
            raise ValueError(f"{request}：FBAは1依頼につき1件です。箱数を入力しないでください")
        if name == "汎用作業料":
            minutes = amount(row.get("分/人", ""), request + "作業分数")
            people = amount(row.get("人数", ""), request + "人数", integer=True)
            if q != 1 or people < 1:
                raise ValueError(f"{request}：汎用作業は数量1・人数1以上で入力してください")
            if policy.labor_rounding == "依頼ごとの合計人分":
                q = (minutes * people / 15).to_integral_value(rounding=ROUND_CEILING) / 4
            elif policy.labor_rounding == "各人ごと":
                q = (minutes / 15).to_integral_value(rounding=ROUND_CEILING) * people / 4
            else:
                raise ValueError("人時の丸め方が不明です")
            details.append(f"{minutes}分×{people}人・{policy.labor_rounding}を15分単位で切上げ")
        if name == "車両受入費" and bool(row.get("時間外再手配", False)):
            p += prices(name)[1]
            details.append("時間帯外到着による段取り変更あり。待機・当社都合の前倒しは対象外")
        result.append(line(name, p, q, ROWS[name][0], event_unit(name),
                           "／".join(details)))
    return result


def totals(lines, support=0, storage_discount=0, policy=Policy()):
    output = [dict(x) for x in lines if x["数量"] != 0]
    base = sum(x["金額"] for x in output if x["区分"] == "第1層")
    storage = sum(x["金額"] for x in output if x["区分"] == "保管")
    support = amount(support, "応援控除", integer=True)
    discount = amount(storage_discount, "保管費値引き", integer=True)
    if support > base or discount > storage:
        raise ValueError("控除額はそれぞれ第1層・保管費の合計以下にしてください")
    if support:
        output.append({**line("応援控除", support, 1, "控除", "式"), "金額": -int(support), "単価": -support})
    if discount:
        output.append({**line("保管費値引き", discount, 1, "控除", "式"), "金額": -int(discount), "単価": -discount})
    management_base = base - (int(support) if policy.support_reduces_base else 0)
    management = yen(Decimal(management_base) * MANAGEMENT_RATE, policy.management_rounding)
    output.append(line("管理費", management, 1, "管理費", "式",
                       f"第1層{management_base:,}円 × 5%（{policy.management_rounding}）"))
    subtotal = sum(x["金額"] for x in output)
    tax = yen(Decimal(subtotal) * Decimal("0.1"))
    return {"items": output, "management_base": management_base, "subtotal": subtotal,
            "tax": tax, "total": subtotal + tax}
