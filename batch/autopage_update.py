# -*- coding: utf-8 -*-
"""商品ページ自動更新バッチ（GitHub Actions / ローカル実行用）。

使い方:
    python batch/autopage_update.py                # configに従い実行（dry_run設定を尊重）
    python batch/autopage_update.py --dry-run      # 強制dry-run
    python batch/autopage_update.py --apply        # config.enabled=true時のみ実反映
    python batch/autopage_update.py --remove-all   # 全ブロック撤去（タグ削除相当）
    python batch/autopage_update.py --items A,B    # 対象商品を指定
    python batch/autopage_update.py --limit 10

必要な環境変数（GitHub Secrets）:
    RMS_SERVICE_SECRET / RMS_LICENSE_KEY / RAKUTEN_APP_ID / GOLD_SHOP_URL
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.autopage import config as apconfig  # noqa: E402
from lib.autopage import runner, state as apstate  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="PATCHせず差分のみ")
    ap.add_argument("--apply", action="store_true",
                    help="config.dry_runを無視して実反映（enabled=true必須）")
    ap.add_argument("--remove-all", action="store_true", help="全ブロック撤去")
    ap.add_argument("--items", default="", help="対象商品管理番号（カンマ区切り）")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    cfg = apconfig.load_config()
    if args.dry_run:
        cfg["dry_run"] = True
    if args.apply:
        cfg["dry_run"] = False
    if args.remove_all and not cfg.get("enabled"):
        # 撤去はenabledに関わらず実行できる必要がある（緊急ロールバック用）
        cfg["enabled"] = True
        cfg["dry_run"] = False

    targets = [t.strip() for t in args.items.split(",") if t.strip()] or None
    st = apstate.State()
    try:
        summary = runner.run(cfg, st, targets=targets,
                             remove_all=args.remove_all, limit=args.limit)
    finally:
        st.close()

    apstate.write_run_log(summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if summary["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
