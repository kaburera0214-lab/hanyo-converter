# -*- coding: utf-8 -*-
"""
商品ページ自動更新モジュール（EC-UP置換の内製版）。

楽天RMS Item API 2.0でスマホ用商品説明文にブロック（パンくずリスト・
商品スコア・同時購入・類似商品・更新日）をマーカー方式で自動挿入・更新する。
自社作成部分は <!--AP_TOP_START-->〜 等のHTMLコメントで分離し、
全撤去モードで完全に元へ戻せる。

構成:
- creds.py    : Secrets/環境変数の両対応（Streamlit Cloud / GitHub Actions）
- config.py   : config/autopage.json の読み書き（GitHub Contents API保存対応）
- compose.py  : マーカー方式の説明文合成器（バイト計算・優先度間引き）
- blocks.py   : 各システムのHTMLブロック生成（楽天スマホ説明文の許可タグのみ使用）
- rms_items.py: RMS Item/Category APIラッパ（診断機能付き）
- reviews.py  : 楽天市場商品検索APIでレビュー点数・件数取得
- state.py    : SQLite状態DB（生成ハッシュ・レビュー/カテゴリキャッシュ）
- runner.py   : バッチ/UI共通の実行エンジン
"""
