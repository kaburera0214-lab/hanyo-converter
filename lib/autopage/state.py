# -*- coding: utf-8 -*-
"""SQLite状態DB。

data/autopage/state.sqlite に、商品ごとの生成ハッシュ・パッチ履歴と、
レビュー/カテゴリのキャッシュを持つ。バッチ実行後にリポジトリへコミットされる。
"""
import datetime
import json
import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = REPO_ROOT / "data" / "autopage" / "state.sqlite"
LOG_DIR = REPO_ROOT / "data" / "autopage" / "logs"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    manage_number TEXT PRIMARY KEY,
    own_hash      TEXT,
    gen_hash      TEXT,
    included      TEXT,
    dropped       TEXT,
    last_patched  TEXT,
    last_seen     TEXT,
    last_error    TEXT
);
CREATE TABLE IF NOT EXISTS reviews (
    manage_number  TEXT PRIMARY KEY,
    review_count   INTEGER,
    review_average REAL,
    fetched_at     TEXT
);
CREATE TABLE IF NOT EXISTS categories (
    manage_number TEXT PRIMARY KEY,
    path_json     TEXT,
    fetched_at    TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class State:
    def __init__(self, db_path=None):
        self.path = Path(db_path or DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(self.path)
        self.con.executescript(_SCHEMA)

    def close(self):
        self.con.commit()
        self.con.close()

    # ---- items ----
    def get_item(self, mn):
        row = self.con.execute(
            "SELECT own_hash, gen_hash, included, dropped, last_patched, "
            "last_seen, last_error FROM items WHERE manage_number=?", (mn,)
        ).fetchone()
        if not row:
            return None
        return {"own_hash": row[0], "gen_hash": row[1],
                "included": json.loads(row[2] or "[]"),
                "dropped": json.loads(row[3] or "[]"),
                "last_patched": row[4], "last_seen": row[5], "last_error": row[6]}

    def upsert_item(self, mn, *, own_hash=None, gen_hash=None, included=None,
                    dropped=None, patched=False, error=None):
        cur = self.get_item(mn) or {}
        self.con.execute(
            "INSERT INTO items (manage_number, own_hash, gen_hash, included, "
            "dropped, last_patched, last_seen, last_error) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(manage_number) DO UPDATE SET own_hash=excluded.own_hash, "
            "gen_hash=excluded.gen_hash, included=excluded.included, "
            "dropped=excluded.dropped, last_patched=excluded.last_patched, "
            "last_seen=excluded.last_seen, last_error=excluded.last_error",
            (mn,
             own_hash if own_hash is not None else cur.get("own_hash"),
             gen_hash if gen_hash is not None else cur.get("gen_hash"),
             json.dumps(included if included is not None else cur.get("included", []),
                        ensure_ascii=False),
             json.dumps(dropped if dropped is not None else cur.get("dropped", []),
                        ensure_ascii=False),
             _now() if patched else cur.get("last_patched"),
             _now(),
             error))
        self.con.commit()

    def summary(self):
        row = self.con.execute(
            "SELECT COUNT(*), SUM(CASE WHEN last_patched IS NOT NULL THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN last_error IS NOT NULL THEN 1 ELSE 0 END) FROM items"
        ).fetchone()
        return {"items": row[0] or 0, "patched": row[1] or 0, "errors": row[2] or 0}

    # ---- reviews ----
    def get_review(self, mn, max_age_days=None):
        row = self.con.execute(
            "SELECT review_count, review_average, fetched_at FROM reviews "
            "WHERE manage_number=?", (mn,)).fetchone()
        if not row:
            return None
        if max_age_days is not None and row[2]:
            fetched = datetime.datetime.strptime(row[2], "%Y-%m-%d %H:%M:%S")
            if (datetime.datetime.now() - fetched).days > max_age_days:
                return None
        return {"review_count": row[0] or 0, "review_average": row[1] or 0}

    def set_review(self, mn, review_count, review_average):
        self.con.execute(
            "INSERT INTO reviews VALUES (?,?,?,?) ON CONFLICT(manage_number) "
            "DO UPDATE SET review_count=excluded.review_count, "
            "review_average=excluded.review_average, fetched_at=excluded.fetched_at",
            (mn, review_count, review_average, _now()))
        self.con.commit()

    # ---- categories ----
    def get_categories(self, mn, max_age_days=None):
        row = self.con.execute(
            "SELECT path_json, fetched_at FROM categories WHERE manage_number=?",
            (mn,)).fetchone()
        if not row:
            return None
        if max_age_days is not None and row[1]:
            fetched = datetime.datetime.strptime(row[1], "%Y-%m-%d %H:%M:%S")
            if (datetime.datetime.now() - fetched).days > max_age_days:
                return None
        return json.loads(row[0] or "[]")

    def set_categories(self, mn, categories):
        self.con.execute(
            "INSERT INTO categories VALUES (?,?,?) ON CONFLICT(manage_number) "
            "DO UPDATE SET path_json=excluded.path_json, fetched_at=excluded.fetched_at",
            (mn, json.dumps(categories, ensure_ascii=False), _now()))
        self.con.commit()


def write_run_log(result):
    """実行サマリを data/autopage/logs/latest.json と日付付きファイルに書く。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    body = json.dumps(result, ensure_ascii=False, indent=2)
    (LOG_DIR / f"run_{ts}.json").write_text(body, encoding="utf-8")
    (LOG_DIR / "latest.json").write_text(body, encoding="utf-8")


def read_latest_log():
    p = LOG_DIR / "latest.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return None
