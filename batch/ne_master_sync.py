#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
週次バッチ: NE商品マスタをAPIで全件取得し、Driveへ master_auto_*.csv として保存する。

GitHub Actions（.github/workflows/ne-master-sync.yml）等からヘッドレスで実行する。
アプリのlib（NEトークン自動更新・Drive保存・使用量カウント・Chatwork通知）を
そのまま再利用するため、Streamlit非依存にする軽量シム（batch/st_shim.py）を噛ませる。

必要な環境変数（GitHub Secrets / ローカルのenv）:
  GOOGLE_REFRESH_TOKEN / GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET  … Drive認証
  NE_CLIENT_ID / NE_CLIENT_SECRET                                … NE認証
  PRODUCT_MASTER_FOLDER_ID                                       … 保存先Driveフォルダ
  CHATWORK_API_TOKEN（任意）                                     … 失敗/超過アラート
※NEのアクセストークンはDriveの ne_tokens.json を使う（事前にアプリで認可済みが前提）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from batch import st_shim                          # noqa: E402
st_shim.install()                                  # libのimportより前に差し替える

from lib import master_store                       # noqa: E402
from lib.ne_api import master_sync, usage          # noqa: E402


def main():
    folder = master_store.folder_id()
    print(f"[ne_master_sync] start: folder={folder}", flush=True)

    def _progress(done, total):
        if total and (done % 20000 == 0 or done >= total):
            print(f"[ne_master_sync] fetched {done:,}/{total:,}", flush=True)

    df, jan_ok = master_sync.fetch_master(on_progress=_progress)
    name = master_sync.save_master_auto(df, folder)
    usage.flush()   # このバッチのAPI呼び出し回数もカウンタへ反映
    print(f"[ne_master_sync] OK saved={name} rows={len(df):,} jan_ok={jan_ok}", flush=True)
    if not jan_ok:
        print("[ne_master_sync] WARN: JANコード列が空でした", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"[ne_master_sync] FAILED: {e}", file=sys.stderr, flush=True)
        try:
            from lib.notify import chatwork
            chatwork.create_task("[info][title]⚠️ NEマスタ週次自動取得が失敗[/title]"
                                 f"{e}\nGitHub Actionsのログを確認してください。[/info]")
        except Exception:  # noqa: BLE001
            pass
        sys.exit(1)
