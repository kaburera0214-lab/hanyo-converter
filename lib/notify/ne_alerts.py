# -*- coding: utf-8 -*-
"""
NE関連アラートの本文（Chatworkタスク）。

【方針】通知は「読んだ人がその場で完結できる」ことを最優先にする。
souko（倉庫）アカウントは現場スタッフが見るので、
  - 送るのは「スタッフが自分で直せること」＝NEの再認可（ブラウザでログインするだけ）のみ
  - 本文は画面の名前どおりの手順にし、専門用語（トークン・API・GitHub等）は出さない
  - できなかったときの逃げ道（タスクに返信）を必ず書く
バッチの不具合や無料枠超過など**スタッフには直せないもの**は管理者宛に送り、
「スタッフの対応は不要」と明記する（現場を止めない・不安にさせない）。
"""

REPO_ACTIONS = "https://github.com/kaburera0214-lab/hanyo-converter/actions"
# 入荷登録ページの相対パス（Streamlitはファイル名から日本語スラッグを作る）。
# アプリのURL自体は公開リポジトリに置かない方針のため、APP_URL（Secrets）から組み立てる。
RECEIVING_PATH = "/%E5%85%A5%E8%8D%B7%E7%99%BB%E9%8C%B2"


def receiving_url(app_url):
    """アプリTOPのURL → 入荷登録ページの直リンク。app_urlが空なら空文字。"""
    app_url = (app_url or "").strip()
    return app_url.rstrip("/") + RECEIVING_PATH if app_url else ""


def reauth_body(app_url=""):
    """スタッフ向け: NE再認可のお願い（手順つき）。
    app_url（アプリTOP）があれば入荷登録ページへの直リンクにして手順を1つ減らす。"""
    url = receiving_url(app_url)
    if url:
        steps = ["下のリンクを開く（入荷登録の画面が開きます）\n   " + url]
    else:
        steps = ["パピー業務ツールを開く（いつものブックマークから）",
                 "左のメニューから「📥 入荷登録」を開く"]
    steps += [
        "画面の上のほうにある「🔐 NE API接続（管理者用）」の行をクリックして開く",
        "中にある「🔑 NEにログインして認可する」ボタンを押す",
        "ネクストエンジンのログイン画面が開くので、いつものID・パスワードでログインする",
        "「許可」を押す",
        "パピー業務ツールの最初の画面に戻り、緑色で\n"
        "   「✅ ネクストエンジンAPIの認可が完了しました」と出れば完了です",
    ]
    steps_text = "".join("{}. {}\n".format(i, t) for i, t in enumerate(steps, 1))
    return (
        "[info][title]🔐【要対応】ネクストエンジンの再認可をお願いします（3分）[/title]"
        "入荷登録の「ネクストエンジンへの自動反映」が止まっています。\n"
        "このままだと入荷登録で「🚀 更新を実行」を押しても、"
        "ロケーションと配送サイズがネクストエンジンに反映されません。\n"
        "用意するもの: ネクストエンジンのID・パスワード（いつも使っているもの）\n"
        "[hr]"
        "■ やること\n"
        + steps_text +
        "[hr]"
        "■ できたか確かめる\n"
        "「📥 入荷登録」→「🔐 NE API接続」を開いて、緑色で「認可済み」と出ていればOKです。\n"
        "■ 入荷作業の途中だった場合\n"
        "入力した内容は消えていません。「📥 入荷登録」に戻って\n"
        "「🔁 失敗した処理だけ再実行」を押してください。\n"
        "■ うまくいかないとき\n"
        "このタスクに「できませんでした」と、画面に出ている赤い文字をそのまま返信してください。"
        "犬飼が対応します。無理に進めなくて大丈夫です。"
        "[/info]")


def admin_body(title, error, impact, action, workflow=""):
    """管理者向け: 開発担当者しか直せない失敗の通知（原因と次の一手を明記）。"""
    link = "\n{}/workflows/{}".format(REPO_ACTIONS, workflow) if workflow \
        else "\n" + REPO_ACTIONS
    return (
        "[info][title]⚠️【犬飼対応】{}[/title]".format(title) +
        "エラー: {}\n".format(error) +
        "影響: {}\n".format(impact) +
        "対応: {}{}\n".format(action, link) +
        "※倉庫スタッフの対応は不要です（soukoには通知していません）。"
        "[/info]")
