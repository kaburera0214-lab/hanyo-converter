# -*- coding: utf-8 -*-
"""バッチ/UI共通の実行エンジン。

1商品の処理フロー:
  RMSから商品取得 → 自社作成部分を分離 → 有効システムのブロック生成
  → バイト上限内で合成 → 前回生成分とハッシュ比較 → 変化時のみPATCH

安全装置:
- config.enabled=false または dry_run=true の間はPATCHしない
- allowlist が空でない間は列挙された商品しか処理しない
- remove_all モードで全ブロックを撤去し完全に元へ戻せる
"""
import hashlib
import time

from . import blocks, compose, creds, reviews, rms_items


def _hash(text):
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:16]


def _shop_code(cfg):
    return cfg.get("shop_code") or creds.shop_code()


def build_blocks(cfg, item, state):
    """有効システムのブロックHTMLをレイアウト順（優先度順）で返す。"""
    mn = item["manage_number"]
    shop = _shop_code(cfg)
    systems = cfg.get("systems", {})
    out = []
    notes = {}

    def enabled(name):
        return bool(systems.get(name, {}).get("enabled"))

    for zone, layout in (("top", cfg.get("layout_top", [])),
                         ("bottom", cfg.get("layout_bottom", []))):
        for name in layout:
            if not enabled(name):
                continue
            html = None
            try:
                if name == "breadcrumb":
                    # キャッシュには位置設定適用済みの階層パスを保存する
                    # （category_position変更後は最大refresh_days日で追従）
                    path = state.get_categories(
                        mn, max_age_days=cfg.get("category_refresh_days", 7))
                    if path is None:
                        path = rms_items.resolve_item_breadcrumb(
                            mn, systems[name].get("category_position", "last"))
                        state.set_categories(mn, path)
                        time.sleep(float(cfg.get("rate_sleep", 0.7)))
                    html = blocks.breadcrumb(systems[name], shop,
                                             cfg.get("shop_id", ""), path)
                elif name == "score":
                    rev = state.get_review(
                        mn, max_age_days=cfg.get("review_refresh_days", 7))
                    if rev is None and reviews.is_configured():
                        fetched = reviews.fetch_review(shop, mn)
                        if fetched:
                            state.set_review(mn, fetched["review_count"],
                                             fetched["review_average"])
                            rev = fetched
                        time.sleep(1.0)  # 公開APIは1req/s制限
                    if rev:
                        url = f"https://item.rakuten.co.jp/{shop}/{mn}/"
                        html = blocks.score(systems[name], url,
                                            rev["review_average"],
                                            rev["review_count"])
                elif name == "update_date":
                    import datetime
                    html = blocks.update_date(
                        systems[name],
                        datetime.date.today().strftime("%Y/%m/%d"))
                elif name in ("copurchase", "similar"):
                    # Phase 3で実装。データ未整備のためスキップ
                    continue
            except Exception as e:  # noqa: BLE001
                notes[name] = f"生成エラー: {e}"
                continue
            if html:
                out.append({"system": name, "zone": zone, "html": html})
    return out, notes


def process_item(mn, cfg, state, *, remove_all=False, force=False):
    """1商品を処理して結果dictを返す。PATCH可否はconfigに従う。"""
    result = {"manage_number": mn, "action": "none", "included": [],
              "dropped": [], "notes": {}, "error": None,
              "bytes_before": 0, "bytes_after": 0}
    try:
        item = rms_items.get_item(mn)
    except Exception as e:  # noqa: BLE001
        result["error"] = f"商品取得失敗: {e}"
        state.upsert_item(mn, error=result["error"])
        return result

    sp = item["sp_description"]
    own = compose.strip_generated(sp)
    result["bytes_before"] = compose.rakuten_len(sp)

    if remove_all:
        new_sp, included, dropped = own, [], []
    elif blocks.is_hidden(item["title"], mn, cfg.get("hidden_items")):
        # 非表示商品はブロックを入れない（既存挿入分は撤去）
        new_sp, included, dropped = own, [], ["(非表示商品設定)"]
    else:
        blks, notes = build_blocks(cfg, item, state)
        result["notes"] = notes
        new_sp, included, dropped = compose.compose(
            own, blks, cfg.get("byte_limit", 10240), cfg.get("byte_reserve", 250))

    result["included"], result["dropped"] = included, dropped
    result["bytes_after"] = compose.rakuten_len(new_sp)
    result["preview"] = new_sp

    if new_sp == sp:
        result["action"] = "unchanged"
        state.upsert_item(mn, own_hash=_hash(own), gen_hash=_hash(new_sp),
                          included=included, dropped=dropped, error=None)
        return result

    can_patch = bool(cfg.get("enabled")) and not cfg.get("dry_run") or force
    if can_patch:
        try:
            rms_items.patch_sp_description(mn, new_sp)
            result["action"] = "patched"
            state.upsert_item(mn, own_hash=_hash(own), gen_hash=_hash(new_sp),
                              included=included, dropped=dropped,
                              patched=True, error=None)
            time.sleep(float(cfg.get("rate_sleep", 0.7)))
        except Exception as e:  # noqa: BLE001
            result["error"] = f"PATCH失敗: {e}"
            result["action"] = "error"
            state.upsert_item(mn, error=result["error"])
    else:
        result["action"] = "would_patch"  # dry-run
        state.upsert_item(mn, own_hash=_hash(own), gen_hash=_hash(new_sp),
                          included=included, dropped=dropped, error=None)
    return result


def run(cfg, state, *, targets=None, remove_all=False, force=False, limit=None):
    """複数商品を処理してサマリを返す。

    targets未指定時はconfig.allowlistを使う。allowlistも空なら安全のため何もしない
    （全商品走査はPhase 4で追加予定）。
    """
    mns = list(targets or cfg.get("allowlist") or [])
    if limit:
        mns = mns[:int(limit)]
    summary = {
        "mode": ("remove_all" if remove_all
                 else ("apply" if (cfg.get("enabled") and not cfg.get("dry_run")) or force
                       else "dry_run")),
        "targets": len(mns), "patched": 0, "would_patch": 0,
        "unchanged": 0, "errors": 0, "results": [],
    }
    if not mns:
        summary["note"] = "対象商品がありません（config.allowlistが空です）"
        return summary
    for mn in mns:
        r = process_item(mn, cfg, state, remove_all=remove_all, force=force)
        summary["results"].append(
            {k: r[k] for k in ("manage_number", "action", "included",
                               "dropped", "error", "bytes_before", "bytes_after")})
        key = {"patched": "patched", "would_patch": "would_patch",
               "unchanged": "unchanged"}.get(r["action"])
        if key:
            summary[key] += 1
        if r["error"]:
            summary["errors"] += 1
    return summary
