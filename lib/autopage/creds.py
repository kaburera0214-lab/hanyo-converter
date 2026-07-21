# -*- coding: utf-8 -*-
"""Secrets互換レイヤ。Streamlit Secrets → 環境変数の順で解決する。

Streamlit Cloud上ではst.secrets、GitHub Actions上では環境変数（GitHub Secrets）
から同名キーを読む。バッチからstreamlit未インストールでも動作する。
"""
import os


def get_secret(name, default=""):
    try:
        import streamlit as st
        v = str(st.secrets.get(name, "")).strip()
        if v:
            return v
    except Exception:
        pass
    return str(os.environ.get(name, default) or default).strip()


def shop_code():
    """楽天の店舗URL文字列（例: babygoodsfactory）。既存イベントLPと同じSecretsを使う。"""
    return get_secret("GOLD_SHOP_URL")
