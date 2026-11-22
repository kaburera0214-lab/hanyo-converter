# -*- coding: utf-8 -*-
"""
NE APIの使用量カウント（月間の呼び出し回数・通信量）とアラート。

NEは月間の呼び出し回数（と通信データ量）で課金される（〜1000回/月・3GBは無料）。
勝手に課金されるのを防ぐため、全てのNE API呼び出し（lib/ne_api/client.call）を横断で
カウントし、無料枠に近づいたら画面で警告する。入荷登録など個別機能に依存しない。

Streamlit Cloudはセッションが揮発するため、カウンタは Drive の ne_api_usage.json に
永続化する。各呼び出しはまずセッションに貯め、数回ごと（と表示時）にDriveへ反映する
（Driveの読み書き回数を抑える）。小規模チーム前提で厳密な同時実行制御はしない（近似値）。

2026-11-22〜：呼び出し元ごと・日ごとの内訳も残す（by / days）。無料枠は全ツールで分け合う1つの枠で、
合計しか無いと「誰が使ったか」が分からず、80%の通知が来てから掘り起こすことになった。
割り振りの正本は lib/ne_api/budget_plan.py。通知は80%・100%に加えて、
「この月のペースだと枠を超える」「あるツールが割り振りのペースを大きく超えた」で早めに出す。
"""
import calendar
import datetime
import json
import os
import sys

import streamlit as st

from lib.invoice import drive_master
from lib.ne_api import budget_plan

USAGE_NAME = "ne_api_usage.json"
FREE_LIMIT = budget_plan.FREE_LIMIT        # 月間無料枠（NE料金表・呼び出し回数）
WARN_RATIO = 0.8         # この割合を超えたら警告
FLUSH_EVERY = 5          # セッションにこの回数貯まったらDriveへ反映
_PENDING = "_ne_usage_pending"
ONEOFF_MARK = "#単発"
PACE_FROM_DAY = 5        # 月の5日目からペースで見る（月初は1日の多寡で外れやすい）
TOOL_OVER_RATIO = 1.5    # 割り振りのペースの1.5倍を超えたツールを知らせる
TOOL_OVER_MIN = 20       # ただし超過が20回未満なら知らせない（小さい数の揺れで鳴らさない）

_caller = {"name": "", "oneoff": None}


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


# ---------- 呼び出し元 ----------

def set_caller(name, oneoff=None):
    """この先の呼び出しを name の分として数える。oneoff=True で単発（試験・依頼・期間限定）。
    None なら割り振りの正本（budget_plan）の ONEOFF と、手で起動したか（workflow_dispatch）で決まる。"""
    _caller["name"] = str(name or "")
    _caller["oneoff"] = oneoff


def _detect_caller():
    """set_caller が無ければ、呼び出しの元の pages/ ・ batch/ のファイル名を名前にする。"""
    if _caller["name"]:
        return _caller["name"]
    try:
        f = sys._getframe(1)
        while f is not None:
            path = f.f_code.co_filename.replace("\\", "/")
            for part in ("/pages/", "/batch/"):
                if part in path:
                    return os.path.splitext(path.rsplit("/", 1)[-1])[0]
            f = f.f_back
    except Exception:  # noqa: BLE001
        pass
    return "不明"


def _caller_key():
    name = _detect_caller()
    oneoff = _caller["oneoff"]
    if oneoff is None:
        # 定期バッチを手で起動した回（GitHub Actions の workflow_dispatch）は、ルーティンの見込みから外す
        oneoff = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    return name + (ONEOFF_MARK if oneoff else "")


def split_key(key):
    """記録のキー → (呼び出し元, 単発か)。"""
    k = str(key)
    if k.endswith(ONEOFF_MARK):
        return k[:-len(ONEOFF_MARK)], True
    return k, False


# ---------- 記録 ----------

def record(calls=1, nbytes=0):
    """NE API呼び出しを1回ぶん記録する（client.callから呼ぶ）。失敗しても本処理は妨げない。"""
    try:
        p = st.session_state.setdefault(_PENDING, {"calls": 0, "bytes": 0, "by": {}})
        p.setdefault("by", {})
        p["calls"] += calls
        p["bytes"] += nbytes
        key = _caller_key()
        p["by"][key] = p["by"].get(key, 0) + calls
        if p["calls"] >= FLUSH_EVERY:
            flush()
    except Exception:  # noqa: BLE001
        pass


