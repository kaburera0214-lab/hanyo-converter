# -*- coding: utf-8 -*-
"""
認可切れアラートの本文（接続先を問わない）。

【方針】ne_alerts.py と同じ。通知は「読んだ人がその場で完結できる」ことを最優先にする。
  - スタッフに送るのは「自分で直せること」＝ブラウザでログインするだけの再認可のみ
  - 専門用語（トークン・API・GitHub等）は本文に出さない
  - できなかったときの逃げ道（タスクに返信）を必ず書く

NEの文面は実運用で調整済みなので、ne_alerts.reauth_body をそのまま使う。
ここではYahoo分を追加し、接続先で振り分ける入口を用意する。
"""
from lib.notify import ne_alerts

# 入荷登録ページの相対パス（Streamlitはファイル名から日本語スラッグを作る）
RECEIVING_PATH = ne_alerts.RECEIVING_PATH


def _yahoo_steps(app_url=""):
    """Yahoo再認可の手順（切れた後の依頼と、切れる前の依頼で同じものを使う）。"""
    url = ne_alerts.receiving_url(app_url)
    if url:
        steps = ["下のリンクを開く（入荷登録の画面が開きます）\n   " + url]
    else:
        steps = ["パピー業務ツールを開く（いつものブックマークから）",
                 "左のメニューから「📥 入荷登録」を開く"]
    steps += [
        "画面の上のほうにある「🔐 Yahoo API接続（管理者用）」の行をクリックして開く",
        "中にある「🔑 Yahooにログインして認可する」ボタンを押す",
        "Yahooのログイン画面が開くので、**店舗オーナーのYahoo ID**でログインする\n"
        "   （個人のYahoo IDではありません。分からなければ犬飼に聞いてください）",
        "「同意する」を押す",
        "パピー業務ツールの最初の画面に戻ります",
    ]
    return "".join("{}. {}\n".format(i, t) for i, t in enumerate(steps, 1))


def yahoo_expiring_body(deadline_text="", app_url=""):
    """切れる前の再認可のお願い。期限までに済ませれば、業務は一度も止まらない。"""
    return (
        "[info][title]🔐【期限あり】Yahooの再認可をお願いします（3分）[/title]"
        "Yahooへの自動反映は、28日ごとにログインし直さないと止まる決まりです（延長できません）。\n"
        "**いまはまだ動いています。**" + (deadline_text + "。\n" if deadline_text else "\n") +
        "期限までに下の操作をしておけば、価格改定も入荷登録も止まりません。\n"
        "用意するもの: 店舗オーナーのYahoo ID・パスワード\n"
        "[hr]"
        "■ やること\n"
        + _yahoo_steps(app_url) +
        "[hr]"
        "■ できたか確かめる\n"
        "「📥 入荷登録」→「🔐 Yahoo API接続」を開いて、期限の日付が28日先に変わっていればOKです。\n"
        "■ うまくいかないとき\n"
        "このタスクに「できませんでした」と、画面に出ている赤い文字をそのまま返信してください。"
        "[/info]")


def yahoo_reauth_body(app_url=""):
    """スタッフ向け: Yahoo再認可のお願い（手順つき）。"""
    steps_text = _yahoo_steps(app_url)
    return (
        "[info][title]🔐【要対応】Yahooの再認可をお願いします（3分）[/title]"
        "価格改定の「Yahooへの自動反映」が止まっています。\n"
        "このままだと価格改定で「確定して反映」を押しても、"
        "楽天とネクストエンジンだけ更新され、Yahooの価格が変わりません。\n"
        "用意するもの: 店舗オーナーのYahoo ID・パスワード\n"
        "[hr]"
        "■ やること\n"
        + steps_text +
        "[hr]"
        "■ できたか確かめる\n"
        "「📥 入荷登録」→「🔐 Yahoo API接続」を開いて、緑色で「認可済み」と出ていればOKです。\n"
        "■ 止まっていた分の反映（重要）\n"
        "反映できなかった分は控えてあります。再認可のあと「📥 入荷登録」または「💰 価格改定」を開くと、\n"
        "上のほうに「⏳ 反映できていない処理」が出るので、そこの「🔁 再実行」を押してください。\n"
        "**そのままCSVをアップし直して「確定して反映」を押さないでください。**\n"
        "楽天とネクストエンジンだけ先に新しい価格へ変わっている状態で計算し直すと、\n"
        "その新しい価格を元にもう一段階値上げした金額になってしまいます。\n"
        "（例: 363円→368円まで反映済みのとき、押し直すと373円になる）\n"
        "「⏳ 反映できていない処理」が出ていないときは、犬飼に連絡してください。\n"
        "■ うまくいかないとき\n"
        "このタスクに「できませんでした」と、画面に出ている赤い文字をそのまま返信してください。"
        "犬飼が対応します。無理に進めなくて大丈夫です。"
        "[/info]")


# 接続先キー → スタッフ向け再認可文面
_STAFF_BODIES = {
    "ne": ne_alerts.reauth_body,
    "yahoo": yahoo_reauth_body,
}


_EXPIRING_BODIES = {
    "yahoo": yahoo_expiring_body,
}


def expiring_body(provider_key, deadline_text="", app_url=""):
    """切れる前の再認可依頼。未知の接続先でも黙って落ちない。"""
    builder = _EXPIRING_BODIES.get(provider_key)
    if builder:
        return builder(deadline_text, app_url)
    return (
        "[info][title]🔐【期限あり】{}の再認可が必要です[/title]"
        "{}\nこのタスクに返信してください。犬飼が対応します。"
        "[/info]".format(provider_key, deadline_text))


def reauth_body(provider_key, app_url=""):
    """接続先に応じたスタッフ向け再認可文面。未知の接続先でも黙って落ちない。"""
    builder = _STAFF_BODIES.get(provider_key)
    if builder:
        return builder(app_url)
    return (
        "[info][title]🔐【要対応】{}の再認可が必要です[/title]"
        "パピー業務ツールの「{}」への自動反映が止まっています。\n"
        "このタスクに返信してください。犬飼が対応します。"
        "[/info]".format(provider_key, provider_key))


def admin_body(title, error, impact, action, workflow=""):
    """管理者向け（ne_alerts と共通の書式）。"""
    return ne_alerts.admin_body(title, error, impact, action, workflow)
