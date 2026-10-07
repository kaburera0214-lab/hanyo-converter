# -*- coding: utf-8 -*-
"""
「⏳ 反映できていない処理」の表示と再実行（入荷登録・価格改定で共用）。

控えの正本は lib/pending_retry.py。ここは画面だけ。
どの画面・どの人が開いても同じ控えが見え、同じボタンで再実行できる。
"""
import pandas as pd
import streamlit as st

from lib import pending_retry as pr


def remember(source, failed, label=""):
    """失敗分を控える。返り値: (id または None, エラー文または "")。

    控えに失敗しても、その場の「失敗した処理だけ再実行」は従来どおり使える。
    ただし画面を閉じると消えるので、失敗は必ず画面に出す。
    """
    try:
        return pr.add(source, failed, label), ""
    except Exception as e:  # noqa: BLE001
        return None, f"失敗した分を控えに保存できませんでした（この画面を閉じると再実行できなくなります）: {e}"


def settle(entry_id, still_failed, results=None):
    """再実行の結果を控えに書き戻す。返り値: エラー文または ""。"""
    if not entry_id:
        return ""
    try:
        pr.record_attempt(entry_id, still_failed, results)
        return ""
    except Exception as e:  # noqa: BLE001
        return f"再実行の結果を控えに書き戻せませんでした: {e}"


def open_count():
    """全画面ぶんの残り件数。読めなければ None（0にしない）。"""
    try:
        return len(pr.open_of(pr.load(use_cache=True)))
    except Exception:  # noqa: BLE001
        return None


def parse_yahoo_prices(text):
    """「code,price」の行（バックアップの yahoo_data.csv と同じ形）を {code: 価格} にする。

    読めない行が1つでもあれば ValueError（読めた分だけ黙って登録しない）。
    """
    prices = {}
    for no, line in enumerate(str(text or "").splitlines(), 1):
        line = line.strip()
        if not line or line.replace(" ", "").lower() == "code,price":
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 2 or not parts[0] or not parts[1].isdigit() or int(parts[1]) <= 0:
            raise ValueError(f"{no}行目を読めません（「商品コード,価格」の形で入力してください）: {line}")
        prices[parts[0]] = int(parts[1])
    if not prices:
        raise ValueError("登録する行がありません。")
    return prices


def render_manual_add(source):
    """控えに載らなかった失敗分を、あとから手で載せる（管理者用）。

    控えの保存そのものに失敗したとき（画面に「控えに保存できませんでした」と出たとき）や、
    この仕組みを入れる前の失敗分のための入口。Drive上の控えはこのアプリが作った
    ファイルしか読めないので、外から置いても載らない（2026-10-07 に確認）。
    """
    with st.expander("🛠️ 反映できなかったYahoo価格を控えに載せる（管理者用）", expanded=False):
        st.caption("失敗した実行のバックアップ（Driveの「価格改定履歴」内のフォルダ）にある "
                   "`yahoo_data.csv` の中身を、そのまま貼ってください（1行に「商品コード,価格」）。"
                   "載せただけでは反映されません。上に出る「⏳ 反映できていない処理」で"
                   "「🔁 再実行」を押すと Yahoo へ反映します。")
        label = st.text_input("どの実行の分か（バックアップのフォルダ名など）",
                              key=f"pr_manual_label_{source}")
        text = st.text_area("code,price", key=f"pr_manual_text_{source}", height=100,
                            placeholder="code,price\nabcd0001,1980")
        if st.button("控えに載せる", key=f"pr_manual_add_{source}",
                     disabled=not (text.strip() and label.strip())):
            try:
                prices = parse_yahoo_prices(text)
                pr.add(source, {"yahoo_price": prices}, f"{label.strip()}（手で登録）")
                st.rerun()
            except Exception as e:  # noqa: BLE001
                st.error(f"控えに載せられませんでした: {e}")


def render(source, execute, skip_ids=()):
    """source の控えを一覧し、1件ずつ再実行・取り下げできるようにする。

    execute: tasks を受けて (results, still_failed) を返す関数（その画面の実行関数）。
    skip_ids: いまの画面で実行中の分（下の結果欄に同じボタンがあるので二重に出さない）。
    """
    try:
        entries = pr.open_of(pr.load(use_cache=True), source)
    except Exception as e:  # noqa: BLE001
        st.error(f"⚠️ 反映できていない処理の控えを読めません（0件ではなく、確認できていません）: {e}")
        return
    skip = {i for i in skip_ids if i}
    entries = [e for e in entries if e.get("id") not in skip]

    last = st.session_state.get(f"_pending_retry_last_{source}")
    if not entries and not last:
        return

    title = (f"⏳ 反映できていない処理 {len(entries)}件（再実行できます）" if entries
             else "⏳ 反映できていない処理（いま再実行した結果）")
    with st.expander(title, expanded=True):
        if last:
            if last.get("err"):
                st.error(last["err"])
            n_ng = sum(1 for r in last["results"] if r.get("状態") == "失敗")
            if n_ng:
                st.error(f"再実行しましたが、まだ {n_ng}件 失敗しています（下の表）。控えには残っています。")
            else:
                st.success("✅ 再実行して反映しました。")
            st.dataframe(pd.DataFrame(last["results"]), use_container_width=True,
                         hide_index=True)
        if entries:
            st.caption("以前の実行で反映できなかった分です。認証切れなどの原因を直してから"
                       "「🔁 再実行」を押してください。CSVのアップし直し・入力のし直しは不要です。")
        for entry in entries:
            eid = entry["id"]
            age = pr.age_days(entry)
            old = "" if not age else f"・**{age}日前から未反映**"
            st.markdown(f"**{pr.created_jst(entry)}** {entry.get('label') or ''}{old}")
            for line in pr.describe(entry.get("tasks")):
                st.markdown(f"- {line}")
            c1, c2, c3 = st.columns([1, 2, 1])
            if c1.button("🔁 再実行", key=f"pr_run_{eid}", type="primary"):
                with st.spinner("再実行中…"):
                    try:
                        results, still = execute(entry["tasks"])
                    except Exception as e:  # noqa: BLE001
                        results = [{"ステップ": "実行", "対象": "-", "状態": "失敗",
                                    "メッセージ": f"実行中に想定外のエラー: {e}"}]
                        still = entry["tasks"]
                err = settle(eid, still, results)
                st.session_state[f"_pending_retry_last_{source}"] = {
                    "results": results, "err": err}
                st.rerun()
            reason = c2.text_input("取り下げる理由", key=f"pr_reason_{eid}",
                                   label_visibility="collapsed",
                                   placeholder="再実行しない場合はその理由（例: 手動で反映済み）")
            if c3.button("取り下げる", key=f"pr_drop_{eid}", disabled=not reason.strip()):
                try:
                    pr.withdraw(eid, reason)
                    st.rerun()
                except Exception as e:  # noqa: BLE001
                    st.error(f"取り下げに失敗しました: {e}")
            st.divider()
