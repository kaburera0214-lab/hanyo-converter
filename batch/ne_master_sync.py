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

在庫の列（在庫数・引当数・フリー在庫数ほか）も一緒に保存する（2026-10-09〜）。
  python batch/ne_master_sync.py            # 全件取得して保存
  python batch/ne_master_sync.py --probe    # 在庫の項目が使えるかだけ確かめる（項目名しか出さない）
このリポジトリは公開なので、ログに出すのは件数・項目名・ファイル名だけ。在庫の値は出さない。
在庫の列が取れなかったときは、マスタは保存したうえで失敗として終わる
（「在庫0」と「取れなかった」を取り違えないため、列は出さず、止まったことが分かるようにする）。

区分の列（商品区分・取扱区分・仕入先コード）も足す（2026-10-16〜）。取れなかった項目は列を出さずにログへ名前を出す。
セット商品マスタも setgoods_auto_*.csv として保存する（2026-10-16〜）。0件や取得失敗は、
商品マスタを保存したうえで失敗として終わる（「セットが無い」と「取れなかった」を取り違えないため）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from batch import st_shim                          # noqa: E402
st_shim.install()                                  # libのimportより前に差し替える

from lib import master_store                       # noqa: E402
from lib.ne_api import client, master_sync, usage  # noqa: E402


class StockColumnsMissing(Exception):
    """マスタは保存できたが、在庫の列が取れなかった。"""


class SetgoodsFailed(Exception):
    """商品マスタは保存できたが、セット商品マスタが取れなかった（0件を含む）。"""


def probe():
    """在庫・区分の項目がこの環境で使えるかを確かめる。出すのは項目名だけ。"""
    alive, dead = master_sync.check_stock_fields()
    usage.flush()
    print(f"[ne_master_sync] probe: 使える在庫・区分の項目 {len(alive)}個", flush=True)
    for f in alive:
        print(f"  o {f} -> {master_sync.OPTIONAL_FIELD_MAP[f]}", flush=True)
    print(f"[ne_master_sync] probe: 使えない在庫・区分の項目 {len(dead)}個", flush=True)
    for f in dead:
        print(f"  x {f} -> {master_sync.OPTIONAL_FIELD_MAP[f]}", flush=True)
    return 0 if "stock_quantity" in alive else 1


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
    missing = master_sync.missing_stock_columns(df)
    got = [c for c in master_sync.STOCK_ORDER if c in df.columns]
    print(f"[ne_master_sync] 在庫の列: 入った {len(got)}個 {got} / 入らなかった {len(missing)}個 {missing}",
          flush=True)
    k_missing = master_sync.missing_kubun_columns(df)
    k_got = [c for c in master_sync.KUBUN_ORDER if c in df.columns]
    print(f"[ne_master_sync] 区分の列: 入った {len(k_got)}個 {k_got} / 入らなかった {len(k_missing)}個 {k_missing}",
          flush=True)

    # セット商品マスタ。商品マスタは保存済みなので、ここで失敗しても商品マスタは残る
    set_error = None
    try:
        sdf = master_sync.fetch_setgoods()
        if sdf.empty:
            raise RuntimeError("セット商品マスタが0件でした（取れなかったとみなす）")
        sname = master_sync.save_setgoods_auto(sdf, folder)
        print(f"[ne_master_sync] OK saved={sname} rows={len(sdf):,} sets={sdf['セット商品コード'].nunique():,}",
              flush=True)
    except client.NEAuthError:
        raise
    except Exception as e:  # noqa: BLE001
        set_error = e
    usage.flush()

    if master_sync.STOCK_KEY_COLUMN in missing:
        raise StockColumnsMissing(
            f"マスタ {name} は保存しましたが、在庫の列（{master_sync.STOCK_KEY_COLUMN}）を取得できませんでした"
            + (f"。セット商品マスタも失敗: {set_error}" if set_error else ""))
    if set_error:
        raise SetgoodsFailed(f"マスタ {name} は保存しましたが、セット商品マスタを保存できませんでした: {set_error}")


if __name__ == "__main__":
    if "--probe" in sys.argv[1:]:
        try:
            sys.exit(probe())
        except Exception as e:  # noqa: BLE001
            print(f"[ne_master_sync] probe FAILED: {e}", file=sys.stderr, flush=True)
            sys.exit(1)
    try:
        main()
    except StockColumnsMissing as e:
        print(f"[ne_master_sync] FAILED: {e}", file=sys.stderr, flush=True)
        try:
            from lib.notify import chatwork, ne_alerts
            chatwork.create_task(ne_alerts.admin_body(
                title="NEマスタ週次自動取得：在庫の列が取れませんでした",
                error=str(e),
                impact="商品マスタ（原価など）は更新されています。在庫数の列だけがありません。"
                       "在庫を見て判断する処理は、前回のマスタの在庫か「不明」になります。",
                action="Run workflow の probe で使える在庫の項目を確認 → lib/ne_api/master_sync.py の STOCK_FIELD_MAP を直す",
                workflow="ne-master-sync.yml"), limit_days=3, audience=chatwork.ADMIN)
        except Exception:  # noqa: BLE001
            pass
        sys.exit(1)
    except SetgoodsFailed as e:
        print(f"[ne_master_sync] FAILED: {e}", file=sys.stderr, flush=True)
        try:
            from lib.notify import chatwork, ne_alerts
            chatwork.create_task(ne_alerts.admin_body(
                title="NEマスタ週次自動取得：セット商品マスタが取れませんでした",
                error=str(e),
                impact="商品マスタ（原価・在庫・区分）は更新されています。セット商品マスタ（setgoods_auto_*）だけが古いままです。",
                action="ログを確認して修正 → Run workflow で再実行",
                workflow="ne-master-sync.yml"), limit_days=3, audience=chatwork.ADMIN)
        except Exception:  # noqa: BLE001
            pass
        sys.exit(1)
    except Exception as e:  # noqa: BLE001
        print(f"[ne_master_sync] FAILED: {e}", file=sys.stderr, flush=True)
        # 原因で宛先を分ける: 認証切れは現場スタッフが再認可で直せる（手順つきで souko へ）。
        # それ以外はスタッフには直せないので管理者にだけ送る。
        try:
            from lib.notify import chatwork, ne_alerts
            message = str(e)
            if "認証" in message or "認可" in message or "002" in message:
                chatwork.create_task(ne_alerts.reauth_body(os.environ.get("APP_URL", "").strip()),
                                     limit_days=1, audience=chatwork.STAFF)
            else:
                chatwork.create_task(ne_alerts.admin_body(
                    title="NEマスタ週次自動取得が失敗",
                    error=message,
                    impact="Driveの商品マスタ（master_auto_*）が更新されません。"
                           "手動アップのマスタが最新ならすぐ困ることはありません。",
                    action="ログを確認して修正 → Run workflow で再実行",
                    workflow="ne-master-sync.yml"), limit_days=3, audience=chatwork.ADMIN)
        except Exception:  # noqa: BLE001
            pass
        sys.exit(1)
