# -*- coding: utf-8 -*-
"""
反映できなかった処理の控え（失敗分をため込んで、あとから誰でも再実行できるようにする）。

【なぜ必要か】
入荷登録も価格改定も、失敗した分を「その画面（セッション）」にしか持っていなかった。
画面を閉じる・再読み込みする・再認可のためにログイン画面へ行って戻る、のどれでも消える。
消えると、NEと楽天だけ新しい価格で Yahoo だけ旧いまま、という食い違いが残り、
しかも「何が残っているか」を後から知る手段が無い。

  実例（2026-10-07）: 入荷登録で Yahoo の認証が切れていて販売価格1件が失敗。
  画面は「再認可してから『失敗した処理だけ再実行』を押してください」と案内したが、
  再認可して戻るとボタンごと消えていた。

そこで失敗分を Drive に控え、どの画面・どの人からでも再実行できるようにする。
人が押すまで反映されない経路なので、滞留は batch/pending_retry_watch.py が毎日見張る。

【決まり】
- 追記のみ。済んだものも取り下げたものも消さず、状態と履歴を残す
- 読めなかったときは「0件」にしない（例外を投げる）。空で上書きすると控えを失う
- 取り下げには理由が要る

保存先: Drive（PRODUCT_MASTER_FOLDER_ID）の pending_retries.json
1件の形:
  {id, source, label, created_at, status(open/done/withdrawn),
   original_tasks（最初に失敗した分。書き換えない）, tasks（いま残っている分）,
   attempts: [{at, remaining, results}], closed_at, note}
"""
import datetime
import json
import uuid

STORE_NAME = "pending_retries.json"
OPEN, DONE, WITHDRAWN = "open", "done", "withdrawn"

# どの画面の処理か。再実行はその画面の実行関数（runner.execute / apply.execute）で行う。
SOURCES = {"receiving": "入荷登録", "pricing": "価格改定"}

# 何日残ったら異常として扱うか。再実行はボタン1つで済み、残っている間は
# モール間で価格・送料が食い違ったまま売れ続けるので、翌日の点検で必ず赤にする。
STALE_DAYS = 1

_TTL = 60                     # 画面表示用の読込をセッションに覚えておく秒数（Drive往復の削減）
_CK = "_pending_retry_ck"


class StoreError(RuntimeError):
    """控えを読めない・壊れている。0件として扱ってはいけない。"""


def _folder():
    from lib import master_store
    return master_store.folder_id()


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _stamp(now=None):
    return (now or _now()).isoformat(timespec="seconds")


