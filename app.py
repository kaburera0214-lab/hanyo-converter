import csv
import io
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

import streamlit as st

# ── Team-EC 固定受注者情報 ────────────────────────────────
TEAMEC = {
    "郵便番号": "192-0052",
    "住所１":   "東京都八王子市本郷町10-6　LC BLDG.",
    "住所２":   "",
    "名前":     "Team-EC株式会社",
    "電話":     "042-686-2176",
}

OUT_HEADERS = [
    "店舗伝票番号", "受注日", "受注郵便番号", "受注住所１", "受注住所２",
    "受注名", "受注名カナ", "受注電話番号", "受注メールアドレス",
    "発送郵便番号", "発送先住所１", "発送先住所２", "発送先名", "発送先カナ", "発送電話番号",
    "支払方法", "発送方法",
    "商品計", "税金", "発送料", "手数料", "ポイント", "その他費用", "合計金額",
    "ギフトフラグ", "時間帯指定", "日付指定", "作業者欄", "備考",
    "商品名", "商品コード", "商品価格", "受注数量", "商品オプション",
    "出荷済フラグ", "顧客区分", "顧客コード", "消費税率（%）",
]

MASTER_PATH = Path(__file__).parent / "master.csv"


# ── ユーティリティ ─────────────────────────────────────────
def to_int(val):
    try:
        return int(val.strip()) if val and val.strip() else 0
    except (ValueError, AttributeError):
        return 0


def today_midnight():
    d = datetime.now()
    return f"{d.year}/{d.month}/{d.day} 0:00:00"


def order_datetime(raw):
    raw = raw.strip()
    if not raw:
        return ""
    try:
        dt = datetime.strptime(raw, "%Y/%m/%d %H:%M")
        return f"{dt.year}/{dt.month}/{dt.day} {dt.hour}:{dt.minute:02d}"
    except ValueError:
        return raw


def delivery_note(first):
    for key in ("カスタマー備考", "倉庫備考", "配送備考"):
        v = first.get(key, "").strip()
        if v:
            return v
    return ""


def receiver_info(first):
    orderer = first.get("注文者氏名", "").strip()
    ship_to = first.get("送付先氏名", "").strip()
    if orderer == ship_to:
        return {
            "郵便番号": TEAMEC["郵便番号"],
            "住所１":   TEAMEC["住所１"],
            "住所２":   TEAMEC["住所２"],
            "名前":     TEAMEC["名前"],
            "電話":     TEAMEC["電話"],
        }
    addr1 = first.get("都道府県", "") + first.get("市区町村", "") + first.get("町名・番地以降", "")
    return {
        "郵便番号": first.get("注文者郵便番号", ""),
        "住所１":   addr1,
        "住所２":   "",
        "名前":     orderer,
        "電話":     first.get("注文者電話番号", ""),
    }


