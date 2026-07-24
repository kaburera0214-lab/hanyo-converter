#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
週次バッチ: NE商品マスタをAPIで全件取得し、Driveへ master_auto_*.csv として保存する。

GitHub Actions（.github/workflows/ne-master-sync.yml）等からヘッドレスで実行する。
アプリのlib（NEトークン自動更新・Drive保存・使用量カウント・Chatwork通知）を
そのまま再利用するため、Streamlit非依存にする軽量シムを噛ませる（st.secrets=環境変数）。

必要な環境変数（GitHub Secrets / ローカルのenv）:
  GOOGLE_REFRESH_TOKEN / GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET  … Drive認証
  NE_CLIENT_ID / NE_CLIENT_SECRET                                … NE認証
  PRODUCT_MASTER_FOLDER_ID                                       … 保存先Driveフォルダ
  CHATWORK_API_TOKEN（任意）                                     … 失敗/超過アラート
※NEのアクセストークンはDriveの ne_tokens.json を使う（事前にアプリで認可済みが前提）。
"""
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# ── Streamlit ヘッドレスシム（libがst.secrets/st.session_stateだけ使うのを満たす） ──
class _Secrets:
    def get(self, key, default=None):
        return os.environ.get(key, default)

    def __getitem__(self, key):
        v = os.environ.get(key)
        if v is None:
            raise KeyError(key)
        return v

    def __contains__(self, key):
        return key in os.environ


class _NoOp:
    """st.write/st.spinner等の代替（呼んでも何もしない・with可）。"""
    def __call__(self, *a, **k):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __getattr__(self, _name):
        return self


class _StreamlitShim(types.ModuleType):
    secrets = _Secrets()
    session_state = {}

    def __getattr__(self, _name):
        return _NoOp()


sys.modules["streamlit"] = _StreamlitShim("streamlit")

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
