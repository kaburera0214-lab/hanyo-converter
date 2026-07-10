# -*- coding: utf-8 -*-
"""
楽天GOLDへのローカルFTPアップローダ(Streamlit CloudはFTPを遮断しているため、これが正式ルート)。

使い方(どちらか):
    1. アプリの「アップ用パッケージをダウンロード」で取得したzipを gold_upload.bat にドロップ
       (= python tools/gold_upload_local.py <zipファイル>)
    2. HTMLファイルとリモートパスを直接指定
       python tools/gold_upload_local.py index.html event/2609_ss/index.html

認証情報は次の優先順で読む:
    1. 環境変数 GOLD_FTP_USER / GOLD_FTP_PASS (任意で GOLD_FTP_HOST, GOLD_SHOP_URL)
    2. %USERPROFILE%\\.gold_ftp.json
       {"host": "ftp.rakuten.ne.jp", "user": "...", "pass": "...", "shop": "babygoodsfactory"}
"""
import ftplib
import io
import json
import os
import posixpath
import sys
import zipfile


def load_conf():
    conf = {
        "host": os.environ.get("GOLD_FTP_HOST", ""),
        "user": os.environ.get("GOLD_FTP_USER", ""),
        "pass": os.environ.get("GOLD_FTP_PASS", ""),
        "shop": os.environ.get("GOLD_SHOP_URL", ""),
    }
    if not conf["user"] or not conf["pass"]:
        path = os.path.join(os.path.expanduser("~"), ".gold_ftp.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fp:
                file_conf = json.load(fp)
            for k in conf:
                conf[k] = conf[k] or str(file_conf.get(k, ""))
    conf["host"] = conf["host"] or "ftp.rakuten.ne.jp"
    if not conf["user"] or not conf["pass"]:
        sys.exit("認証情報がありません。%USERPROFILE%\\.gold_ftp.json を作成してください。\n"
                 '内容例: {"host": "ftp.rakuten.ne.jp", "user": "FTPユーザー名", '
                 '"pass": "FTPパスワード", "shop": "babygoodsfactory"}')
    return conf


def read_input(path):
    """引数からアップ対象を読み取り (data, remote_path, shop_hint) を返す。"""
    if path.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            meta = json.loads(z.read("upload.json").decode("utf-8"))
            data = z.read("index.html")
        remote_path = meta.get("remote_path", "")
        if not remote_path:
            sys.exit("zip内のupload.jsonにremote_pathがありません。")
        return data, remote_path, meta.get("shop", "")
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    with open(path, "rb") as fp:
        data = fp.read()
    return data, sys.argv[2].strip().lstrip("/"), ""


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    path = sys.argv[1]
    if not os.path.exists(path):
        sys.exit(f"ファイルが見つかりません: {path}")
    data, remote_path, shop_hint = read_input(path)
    conf = load_conf()
    shop = conf["shop"] or shop_hint

    print(f"接続中: {conf['host']} ...")
    ftp = ftplib.FTP()
    ftp.connect(conf["host"], 21, timeout=20)
    ftp.login(conf["user"], conf["pass"])
    ftp.set_pasv(True)
    # ディレクトリを再帰作成
    cur = ""
    for p in [x for x in posixpath.dirname(remote_path).split("/") if x]:
        cur = f"{cur}/{p}" if cur else p
        try:
            ftp.mkd(cur)
        except ftplib.error_perm:
            pass
    ftp.storbinary(f"STOR {remote_path}", io.BytesIO(data))
    try:
        size = ftp.size(remote_path)
    except ftplib.error_perm:
        size = None
    ftp.quit()

    print(f"アップロード完了: {remote_path} ({len(data):,} bytes, サーバ側 {size})")
    if shop:
        print(f"公開URL: https://www.rakuten.ne.jp/gold/{shop}/{remote_path}")


if __name__ == "__main__":
    main()
