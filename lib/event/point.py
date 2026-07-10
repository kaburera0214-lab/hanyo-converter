# -*- coding: utf-8 -*-
"""
ポイント変倍の設定・解除(RMS Item API 2.0)。

PATCH /es/2.0/items/manage-numbers/{管理番号}
  {"pointCampaign": {"applicablePeriod": {"start": "YYYY-MM-DDTHH:MM:SS+09:00",
                                          "end":   "YYYY-MM-DDTHH:MM:SS+09:00"},
                     "benefits": {"pointRate": N}}}
解除は {"pointCampaign": null} をPATCHする。
(構造の出典: JakeJP/Rakuten.RMS.Api ItemAPI20/PointCampaign.cs, Period.cs)
"""
import re

from . import rms_api


class PointError(RuntimeError):
    pass


def _fmt_dt(text):
    """'2026-09-04 20:00' → '2026-09-04T20:00:00+09:00'。"""
    s = str(text or "").strip().replace("/", "-")
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})(?::\d{2})?$", s)
    if not m:
        raise PointError(f"日時は『YYYY-MM-DD HH:MM』形式で入力してください: {text}")
    return f"{m.group(1)}T{m.group(2)}:00+09:00"


def get_current(manage_number):
    """商品の現在のポイント変倍設定を返す(未設定はNone)。"""
    data = rms_api.get(f"/es/2.0/items/manage-numbers/{manage_number}")
    item = data.get("item", data)
    pc = item.get("pointCampaign")
    if not pc:
        return None
    period = pc.get("applicablePeriod") or {}
    benefits = pc.get("benefits") or {}
    return {
        "rate": benefits.get("pointRate"),
        "start": period.get("start", ""),
        "end": period.get("end", ""),
    }


def set_campaign(manage_number, *, rate, start, end):
    """1商品にポイント変倍を設定する。"""
    body = {"pointCampaign": {
        "applicablePeriod": {"start": _fmt_dt(start), "end": _fmt_dt(end)},
        "benefits": {"pointRate": int(rate)},
    }}
    rms_api.patch(f"/es/2.0/items/manage-numbers/{manage_number}", body)


def clear_campaign(manage_number):
    """1商品のポイント変倍を解除する。"""
    rms_api.patch(f"/es/2.0/items/manage-numbers/{manage_number}",
                  {"pointCampaign": None})


def bulk_apply(manage_numbers, *, rate, start, end):
    """
    複数商品へ一括設定。戻り値: (成功リスト, {管理番号: エラー文})
    """
    ok, errors = [], {}
    for mn in manage_numbers:
        mn = str(mn).strip()
        if not mn:
            continue
        try:
            set_campaign(mn, rate=rate, start=start, end=end)
            ok.append(mn)
        except Exception as e:  # noqa: BLE001
            errors[mn] = str(e)
    return ok, errors


def bulk_clear(manage_numbers):
    """複数商品の一括解除。戻り値: (成功リスト, {管理番号: エラー文})"""
    ok, errors = [], {}
    for mn in manage_numbers:
        mn = str(mn).strip()
        if not mn:
            continue
        try:
            clear_campaign(mn)
            ok.append(mn)
        except Exception as e:  # noqa: BLE001
            errors[mn] = str(e)
    return ok, errors


def manual_procedure_md(*, rate, start, end, manage_numbers):
    """API不可時の、RMS画面での手動設定手順書(Markdown)を返す。"""
    return f"""# ポイント変倍 手動設定手順

APIでの自動設定ができなかったため、RMS管理画面から以下の内容で設定してください。

## 手順
1. RMSメインメニュー → **1 店舗設定** → **商品管理** → **商品一覧**
2. 下記の各商品を開き、「ポイント変倍」欄に倍率と適用期間を入力して保存

## 設定値
| 項目 | 値 |
|---|---|
| ポイント倍率 | {rate}倍 |
| 適用期間 | {start} 〜 {end} |
| 対象商品 | {"、".join(str(m) for m in manage_numbers)} |

## イベント終了後
同じ画面でポイント変倍欄を空にして保存すると解除されます。
"""