def _read_all():
    """全件を読む。ファイルが無ければ空、あるのに読めなければ StoreError。"""
    from lib.invoice import drive_master
    f = drive_master.find_file(STORE_NAME, _folder())
    if not f:
        return []
    try:
        data = json.loads(drive_master.download_bytes(f["id"]).decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise StoreError(f"{STORE_NAME} を読めません: {e}") from e
    if not isinstance(data, list):
        raise StoreError(f"{STORE_NAME} の形式が想定と違います（配列ではありません）")
    return data


def _write_all(entries):
    from lib.invoice import drive_master
    drive_master.upload_or_replace(
        json.dumps(entries, ensure_ascii=False, indent=2, default=str).encode("utf-8"),
        STORE_NAME, _folder(), mimetype="application/json")
    _drop_cache()


def _drop_cache():
    try:
        import streamlit as st
        st.session_state.pop(_CK, None)
    except Exception:  # noqa: BLE001
        pass


def load(use_cache=False):
    """全件。use_cache=True は画面表示用（毎rerunでDriveを読まない）。"""
    if not use_cache:
        return _read_all()
    import time
    import streamlit as st
    ent = st.session_state.get(_CK)
    if ent and (time.time() - ent["at"]) < _TTL:
        return ent["entries"]
    entries = _read_all()
    st.session_state[_CK] = {"at": time.time(), "entries": entries}
    return entries


# ---------------------------------------------------------------- 純関数（テスト対象）

def clean_tasks(tasks):
    """中身のある項目だけ残す（{"ne_price": [], "yahoo_price": {...}} → yahoo_priceのみ）。"""
    return {k: v for k, v in (tasks or {}).items() if v}


def count_units(tasks):
    """残っている件数（表示用）。リストは要素数、価格の対応表は商品数で数える。"""
    n = 0
    for key, value in clean_tasks(tasks).items():
        if key == "yahoo_delivery":
            n += len((value or {}).get("rows") or [])
        elif isinstance(value, (list, dict)):
            n += len(value)
        else:
            n += 1
    return n


_KEY_LABEL = {
    "ne_main": "NE ロケーション・項目1",
    "ne_price": "NE 売価",
    "rakuten_delivery": "楽天 配送方法セット",
    "rakuten_price": "楽天 販売価格",
    "yahoo_price": "Yahoo 販売価格",
    "yahoo_delivery": "Yahoo 配送グループ",
}


def describe(tasks, limit=5):
    """何が残っているかを1行ずつ（再実行する人が中身を見て判断できるように）。"""
    lines = []
    for key, value in clean_tasks(tasks).items():
        name = _KEY_LABEL.get(key, key)
        if key == "yahoo_price":
            items = [f"{code}→{price}円" for code, price in value.items()]
        elif key == "yahoo_delivery":
            items = [f"{r.get('code')}→{r.get('便種')}" for r in value.get("rows") or []]
        elif key == "ne_price":
            items = [f"{r.get('syohin_code')}→{r.get('baika_tnk')}円" for r in value]
        elif key == "ne_main":
            items = [f"{r.get('syohin_code')}→{r.get('location')}" for r in value]
        elif key == "rakuten_price":
            items = [f"{r.get('商品管理番号')}（{len(r.get('sku_prices') or {})}SKU）"
                     for r in value]
        elif key == "rakuten_delivery":
            items = [f"{r.get('商品管理番号')}→{r.get('新便種')}" for r in value]
        else:
            items = [str(value)]
        shown = "、".join(items[:limit]) + (f" ほか{len(items) - limit}件"
                                            if len(items) > limit else "")
        lines.append(f"{name}（{len(items)}件）: {shown}")
    return lines


def new_entry(source, tasks, label="", now=None):
    tasks = clean_tasks(tasks)
    # JSONに通して、保存後に読み戻したときと同じ形にそろえる（数値のキー等の差をなくす）
    tasks = json.loads(json.dumps(tasks, ensure_ascii=False, default=str))
    stamp = _stamp(now)
    return {"id": f"{stamp[:19].replace('-', '').replace(':', '').replace('T', '-')}"
                  f"-{uuid.uuid4().hex[:6]}",
            "source": source, "label": str(label or ""), "created_at": stamp,
            "status": OPEN, "original_tasks": tasks, "tasks": tasks,
            "attempts": [], "closed_at": "", "note": ""}


def apply_attempt(entry, still_failed, results=None, now=None):
    """再実行の結果を1件に反映する（履歴は足すだけ）。全部通ったら done。"""
    remaining = json.loads(json.dumps(clean_tasks(still_failed),
                                      ensure_ascii=False, default=str))
    stamp = _stamp(now)
    entry.setdefault("attempts", []).append({
        "at": stamp, "remaining": count_units(remaining),
        "results": [{k: str(r.get(k, "")) for k in ("ステップ", "対象", "状態", "メッセージ")}
                    for r in (results or [])]})
    entry["tasks"] = remaining
    if not remaining:
        entry["status"], entry["closed_at"] = DONE, stamp
    return entry


def open_of(entries, source=None):
    return [e for e in entries
            if e.get("status") == OPEN and (source is None or e.get("source") == source)]


def age_days(entry, now=None):
    """控えてから何日経ったか。日時が読めなければ None（0日に丸めない）。"""
    try:
        created = datetime.datetime.fromisoformat(str(entry.get("created_at", "")))
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=datetime.timezone.utc)
    return int(((now or _now()) - created).total_seconds() // 86400)


def created_jst(entry):
    """画面表示用の日時（日本時間）。読めなければ元の文字列のまま。"""
    try:
        created = datetime.datetime.fromisoformat(str(entry.get("created_at", "")))
    except ValueError:
        return str(entry.get("created_at", ""))
    if created.tzinfo is None:
        created = created.replace(tzinfo=datetime.timezone.utc)
    jst = created.astimezone(datetime.timezone(datetime.timedelta(hours=9)))
    return jst.strftime("%m/%d %H:%M")


# ---------------------------------------------------------------- 読み書き

def add(source, tasks, label=""):
    """失敗分を控える。控えるものが無ければ None、あれば id を返す。"""
    if not clean_tasks(tasks):
        return None
    entries = _read_all()
    entry = new_entry(source, tasks, label)
    entries.append(entry)
    _write_all(entries)
    return entry["id"]


def record_attempt(entry_id, still_failed, results=None):
    """再実行の結果を書き戻す。見つからなければ StoreError（黙って捨てない）。"""
    entries = _read_all()
    for entry in entries:
        if entry.get("id") == entry_id:
            apply_attempt(entry, still_failed, results)
            _write_all(entries)
            return entry
    raise StoreError(f"控えが見つかりません: {entry_id}")


def withdraw(entry_id, reason):
    """再実行しないと決めたものを取り下げる（消さずに状態と理由を残す）。"""
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("取り下げる理由を入力してください。")
    entries = _read_all()
    for entry in entries:
        if entry.get("id") == entry_id:
            entry["status"], entry["closed_at"], entry["note"] = WITHDRAWN, _stamp(), reason
            _write_all(entries)
            return entry
    raise StoreError(f"控えが見つかりません: {entry_id}")
