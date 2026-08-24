# -*- coding: utf-8 -*-
"""旧Yahoo価格キューを楽天の現在価格で復旧する一時運用。

Driveの yahoo_pending_prices.csv は、過去にYahoo APIが失敗した際の価格候補であり、
現在の正しい価格とは限らない。キューの親商品コードごとに楽天RMSから全SKUの現在価格を
読み、単一価格に確定できる商品だけをYahooへ反映する。処理結果はDriveへ毎バッチ保存し、
中断しても続きから再開できる。

SKU価格が割れている商品・楽天に無い商品・Yahooに無い商品は自動反映しない。
旧キュー自体はこの処理では削除・変更しない。
"""
import datetime
import json

import pandas as pd

from lib.event import rms_api
from lib.invoice import drive_master
from lib.yahoo_api import client as yahoo_client, items as yahoo_items

STATE_NAME = "yahoo_price_recovery_20260824.json"
AUDIT_NAME = "yahoo_price_recovery_20260824.csv"
BATCH_SIZE = 50
RETRYABLE_STATUSES = {"楽天取得失敗", "Yahoo反映予約失敗"}
_TRANSIENT_YAHOO_MARKERS = (
    "http 408", "http 429", "http 500", "http 502", "http 503", "http 504",
    "timeout", "timed out", "connection", "temporarily", "service unavailable",
    "rate limit", "一時", "タイムアウト", "接続", "ed-00006",
    "反映またはアップロード中",
)


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _codes(queue_df):
    """キューのcodeを小文字・重複なし・元順序維持で返す。"""
    if queue_df is None or queue_df.empty or "code" not in queue_df.columns:
        return []
    return list(dict.fromkeys(str(v).strip().lower() for v in queue_df["code"]
                              if str(v).strip()))


def new_state(queue_df):
    return {"version": 1, "started_at": _now(), "updated_at": _now(),
            "queue_total": len(_codes(queue_df)), "results": {}}


def load_state(folder_id):
    f = drive_master.find_file(STATE_NAME, folder_id)
    if not f:
        return None
    return json.loads(drive_master.download_bytes(f["id"]).decode("utf-8"))


def _queue_price_map(queue_df):
    out = {}
    if queue_df is None or queue_df.empty:
        return out
    for _, row in queue_df.iterrows():
        code = str(row.get("code", "")).strip().lower()
        if code:
            out[code] = row.get("price", "")
    return out


def audit_df(queue_df, state):
    prices = _queue_price_map(queue_df)
    rows = []
    for code in _codes(queue_df):
        r = (state.get("results") or {}).get(code, {})
        category = r.get("category_repair") or {}
        rows.append({"code": code, "旧キュー価格": prices.get(code, ""),
                     "楽天現在価格": r.get("rakuten_price", ""),
                     "YahooカテゴリID": category.get("category_id", ""),
                     "Yahooカテゴリ名": category.get("category_name", ""),
                     "カテゴリ推定根拠": category.get("reason", ""),
                     "状態": r.get("status", "未処理"),
                     "メッセージ": r.get("message", ""),
                     "処理日時": r.get("processed_at", "")})
    return pd.DataFrame(rows, columns=["code", "旧キュー価格", "楽天現在価格",
                                      "YahooカテゴリID", "Yahooカテゴリ名", "カテゴリ推定根拠",
                                      "状態", "メッセージ", "処理日時"])


def save_state(folder_id, queue_df, state):
    state["updated_at"] = _now()
    drive_master.upload_or_replace(
        json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8"),
        STATE_NAME, folder_id, mimetype="application/json")
    data = audit_df(queue_df, state).to_csv(index=False, lineterminator="\r\n").encode("utf-8-sig")
    drive_master.upload_or_replace(data, AUDIT_NAME, folder_id, mimetype="text/csv")


def fetch_rakuten_parent_price(parent):
    """楽天の商品管理番号から、Yahooに使える単一の現在価格を返す。

    返り値: (price, message)。全SKUが同額のときだけpriceを返す。
    """
    data = rms_api.get(f"/es/2.0/items/manage-numbers/{parent}")
    item = data.get("item", data)
    variants = item.get("variants") or {}
    if not isinstance(variants, dict) or not variants:
        return None, "楽天の商品・SKU情報を取得できません"
    prices = []
    for variant in variants.values():
        if not isinstance(variant, dict):
            continue
        price = None
        for key in ("standardPrice", "price", "salesPrice"):
            if variant.get(key) is not None:
                try:
                    price = int(float(variant[key]))
                    break
                except (TypeError, ValueError):
                    pass
        if price is not None and price > 0:
            prices.append(price)
    if not prices:
        return None, "楽天SKUの販売価格を取得できません"
    unique = sorted(set(prices))
    if len(unique) != 1:
        shown = "、".join(f"{p}円" for p in unique[:10])
        return None, f"楽天のSKU間で価格が異なるため自動反映対象外（{shown}）"
    return unique[0], ""


def remaining_codes(queue_df, state):
    done = set((state.get("results") or {}).keys())
    return [code for code in _codes(queue_df) if code not in done]


