# -*- coding: utf-8 -*-
"""
NE APIの使用量カウント（月間の呼び出し回数・通信量）とアラート。

NEは月間の呼び出し回数（と通信データ量）で課金される（〜1000回/月・3GBは無料）。
勝手に課金されるのを防ぐため、全てのNE API呼び出し（lib/ne_api/client.call）を横断で
カウントし、無料枠に近づいたら画面で警告する。入荷登録など個別機能に依存しない。

Streamlit Cloudはセッションが揮発するため、カウンタは Drive の ne_api_usage.json に
永続化する。各呼び出しはまずセッションに貯め、数回ごと（と表示時）にDriveへ反映する
（Driveの読み書き回数を抑える）。小規模チーム前提で厳密な同時実行制御はしない（近似値）。
"""
import datetime
import json

import streamlit as st

from lib.invoice import drive_master

USAGE_NAME = "ne_api_usage.json"
FREE_LIMIT = 1000        # 月間無料枠（NE料金表・呼び出し回数）
WARN_RATIO = 0.8         # この割合を超えたら警告
FLUSH_EVERY = 5          # セッションにこの回数貯まったらDriveへ反映
_PENDING = "_ne_usage_pending"


def _folder():
    from lib import master_store
    return master_store.folder_id()


def _month():
    return datetime.datetime.now().strftime("%Y-%m")


def _read():
    try:
        f = drive_master.find_file(USAGE_NAME, _folder())
        if f:
            return json.loads(drive_master.download_bytes(f["id"]).decode("utf-8"))
    except Exception:  # noqa: BLE001
        pass
    return {}


def _write(data):
    try:
        drive_master.upload_or_replace(
            json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
            USAGE_NAME, _folder(), mimetype="application/json")
    except Exception:  # noqa: BLE001
        pass


def record(calls=1, nbytes=0):
    """NE API呼び出しを1回ぶん記録する（client.callから呼ぶ）。失敗しても本処理は妨げない。"""
    try:
        p = st.session_state.setdefault(_PENDING, {"calls": 0, "bytes": 0})
        p["calls"] += calls
        p["bytes"] += nbytes
        if p["calls"] >= FLUSH_EVERY:
            flush()
    except Exception:  # noqa: BLE001
        pass


def flush():
    """セッションに貯めた回数・通信量をDriveの月間カウンタへ加算する。"""
    p = st.session_state.get(_PENDING)
    if not p or (p["calls"] == 0 and p["bytes"] == 0):
        return
    data = _read()
    month = _month()
    if data.get("month") != month:   # 月替わり: 先月分をprevへ退避してリセット
        data = {"month": month, "calls": 0, "bytes": 0,
                "prev_month": data.get("month"), "prev_calls": data.get("calls", 0)}
    data["calls"] = int(data.get("calls", 0)) + p["calls"]
    data["bytes"] = int(data.get("bytes", 0)) + p["bytes"]
    data["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
    _write(data)
    st.session_state[_PENDING] = {"calls": 0, "bytes": 0}


def _level(calls, limit, warn_ratio):
    """使用量レベル（純関数・テスト対象）: over / warn / ok。"""
    if limit and calls >= limit:
        return "over"
    if limit and calls >= limit * warn_ratio:
        return "warn"
    return "ok"


def status():
    """現在の使用状況（pending反映済み）を返す。"""
    flush()
    data = _read()
    calls = int(data.get("calls", 0)) if data.get("month") == _month() else 0
    nbytes = int(data.get("bytes", 0)) if data.get("month") == _month() else 0
    return {"month": _month(), "calls": calls, "bytes": nbytes, "limit": FREE_LIMIT,
            "ratio": (calls / FREE_LIMIT) if FREE_LIMIT else 0,
            "level": _level(calls, FREE_LIMIT, WARN_RATIO)}


def render(compact=False):
    """使用量ウィジェット（どのページ・app.pyからでも呼べる）。メトリクス＋進捗＋アラート。"""
    try:
        s = status()
    except Exception as e:  # noqa: BLE001
        st.caption(f"（NE API使用量を取得できません: {e}）")
        return
    mb = s["bytes"] / (1024 * 1024)
    if not compact:
        c1, c2, c3 = st.columns(3)
        c1.metric(f"NE API 呼び出し（{s['month']}）", f"{s['calls']:,} 回", f"上限 {s['limit']:,}回")
        c2.metric("推定通信量（今月）", f"{mb:,.1f} MB", "無料枠 3GB")
        c3.metric("無料枠の残り", f"{max(s['limit'] - s['calls'], 0):,} 回")
    st.progress(min(s["ratio"], 1.0),
                text=f"NE API 呼び出し {s['calls']:,}/{s['limit']:,}回（{s['ratio']*100:.0f}%）")
    if s["level"] == "over":
        st.error(f"🔴 今月のNE API無料枠（{s['limit']:,}回）を超えました。以降は課金対象です。"
                 "マスタ自動取得や更新の頻度を見直してください。")
    elif s["level"] == "warn":
        st.warning(f"🟡 今月のNE API呼び出しが無料枠の{int(WARN_RATIO*100)}%を超えました"
                   f"（{s['calls']:,}/{s['limit']:,}回）。残り{s['limit']-s['calls']:,}回で課金に入ります。")
