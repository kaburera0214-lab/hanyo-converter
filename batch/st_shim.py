# -*- coding: utf-8 -*-
"""
Streamlit ヘッドレスシム（GitHub Actions等でlibをそのまま動かすための最小代替）。

libは st.secrets / st.session_state と、表示系（st.write/st.spinner等）しか使わないため、
secretsを環境変数に、session_stateをただのdictに、表示系を何もしないダミーに差し替える。

使い方（libをimportする**前**に呼ぶこと）:
    from batch import st_shim; st_shim.install()
    from lib.ne_api import client
"""
import os
import sys
import types


class _Secrets:
    def get(self, key, default=None):
        # GitHub Actionsは未登録のsecretも空文字でenvにセットするため、空は「未設定」として既定値を使う
        v = os.environ.get(key)
        return v if v not in (None, "") else default

    def __getitem__(self, key):
        v = os.environ.get(key)
        if not v:
            raise KeyError(key)
        return v

    def __contains__(self, key):
        return bool(os.environ.get(key))


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


def install():
    """sys.modules["streamlit"] をシムに差し替える（既にimport済みなら何もしない）。"""
    if isinstance(sys.modules.get("streamlit"), _StreamlitShim):
        return
    sys.modules["streamlit"] = _StreamlitShim("streamlit")