def merge(data, pending, now=None):
    """月のカウンタ data に、貯めた分 pending を足す（純関数・テスト対象）。月替わりは先月分を prev_* へ。"""
    now = now or datetime.datetime.now()
    month = now.strftime("%Y-%m")
    data = dict(data or {})
    if data.get("month") != month:
        data = {"month": month, "calls": 0, "bytes": 0,
                "prev_month": data.get("month"), "prev_calls": data.get("calls", 0),
                "prev_by": data.get("by", {})}
    data["calls"] = int(data.get("calls", 0)) + int(pending.get("calls", 0))
    data["bytes"] = int(data.get("bytes", 0)) + int(pending.get("bytes", 0))
    by = dict(data.get("by") or {})
    days = dict(data.get("days") or {})
    day = dict(days.get(now.strftime("%Y-%m-%d")) or {})
    for k, n in (pending.get("by") or {}).items():
        by[k] = by.get(k, 0) + n
        day[k] = day.get(k, 0) + n
    if pending.get("by"):
        days[now.strftime("%Y-%m-%d")] = day
    data["by"], data["days"] = by, days
    data["updated"] = now.isoformat(timespec="seconds")
    return data


def flush():
    """セッションに貯めた回数・通信量をDriveの月間カウンタへ加算する。"""
    p = st.session_state.get(_PENDING)
    if not p or (p["calls"] == 0 and p["bytes"] == 0):
        return
    data = merge(_read(), p)
    _maybe_alert(data)   # 閾値を初めて超えたらChatworkタスクを作成（月×レベルで1回）
    _write(data)
    st.session_state[_PENDING] = {"calls": 0, "bytes": 0, "by": {}}


# ---------- ペース（純関数・テスト対象） ----------

def pace_report(data, now=None):
    """今月の使い方を割り振り（budget_plan）と比べる。
    返り値: {day, days_in_month, routine, oneoff, unknown, projected, tools:[...], unknown_callers:[...]}
    projected＝ルーティンを今のペースで月末まで延ばした数＋単発と未登録の実績（単発は延ばさない）。"""
    now = now or datetime.datetime.now()
    day = now.day
    dim = calendar.monthrange(now.year, now.month)[1]
    routine = oneoff = unknown = 0
    per_tool, unknown_callers = {}, {}
    for key, n in (data.get("by") or {}).items():
        caller, is_oneoff = split_key(key)
        kind = budget_plan.classify(caller, is_oneoff)
        if kind == "routine":
            routine += n
            t = budget_plan.tool_of(caller)
            per_tool[t] = per_tool.get(t, 0) + n
        elif kind == "oneoff":
            oneoff += n
        else:
            unknown += n
            unknown_callers[caller] = unknown_callers.get(caller, 0) + n
    # 内訳を取り始める前の分（合計だけある）は「内訳なし」として未登録に入れる（黙って捨てない）
    before = int(data.get("calls", 0)) - (routine + oneoff + unknown)
    if before > 0:
        unknown += before
        unknown_callers["（内訳を取る前）"] = before
    projected = round(routine / day * dim) + oneoff + unknown if day else routine + oneoff + unknown
    tools = []
    for t, plan in budget_plan.ROUTINE.items():
        to_date = plan["monthly"] * day / dim
        actual = per_tool.get(t, 0)
        tools.append({"tool": t, "actual": actual, "plan_to_date": round(to_date), "plan_month": plan["monthly"],
                      "over": actual > to_date * TOOL_OVER_RATIO and actual - to_date >= TOOL_OVER_MIN})
    return {"day": day, "days_in_month": dim, "routine": routine, "oneoff": oneoff, "unknown": unknown,
            "projected": projected, "tools": tools,
            "unknown_callers": sorted(unknown_callers.items(), key=lambda x: -x[1])}