# ── 商品マスタ読み込み ────────────────────────────────────
@st.cache_data(show_spinner="商品マスタを読み込み中...")
def load_master_from_file():
    """リポジトリ内の master.csv を読み込む（UTF-8）。"""
    if not MASTER_PATH.exists():
        return None, "master.csv が見つかりません"
    master = {}
    with open(MASTER_PATH, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            jan = row.get("JANコード", "").strip()
            if jan:
                master[jan] = {
                    "商品コード": row.get("商品コード", "").strip(),
                    "商品名":     row.get("商品名", "").strip(),
                }
    return master, None


def load_master_from_upload(file_bytes):
    """アップロードされた商品マスタCSV（Shift-JIS）を読み込む。"""
    text = file_bytes.decode("shift_jis", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    cols = reader.fieldnames or []
    jan_col  = next((c for c in cols if "JAN" in c or "ＪＡＮ" in c), None)
    code_col = next((c for c in cols if "商品コード" in c), None)
    name_col = next((c for c in cols if "商品名" in c and "英語" not in c), None)
    if not (jan_col and code_col and name_col):
        return None, "商品マスタのカラムが見つかりません"
    master = {}
    for row in reader:
        jan = row[jan_col].strip()
        if jan:
            master[jan] = {
                "商品コード": row[code_col].strip(),
                "商品名":     row[name_col].strip(),
            }
    return master, None


# ── 変換処理 ─────────────────────────────────────────────
def convert(order_bytes, master):
    text = order_bytes.decode("shift_jis", errors="replace")
    all_rows = [r for r in csv.DictReader(io.StringIO(text)) if any(r.values())]

    orders = OrderedDict()
    for row in all_rows:
        oid = row["モール注文番号"].strip()
        orders.setdefault(oid, []).append(row)

    today = today_midnight()
    output_rows = []
    # 未マッチ: {JANコード: [注文番号, ...]}
    not_found = {}

    for order_id, items in orders.items():
        first = items[0]

        item_total = sum(to_int(r["商品単価"]) * to_int(r["注文個数"]) for r in items)
        tax        = sum(to_int(r.get("消費税", "")) for r in items)
        shipping   = to_int(first.get("配送料", ""))
        fee        = to_int(first.get("決済手数料", ""))
        points     = to_int(first.get("ポイント利用額", ""))
        coupon     = to_int(first.get("クーポン利用額", ""))
        wrapping   = to_int(first.get("ラッピング手数料", ""))
        total      = item_total + tax + shipping + fee + wrapping - points - coupon

        recv       = receiver_info(first)
        ship_addr1 = first.get("送付先都道府県", "") + first.get("送付先市区町村", "") + first.get("送付先町名・番地以降", "")
        ship_addr2 = delivery_note(first)

        for item in items:
            jan = item.get("SKUコード", "").strip()
            prod = master.get(jan, {})
            if not prod:
                not_found.setdefault(jan, []).append(order_id)

            row = {
                "店舗伝票番号":     order_id,
                "受注日":           today,
                "受注郵便番号":     recv["郵便番号"],
                "受注住所１":       recv["住所１"],
                "受注住所２":       recv["住所２"],
                "受注名":           recv["名前"],
                "受注名カナ":       "",
                "受注電話番号":     recv["電話"],
                "受注メールアドレス": "",
                "発送郵便番号":     first.get("送付先郵便番号", ""),
                "発送先住所１":     ship_addr1,
                "発送先住所２":     ship_addr2,
                "発送先名":         first.get("送付先氏名", ""),
                "発送先カナ":       "",
                "発送電話番号":     first.get("送付先電話番号", ""),
                "支払方法":         "その他",
                "発送方法":         "ヤマト運輸",
                "商品計":           item_total or "",
                "税金":             tax or "",
                "発送料":           shipping or "",
                "手数料":           fee or "",
                "ポイント":         points or "",
                "その他費用":       coupon or "",
                "合計金額":         total or "",
                "ギフトフラグ":     "",
                "時間帯指定":       first.get("お届け指定時間帯", "").strip(),
                "日付指定":         "",
                "作業者欄":         order_datetime(first.get("モール注文日時", "")),
                "備考":             "",
                "商品名":           prod.get("商品名") or item.get("商品名", ""),
                "商品コード":       prod.get("商品コード") or jan,
                "商品価格":         item.get("商品単価", ""),
                "受注数量":         item.get("注文個数", ""),
                "商品オプション":   "",
                "出荷済フラグ":     "",
                "顧客区分":         "",
                "顧客コード":       "",
                "消費税率（%）":    item.get("税率", ""),
            }
            output_rows.append(row)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=OUT_HEADERS)
    writer.writeheader()
    writer.writerows(output_rows)
    csv_bytes = buf.getvalue().encode("shift_jis", errors="replace")

    return csv_bytes, len(orders), len(output_rows), not_found


# ── パスワード認証 ────────────────────────────────────────
def check_password():
    if st.session_state.get("authenticated"):
        return True
    st.title("🔐 ログイン")
    pw = st.text_input("パスワード", type="password")
    if st.button("ログイン"):
        if pw == st.secrets.get("APP_PASSWORD", ""):
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("パスワードが違います")
    return False


# ── メイン画面 ───────────────────────────────────────────
def main():
    st.set_page_config(page_title="汎用マスタCSV変換", page_icon="📦", layout="centered")

    if not check_password():
        return

    st.title("📦 汎用マスタCSV変換ツール")
    st.caption("先方受注CSV → ネクストエンジン汎用マスタCSV")

    # ── 商品マスタの読み込み ──────────────────────────────
    # セッション内にキャッシュされたマスタを優先
    if "master" not in st.session_state:
        master, err = load_master_from_file()
        if err:
            st.session_state["master"] = None
            st.session_state["master_info"] = "未読み込み"
        else:
            st.session_state["master"] = master
            import os
            mtime = Path(MASTER_PATH).stat().st_mtime
            dt = datetime.fromtimestamp(mtime).strftime("%Y/%m/%d")
            st.session_state["master_info"] = f"{len(master):,} 件（更新日: {dt}）"

    # ── サイドバー：マスタ更新 ────────────────────────────
    with st.sidebar:
        st.header("⚙️ 商品マスタ")
        if st.session_state.get("master"):
            st.success(st.session_state.get("master_info", "読み込み済み"))
        else:
            st.error("マスタ未読み込み")

        st.caption("マスタを更新する場合のみアップロード")
        new_master = st.file_uploader("新しい商品マスタCSV（Shift-JIS）", type="csv")
        if new_master:
            master, err = load_master_from_upload(new_master.read())
            if err:
                st.error(err)
            else:
                st.session_state["master"] = master
                st.session_state["master_info"] = f"{len(master):,} 件（今回アップロード）"
                st.success(f"更新しました：{len(master):,} 件")

    st.divider()

    # ── 受注CSV ──────────────────────────────────────────
    st.subheader("① 受注CSV")
    order_file = st.file_uploader("先方からの受注ファイル", type="csv")

    st.divider()

    # ── 変換ボタン ────────────────────────────────────────
    master = st.session_state.get("master")
    can_convert = order_file is not None and master is not None

    if not master:
        st.error("商品マスタが読み込まれていません。サイドバーからアップロードしてください。")

    if st.button("🔄 変換する", type="primary", disabled=not can_convert):
        with st.spinner("変換中..."):
            csv_bytes, n_orders, n_rows, not_found = convert(order_file.read(), master)

        # ── エラー表示 ────────────────────────────────────
        if not_found:
            st.error(f"⚠️ 商品マスタに存在しないJANコードがあります（{len(not_found)} 件）")
            for jan, orders in not_found.items():
                st.markdown(f"- **JAN: `{jan}`** → 注文番号: {', '.join(orders)}")
            st.warning("上記の商品は入力値のまま出力されています。商品マスタを最新版に更新してください。")
            st.divider()

        # ── 成功・ダウンロード ────────────────────────────
        st.success(f"変換完了：{n_orders} 件の注文 / {n_rows} 行")
        today = datetime.now().strftime("%Y%m%d")
        st.download_button(
            label="⬇️ 変換済みCSVをダウンロード",
            data=csv_bytes,
            file_name=f"hanyo_master_{today}.csv",
            mime="text/csv",
        )


if __name__ == "__main__":
    main()
