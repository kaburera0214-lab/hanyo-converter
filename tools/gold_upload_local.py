# -*- coding: utf-8 -*-
"""
楽天GOLDへのローカルFTPアップローダ(Streamlit CloudからFTPが通らない場合のフォールバック)。

使い方:
    python tools/gold_upload_local.py <HTMLファイル> <リモートパス>
    例) python tools/gold_upload_local.py index.html event/2609_ss/index.html

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
        sys.exit("認証情報がありません。環境変数 GOLD_FTP_USER/GOLD_FTP_PASS か "
                 "~/.gold_ftp.json を設定してください。")
    return conf


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    local_file, remote_path = sys.argv[1], sys.argv[2].strip().lstrip("/")
    if not os.path.exists(local_file):
        sys.exit(f"ファイルが見つかりません: {local_file}")
    with open(local_file, "rb") as fp:
        data = fp.read()
    conf = load_conf()

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
    if conf["shop"]:
        print(f"公開URL: https://www.rakuten.ne.jp/gold/{conf['shop']}/{remote_path}")


if __name__ == "__main__":
    main()
