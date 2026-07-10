# -*- coding: utf-8 -*-
"""
RMSクーポンAPI(XML)によるクーポン発行・削除。

エンドポイント(実装確認: JakeJP/Rakuten.RMS.Api):
    POST /es/1.0/coupon/issue   → <result><coupon><couponCode/><pcGetUrl/></coupon></result>
    POST /es/1.0/coupon/delete
リクエストは <request><couponIssueRequest><coupon>...</coupon></couponIssueRequest></request>。
日時は dateTime形式(YYYY-MM-DDTHH:MM:SS)。エラーは <error><code/><message/></error>。
"""
import re
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

from . import rms_api

# 値引きタイプ(discountType)と対象タイプ(itemType)の組み合わせ
DISCOUNT_TYPES = {
    "定額値引き（円）": {"discountType": 1, "itemType": 3},
    "定率値引き（%）": {"discountType": 2, "itemType": 3},
    "送料無料": {"discountType": 4, "itemType": 5},
}


class CouponError(RuntimeError):
    pass


def _fmt_dt(text):
    """'2026-09-04 20:00' → '2026-09-04T20:00:00'。不正ならCouponError。"""
    s = str(text or "").strip().replace("/", "-")
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})(?::\d{2})?$", s)
    if not m:
        raise CouponError(f"日時は『YYYY-MM-DD HH:MM』形式で入力してください: {text}")
    return f"{m.group(1)}T{m.group(2)}:00"


def build_issue_xml(*, coupon_name, caption, start, end, discount_label,
                    discount_factor, issue_count, member_max, manage_numbers,
                    all_items=False, combine=True, display=True):
    """クーポン発行リクエストXMLを組み立てる(発行せずXMLだけ返す。テスト・確認用)。"""
    if discount_label not in DISCOUNT_TYPES:
        raise CouponError(f"値引きタイプが不正です: {discount_label}")
    conf = DISCOUNT_TYPES[discount_label]
    # itemType: 1=単一商品 / 3=複数商品 / 4=受注(全商品) / 5=送料無料
    if all_items:
        item_type = 4
    elif conf["itemType"] == 5:
        item_type = 5
    else:
        item_type = 1 if len(manage_numbers or []) == 1 else 3
    # 要素間に改行等のテキストノードを入れるとXSD検証で弾かれるため1行で組む
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<request><couponIssueRequest><coupon>",
        f"<couponName>{escape(str(coupon_name))}</couponName>",
        f"<couponCaption>{escape(str(caption or ''))}</couponCaption>",
        f"<couponStartDate>{_fmt_dt(start)}</couponStartDate>",
        f"<couponEndDate>{_fmt_dt(end)}</couponEndDate>",
        f"<issueCount>{int(issue_count)}</issueCount>",
        f"<itemType>{item_type}</itemType>",
        f"<discountType>{conf['discountType']}</discountType>",
        f"<discountFactor>{int(discount_factor)}</discountFactor>",
        f"<memberAvailMaxCount>{int(member_max)}</memberAvailMaxCount>",
        # 以下3つは「条件なし」でも必須要素(bububa/rakuten-go実装より。欠けるとwrong format)
        "<purchaseHistoryCond><type>0</type></purchaseHistoryCond>",
        "<genderCond></genderCond>",
        "<birthmonthCond>0</birthmonthCond>",
        f"<combineFlag>{1 if combine else 0}</combineFlag>",
        f"<displayFlag>{1 if display else 0}</displayFlag>",
    ]
    if not all_items and manage_numbers:
        parts.append("<items>")
        for mn in manage_numbers:
            parts.append(f"<item><itemUrl>{escape(str(mn).strip())}</itemUrl></item>")
        parts.append("</items>")
    parts.append("</coupon></couponIssueRequest></request>")
    return "".join(parts)


def _parse_result(xml_text):
    """レスポンスXMLをパースし、エラーがあればCouponError、なければroot要素を返す。"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise CouponError(f"レスポンスの解析に失敗: {e} / {xml_text[:300]}") from e
    errors = []
    for err in root.iter("error"):
        code = (err.findtext("code") or "").strip()
        msg = (err.findtext("message") or "").strip()
        if (code or msg) and code != "N000":  # N000=正常
            errors.append(f"{code}: {msg}")
    if errors:
        raise CouponError("RMSクーポンAPIエラー → " + " / ".join(errors))
    return root


def issue(xml_body):
    """
    発行を実行し {"couponCode": ..., "getkey_url": ...} を返す。
    pcGetUrlが返ればそれを、無ければcouponCodeからgetCoupon URLを組み立てる。
    """
    resp_text = rms_api.post_xml("/es/1.0/coupon/issue", xml_body)
    root = _parse_result(resp_text)
    code = None
    get_url = None
    for c in root.iter("coupon"):
        code = (c.findtext("couponCode") or "").strip() or code
        get_url = (c.findtext("pcGetUrl") or "").strip() or get_url
    if not code:
        raise CouponError(f"クーポンコードが取得できませんでした: {resp_text[:500]}")
    if not get_url:
        get_url = f"https://coupon.rakuten.co.jp/getCoupon?getkey={code}"
    return {"couponCode": code, "getkey_url": get_url}


def delete(coupon_code):
    """クーポンを削除する。"""
    xml_body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<request><couponDeleteRequest><coupon>"
        f"<couponCode>{escape(str(coupon_code))}</couponCode>"
        "</coupon></couponDeleteRequest></request>")
    resp_text = rms_api.post_xml("/es/1.0/coupon/delete", xml_body)
    _parse_result(resp_text)


def manual_procedure_md(*, coupon_name, caption, start, end, discount_label,
                        discount_factor, issue_count, member_max,
                        manage_numbers, all_items):
    """API不可時の、RMS画面での手動発行手順書(Markdown)を返す。"""
    target = "全商品（受注クーポン）" if all_items else "、".join(manage_numbers or [])
    return f"""# RMSクーポン手動発行手順

APIでの自動発行ができなかったため、RMS管理画面から以下の内容でクーポンを発行してください。

## 手順
1. RMSメインメニュー → **2 販売促進** → **クーポン管理（RaCoupon）**
2. 「新規クーポン作成」を選択し、下記の値を入力
3. 発行後、クーポン一覧に表示される **獲得URL（getCoupon?getkey=◯◯）のgetkey部分** を
   イベントLP作成ページのクーポン欄に貼り付け

## 設定値
| 項目 | 値 |
|---|---|
| クーポン名 | {coupon_name} |
| 説明（キャプション） | {caption or "（なし）"} |
| 利用期間 | {start} 〜 {end} |
| 値引きタイプ | {discount_label} |
| 値引き額/率 | {discount_factor} |
| 発行枚数（利用可能総数） | {issue_count} |
| 1人あたり利用回数 | {member_max} |
| 対象商品 | {target} |
"""