def pace_alerts(data, now=None):
    """今回新しく出す通知の種類と本文（純関数・テスト対象）。出したものは data["alerted_pace"] に記録して月内は再送しない。
    返り値: [(印, 本文)]"""
    now = now or datetime.datetime.now()
    rep = pace_report(data, now)
    sent = set(data.get("alerted_pace") or [])
    out = []
    if rep["day"] >= PACE_FROM_DAY and rep["projected"] > FREE_LIMIT and "projected" not in sent:
        lines = ["・{tool}：{actual}回（割り振りでは今日までに{plan_to_date}回・月{plan_month}回）".format(**t)
                 for t in sorted(rep["tools"], key=lambda t: -t["actual"]) if t["actual"]]
        lines.append("・単発（試験・依頼）：{}回（枠 {}回）".format(rep["oneoff"], budget_plan.ONEOFF_RESERVE))
        if rep["unknown"]:
            lines.append("・未登録：{}回（{}）".format(rep["unknown"], "、".join(
                "{} {}回".format(c, n) for c, n in rep["unknown_callers"][:5])))
        out.append(("projected",
                    "[info][title]🟡【犬飼対応】NE API このペースだと月{}回で無料枠を超えます[/title]"
                    "{}日目で{}回。ルーティンを今のペースで月末まで延ばした見込みです（予測）。\n{}\n"
                    "割り振りの正本: hanyo-converter lib/ne_api/budget_plan.py[/info]".format(
                        rep["projected"], rep["day"], data.get("calls", 0), "\n".join(lines))))
    for t in rep["tools"]:
        mark = "tool:" + t["tool"]
        if t["over"] and mark not in sent:
            out.append((mark,
                        "[info][title]🟡【犬飼対応】NE API {}が割り振りより多く使っています[/title]"
                        "{}日目で{}回（割り振りでは今日までに{}回・月{}回）。作りか使い方が変わっていないか確かめてください。"
                        "割り振りを変えるなら lib/ne_api/budget_plan.py を直します。[/info]".format(
                            t["tool"], rep["day"], t["actual"], t["plan_to_date"], t["plan_month"])))
    return out


def _maybe_alert(data):
    """使用量が閾値を初めて超えたときだけ外部通知（Chatworkタスク）を送る。
    dataの alerted に到達済みレベル(warn/over)を記録し、月内は同レベルの再送を防ぐ。
    ペース（pace_alerts）の通知は alerted_pace に記録する。"""
    try:
        from lib.notify import chatwork
        if not chatwork.is_configured():
            return   # 通知先未設定 → 既読にしない（後で設定したら通知できるように）
    except Exception:  # noqa: BLE001
        return
    for mark, body in pace_alerts(data):
        try:
            if chatwork.create_task(body, limit_days=3, audience=chatwork.ADMIN):
                data["alerted_pace"] = list(data.get("alerted_pace") or []) + [mark]
        except Exception:  # noqa: BLE001
            pass
    calls = int(data.get("calls", 0))
    level = _level(calls, FREE_LIMIT, WARN_RATIO)
    if level == "ok":
        return
    already = data.get("alerted", "")
    if already == "over" or (already == "warn" and level == "warn"):
        return   # 既に通知済み
    try:
        if level == "over":
            body = (f"[info][title]🔴【犬飼対応】NE API 無料枠を超過[/title]"
                    f"今月のNE API呼び出しが無料枠(1000回)を超えました（{calls}回）。"
                    f"以降は課金対象です。マスタ自動取得・入荷登録の頻度を確認してください。[/info]")
        else:
            body = (f"[info][title]🟡【犬飼対応】NE API 無料枠の80%超過[/title]"
                    f"今月のNE API呼び出しが{calls}/1000回になりました。"
                    f"残り{FREE_LIMIT - calls}回で課金に入ります。[/info]")
        # 無料枠は「実行頻度の見直し」でしか対処できず現場スタッフには直せないため管理者へ
        if chatwork.create_task(body, limit_days=3, audience=chatwork.ADMIN):
            data["alerted"] = level
    except Exception:  # noqa: BLE001
        pass


def set_baseline(calls, nbytes):
    """今月のカウンタを指定値で初期化する（計測開始前の過去分を予測値でセットする用）。
    以降の実測はこの値に加算され、翌月は自動で0にリセットされる。"""
    data = {"month": _month(), "calls": int(calls), "bytes": int(nbytes),
            "updated": datetime.datetime.now().isoformat(timespec="seconds"),
            "note": "baseline(予測値含む)"}
    _write(data)
    st.session_state[_PENDING] = {"calls": 0, "bytes": 0, "by": {}}
    return data


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
            "level": _level(calls, FREE_LIMIT, WARN_RATIO),
            "pace": pace_report(data) if data.get("month") == _month() else None}


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
    p = s.get("pace")
    if p and not compact:
        rows = [{"呼び出し元": t["tool"], "今月": t["actual"], "割り振り（今日まで）": t["plan_to_date"],
                 "割り振り（月）": t["plan_month"]} for t in p["tools"]]
        rows.append({"呼び出し元": "単発（試験・依頼）", "今月": p["oneoff"], "割り振り（今日まで）": None,
                     "割り振り（月）": budget_plan.ONEOFF_RESERVE})
        if p["unknown"]:
            rows.append({"呼び出し元": "未登録・内訳なし", "今月": p["unknown"], "割り振り（今日まで）": None,
                         "割り振り（月）": None})
        st.caption("内訳（{}日目・このペースだと月 約{}回＝予測）".format(p["day"], p["projected"]))
        st.dataframe(rows, hide_index=True, use_container_width=True)
