# -*- coding: utf-8 -*-
"""
質問・回答管理のKPI計算。

スプレッドシート時代にあった「集計」シート（日次・月次の件数）が移行で失われ、
質問が増えているのか減っているのかを体感でしか判断できなくなっていた。
ここでその層を戻す。画面はこのモジュールの戻り値を並べるだけにして、
数え方をテストできる場所に集める。

数え方の約束:
  - 件数は営業日で割る（月の営業日数がぶれるため。土日と日本の祝日を除く）
  - 追加質問（ラリー）は「発生日」で数える。質問の起票月で数えると、
    月末に立った質問はまだ追加質問が来ていないぶん低く出る（打ち切り）
  - ラリー率だけは起票月のコホートで見る。当月は打ち切りが効くので参考値
"""
import calendar
import datetime
import functools

from lib.qa.history import JST, count_action, iter_entries

# パピーが動く番／インハナさんが動く番
WAITING_PUPPY = ("未回答", "ドラフト生成済", "再質問")
WAITING_INHANA = ("回答済", "編集中")


@functools.lru_cache(maxsize=1024)
def is_holiday(d):
    """日本の祝日か（jpholiday未導入なら常にFalse）。

    回答管理ページは60秒ごとに自動リロードされ、そのたびに半年ぶんの日付を
    走査するのでキャッシュしておく。
    """
    try:
        import jpholiday
        return bool(jpholiday.is_holiday(d))
    except Exception:  # noqa: BLE001 - 依存が無くても件数表示は続ける
        return False


def business_days(year, month, until=None):
    """その月の営業日数（土日・祝日を除く）。until を渡すとその日までで打ち切る。"""
    d = datetime.date(year, month, 1)
    last = datetime.date(year, month, calendar.monthrange(year, month)[1])
    if until is not None and until < last:
        last = until
    if last < d:
        return 0
    count = 0
    while d <= last:
        if d.weekday() < 5 and not is_holiday(d):
            count += 1
        d += datetime.timedelta(days=1)
    return count


def parse_dt(value):
    """Notionの日時文字列をJSTのdatetimeにする。読めなければ None。"""
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    return dt.astimezone(JST)


def month_range(now, months):
    """新しい順に (year, month) を months 個返す。"""
    out = []
    y, m = now.year, now.month
    for _ in range(months):
        out.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


def _per_day(count, days):
    return round(count / days, 2) if days else 0.0


def monthly(questions, months=6, now=None):
    """月次の指標を新しい順に返す。

    各要素:
      年月 / 営業日 / 質問数 / 質問数_営業日 / 追加質問数 / 追加質問数_営業日
      / ラリー率 / 進行中  （ラリー率は起票月コホート・%）
    """
    now = now or datetime.datetime.now(JST)
    today = now.date()

    posted = {}      # (y, m) -> 質問数
    followups = {}   # (y, m) -> 追加質問の発生数
    cohort = {}      # (y, m) -> [追加質問があったか, ...]
    tracked = set()  # 編集履歴を持つ質問があった月＝ラリーを数えられる月

    for q in questions:
        history = q.get("編集履歴")
        at = parse_dt(q.get("質問日時"))
        if at is not None:
            key = (at.year, at.month)
            posted[key] = posted.get(key, 0) + 1
            if (history or "").strip():
                tracked.add(key)
                cohort.setdefault(key, []).append(count_action(history, "追加質問") > 0)
        for entry_at, _actor, action in iter_entries(history):
            if entry_at is None:
                continue
            k = (entry_at.year, entry_at.month)
            tracked.add(k)
            if action == "追加質問":
                followups[k] = followups.get(k, 0) + 1

    rows = []
    for (y, m) in month_range(now, months):
        until = today if (y, m) == (today.year, today.month) else None
        days = business_days(y, m, until=until)
        seen = cohort.get((y, m), [])
        # 移行前（スプレッドシートから取り込んだ分）は編集履歴を持たないので、
        # ラリーは「0件」ではなく「記録なし」。0と出すと減った/増えたを誤読させる。
        has_record = (y, m) in tracked
        rows.append({
            "年月": f"{y}-{m:02d}",
            "営業日": days,
            "質問数": posted.get((y, m), 0),
            "質問数_営業日": _per_day(posted.get((y, m), 0), days),
            "追加質問数": followups.get((y, m), 0) if has_record else None,
            "追加質問数_営業日": _per_day(followups.get((y, m), 0), days) if has_record else None,
            "ラリー率": round(100 * sum(seen) / len(seen), 1) if seen else (0.0 if has_record else None),
            "記録あり": has_record,
            "進行中": (y, m) == (today.year, today.month),
        })
    return rows


def stalled(questions, now=None):
    """止まっている質問を、待っている側ごとに経過日数の長い順で返す。"""
    now = now or datetime.datetime.now(JST)
    puppy, inhana = [], []
    for q in questions:
        status = q.get("ステータス") or "未回答"
        if status == "完了":
            continue
        at = parse_dt(q.get("質問日時"))
        item = {
            "番号": q.get("番号"),
            "タイトル": q.get("タイトル") or q.get("質問タイトル") or "",
            "ステータス": status,
            "質問日時": at,
            "経過日数": (now - at).days if at else None,
        }
        if status in WAITING_PUPPY:
            puppy.append(item)
        elif status in WAITING_INHANA:
            inhana.append(item)

    def key(x):
        return -(x["経過日数"] if x["経過日数"] is not None else -1)

    puppy.sort(key=key)
    inhana.sort(key=key)
    return {"パピー待ち": puppy, "インハナ待ち": inhana}


def summary(questions, now=None):
    """画面上部に出す当月サマリー（前月との差つき）。"""
    now = now or datetime.datetime.now(JST)
    rows = monthly(questions, months=2, now=now)
    this_month, last_month = rows[0], rows[1]
    waits = stalled(questions, now=now)
    all_waiting = waits["パピー待ち"] + waits["インハナ待ち"]
    oldest = max((w["経過日数"] for w in all_waiting if w["経過日数"] is not None), default=0)
    return {
        "当月": this_month,
        "前月": last_month,
        "質問_前月差": round(this_month["質問数_営業日"] - last_month["質問数_営業日"], 2),
        "追加質問_前月差": (
            round(this_month["追加質問数_営業日"] - last_month["追加質問数_営業日"], 2)
            if this_month["記録あり"] and last_month["記録あり"] else None
        ),
        "パピー待ち": len(waits["パピー待ち"]),
        "インハナ待ち": len(waits["インハナ待ち"]),
        "最長滞留日数": oldest,
        "滞留": waits,
    }