def summary(queue_df, state):
    results = state.get("results") or {}
    counts = {}
    for row in results.values():
        status = row.get("status", "")
        counts[status] = counts.get(status, 0) + 1
    return {"total": len(_codes(queue_df)), "processed": len(results),
            "remaining": len(remaining_codes(queue_df, state)), "counts": counts}


def is_retryable_result(row):
    """再実行で改善し得る失敗だけを返す。Yahooの入力値エラーは対象外。"""
    status = row.get("status")
    if status in RETRYABLE_STATUSES:
        return True
    if status != "Yahoo更新失敗":
        return False
    message = str(row.get("message") or "").lower()
    return any(marker in message for marker in _TRANSIENT_YAHOO_MARKERS)


def retryable_count(state):
    return sum(1 for row in (state.get("results") or {}).values()
               if is_retryable_result(row))


def reset_retryable(state):
    """一時的なAPI失敗だけを未処理へ戻す。商品なし・SKU価格差は戻さない。"""
    results = state.get("results") or {}
    retry = [code for code, row in results.items() if is_retryable_result(row)]
    for code in retry:
        results.pop(code, None)
    return len(retry)


def category_failure_codes(state):
    """it-02037相当のカテゴリ未設定エラーになった商品コード。"""
    return [code for code, row in (state.get("results") or {}).items()
            if row.get("status") == "Yahoo更新失敗"
            and "プロダクトカテゴリが設定されていない" in str(row.get("message") or "")]


def reset_category_failures(state):
    codes = category_failure_codes(state)
    for code in codes:
        state.get("results", {}).pop(code, None)
    return len(codes)


def _record(state, code, price, status, message="", category_detail=None):
    row = {
        "rakuten_price": price if price is not None else "",
        "status": status, "message": str(message or ""), "processed_at": _now()}
    if category_detail:
        row["category_repair"] = category_detail
    state.setdefault("results", {})[code] = row


def process_next(queue_df, state, limit=BATCH_SIZE, on_progress=None, category_plans=None):
    """未処理の次バッチを楽天取得→Yahoo更新する。旧キュー自体は変更しない。"""
    batch = remaining_codes(queue_df, state)[:int(limit)]
    if not batch:
        return state

    yahoo_prices = {}
    for done, code in enumerate(batch, start=1):
        try:
            price, message = fetch_rakuten_parent_price(code)
            if price is None:
                _record(state, code, None, "楽天対象外", message)
            else:
                yahoo_prices[code] = price
        except rms_api.RMSAuthError:
            raise
        except Exception as e:  # noqa: BLE001
            _record(state, code, None, "楽天取得失敗", str(e))
        if on_progress:
            on_progress(done, len(batch), f"楽天価格取得 {done}/{len(batch)}")

    if not yahoo_prices:
        return state

    try:
        yahoo_client.access_token()
        category_repairs = {}
        ok, errors, missing = yahoo_items.update_prices_checked(
            yahoo_prices, category_plans=category_plans,
            on_category_repair=lambda code, detail: category_repairs.__setitem__(code, detail))
        missing_set = set(missing)
        for code in missing:
            _record(state, code, yahoo_prices[code], "Yahoo商品なし",
                    "Yahoo API it-02002: 指定された商品は存在しません")
        candidates = [code for code in yahoo_prices if code not in missing_set]
        if errors:
            message = "／".join(errors[:5])
            repair_publish_errors = yahoo_items.reserve_publish() if category_repairs else []
            for code in candidates:
                if code in category_repairs:
                    detail = category_repairs[code]
                    if repair_publish_errors:
                        _record(state, code, yahoo_prices[code], "Yahoo反映予約失敗",
                                "カテゴリ・価格更新OK、反映予約失敗: "
                                + "／".join(repair_publish_errors[:5]), category_detail=detail)
                    else:
                        _record(state, code, yahoo_prices[code], "Yahoo反映成功",
                                f"カテゴリ{detail['category_id']}を自動設定し楽天現在価格で更新",
                                category_detail=detail)
                else:
                    _record(state, code, yahoo_prices[code], "Yahoo更新失敗", message)
            return state
        if ok != len(candidates):
            message = f"Yahoo成功件数が不一致（予定{len(candidates)}／応答{ok}）"
            for code in candidates:
                _record(state, code, yahoo_prices[code], "Yahoo更新失敗", message)
            return state
        if candidates:
            publish_errors = yahoo_items.reserve_publish()
            if publish_errors:
                message = "更新OK・反映予約失敗: " + "／".join(publish_errors[:5])
                for code in candidates:
                    _record(state, code, yahoo_prices[code], "Yahoo反映予約失敗", message)
            else:
                for code in candidates:
                    detail = category_repairs.get(code)
                    message = "楽天現在価格で更新"
                    if detail:
                        message += (f"・Yahooカテゴリ{detail['category_id']}"
                                    f"（{detail.get('category_name') or '名称不明'}）を自動設定")
                    _record(state, code, yahoo_prices[code], "Yahoo反映成功", message,
                            category_detail=detail)
    except Exception as e:  # noqa: BLE001
        for code, price in yahoo_prices.items():
            if code not in state.get("results", {}):
                _record(state, code, price, "Yahoo更新失敗", str(e))
    return state
