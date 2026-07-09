# -*- coding: utf-8 -*-
"""
楽天GOLDへのFTPアップロード。

Secrets: GOLD_FTP_HOST(既定 ftp.rakuten.ne.jp) / GOLD_FTP_USER / GOLD_FTP_PASS /
         GOLD_SHOP_URL(公開URL組み立て用)
GOLD FTPは平文FTPのためパッシブモードで接続する。Streamlit Cloudから外向きFTPが
通らない環境では tools/gold_upload_local.py(ローカル実行)にフォールバックする。
"""
import ftplib
import io
import posixpath


class GoldFTPError(RuntimeError):
    pass


def _conf():
    import streamlit as st
    host = str(st.secrets.get("GOLD_FTP_HOST", "ftp.rakuten.ne.jp")).strip()
    user = str(st.secrets.get("GOLD_FTP_USER", "")).strip()
    pw = str(st.secrets.get("GOLD_FTP_PASS", "")).strip()
    if not user or not pw:
        raise GoldFTPError("Secrets に GOLD_FTP_USER / GOLD_FTP_PASS が未設定です。")
    return host, user, pw


def is_configured():
    import streamlit as st
    return bool(st.secrets.get("GOLD_FTP_USER", "")) and \
        bool(st.secrets.get("GOLD_FTP_PASS", ""))


def public_url(remote_path):
    import streamlit as st
    shop = str(st.secrets.get("GOLD_SHOP_URL", "")).strip()
    return f"https://www.rakuten.ne.jp/gold/{shop}/{remote_path.lstrip('/')}"


def _connect():
    host, user, pw = _conf()
    try:
        ftp = ftplib.FTP()
        ftp.connect(host, 21, timeout=20)
        ftp.login(user, pw)
        ftp.set_pasv(True)
        return ftp
    except OSError as e:
        raise GoldFTPError(
            f"FTP接続に失敗しました({e})。Streamlit CloudからFTPが通らない場合は、"
            "HTMLをダウンロードして tools/gold_upload_local.py でアップしてください。") from e
    except ftplib.error_perm as e:
        raise GoldFTPError(f"FTPログインに失敗しました({e})。ID/パスワードを確認してください。") from e


def test_connection():
    """接続テスト。ルート直下のエントリ名リストを返す。"""
    ftp = _connect()
    try:
        return ftp.nlst()
    finally:
        try:
            ftp.quit()
        except Exception:  # noqa: BLE001
            ftp.close()


def _ensure_dirs(ftp, dir_path):
    """リモートのディレクトリを再帰的に作成する(既存はスキップ)。"""
    if not dir_path or dir_path == ".":
        return
    parts = [p for p in dir_path.split("/") if p]
    cur = ""
    for p in parts:
        cur = f"{cur}/{p}" if cur else p
        try:
            ftp.mkd(cur)
        except ftplib.error_perm:
            pass  # 既に存在


def upload_html(remote_path, html_text):
    """
    HTML文字列を remote_path(例 'event/2609_ss/index.html')へアップロードする。
    アップ後にSIZEで検証し、{"size": バイト数, "url": 公開URL} を返す。
    """
    remote_path = remote_path.strip().lstrip("/")
    if not remote_path.endswith((".html", ".htm")):
        raise GoldFTPError("GOLDパスは .html で終わるファイルパスを指定してください。")
    data = html_text.encode("utf-8")
    ftp = _connect()
    try:
        _ensure_dirs(ftp, posixpath.dirname(remote_path))
        ftp.storbinary(f"STOR {remote_path}", io.BytesIO(data))
        try:
            size = ftp.size(remote_path)
        except ftplib.error_perm:
            size = None
        if size is not None and size != len(data):
            raise GoldFTPError(f"アップロード後のサイズが一致しません(送信{len(data)} / 受信{size})。")
        return {"size": len(data), "url": public_url(remote_path)}
    finally:
        try:
            ftp.quit()
        except Exception:  # noqa: BLE001
            ftp.close()
