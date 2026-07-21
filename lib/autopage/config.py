# -*- coding: utf-8 -*-
"""config/autopage.json の読み書き。

- ローカル（リポジトリ内）ファイルが正。UIからの保存はローカル書込＋
  GitHub Contents APIでのコミット（Streamlit Cloud上ではローカルが揮発するため）。
- バッチ（GitHub Actions）はcheckoutされたファイルをそのまま読む。
"""
import base64
import copy
import json
from pathlib import Path

import requests

from . import creds

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "config" / "autopage.json"
CONFIG_REPO_PATH = "config/autopage.json"  # GitHub上のパス

DEFAULT_CONFIG = {
    # 全体スイッチ。falseの間はPATCHを一切行わない（dry-run強制）
    "enabled": False,
    # trueの間はPATCHせず差分レポートのみ
    "dry_run": True,
    # 対象商品の許可リスト。空でない間はこの商品管理番号のみ処理する（段階展開用）
    "allowlist": [],
    # 楽天の店舗設定。shop_codeが空ならSecrets GOLD_SHOP_URLを使う
    "shop_code": "",
    "shop_id": "318802",
    # スマホ用商品説明文のバイト上限と温存バイト数（全角=2byte換算）
    "byte_limit": 10240,
    "byte_reserve": 250,
    # API呼び出し間隔（秒）
    "rate_sleep": 0.7,
    # レイアウト: 御社作成説明文の上/下に入れるシステムの順序（先頭ほど優先＝間引きされにくい）
    "layout_top": ["breadcrumb"],
    "layout_bottom": ["score", "copurchase", "similar", "update_date"],
    "systems": {
        "breadcrumb": {
            "enabled": True,
            # link: category=カテゴリページ / search=ショップ内検索結果ページ
            "link": "category",
            # RMS表示先カテゴリの何番目を使うか: first / last
            "category_position": "last",
            "font_size": "medium",  # small / medium
            # link=searchのとき、この語を含むカテゴリはカテゴリページへリンク
            "exception_keywords": ["カテゴリ", "ブランド", "メーカー", "アイテム",
                                   "全商品", "その他", "特集"],
        },
        "score": {
            "enabled": True,
            "min_average": 4.0,
            "min_count": 3,
        },
        "copurchase": {
            "enabled": False,
            "max_items": 2,
            "band_title": "＼よく一緒に購入されています／",
        },
        "similar": {
            "enabled": False,
            "max_items": 10,
            "band_title": "＼こちらもおすすめ／",
        },
        "update_date": {
            "enabled": False,
            "align": "right",
        },
    },
    # 商品名の整形（類似・同時購入の表示用）
    "item_name": {"show": True, "strip_brackets": True},
    # システムに表示しない商品（部分一致）
    "hidden_items": {"name_contains": [], "manage_number_contains": []},
    # レビュー/カテゴリキャッシュの更新間隔（日）
    "review_refresh_days": 7,
    "category_refresh_days": 7,
}


def _merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config():
    """デフォルトにファイル内容を重ねて返す（キー欠落に強い）。"""
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return _merge(DEFAULT_CONFIG, json.load(f))
    return copy.deepcopy(DEFAULT_CONFIG)


def save_config_local(cfg):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")


def save_config_github(cfg):
    """GitHub Contents APIでconfig/autopage.jsonをコミットする。
    戻り値: (成功bool, エラーメッセージ or None)。既存アプリの保存パターンと同じ。"""
    token = creds.get_secret("GITHUB_TOKEN")
    repo = creds.get_secret("GITHUB_REPO")
    if not token or not repo:
        return False, "GITHUB_TOKEN / GITHUB_REPO がSecretsに設定されていません"
    url = f"https://api.github.com/repos/{repo}/contents/{CONFIG_REPO_PATH}"
    headers = {"Authorization": f"token {token}",
               "Accept": "application/vnd.github.v3+json"}
    r = requests.get(url, headers=headers, timeout=30)
    sha = r.json().get("sha") if r.ok else None
    body = json.dumps(cfg, ensure_ascii=False, indent=2) + "\n"
    payload = {
        "message": "Update autopage config via app",
        "content": base64.b64encode(body.encode("utf-8")).decode(),
    }
    if sha:
        payload["sha"] = sha
    r = requests.put(url, json=payload, headers=headers, timeout=30)
    if r.ok:
        return True, None
    return False, r.json().get("message", f"HTTP {r.status_code}")
