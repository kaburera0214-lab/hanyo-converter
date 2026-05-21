import base64
import csv
import io
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
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

MASTER_PATH  = Path(__file__).parent / "master.csv"
KOGUCHI_PATH = Path(__file__).parent / "koguchimaster.csv"


# ── ユーティリティ ─────────────────────────────────────────
def to_int(val):
    try:
        return int(str(val).strip()) if val is not None and str(val).strip() else 0
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


# ── 個口数マスタ ──────────────────────────────────────────
@st.cache_data(show_spinner="個口数マスタを読み込み中...")
def load_koguchi_from_file():
    """戻り値: {jan: [(下限, 上限, 個口数), ...]} 下限昇順。上限0=無制限。個口数は int or '宅配'。"""
    if not KOGUCHI_PATH.exists():
        return {}
    master = {}
    with open(KOGUCHI_PATH, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            jan   = (row.get("JANコード") or row.get("SKUコード") or "").strip()
            lower = to_int(row.get("数量（下限）") or row.get("数量") or "")
            upper = to_int(row.get("数量（上限）", "") or "")
            raw   = str(row.get("個口数", "") or "").strip()
            koguchi = "宅配" if raw == "宅配" else to_int(raw)
            if jan and lower and koguchi:
                master.setdefault(jan, []).append((lower, upper, koguchi))
    for jan in master:
        master[jan].sort(key=lambda x: x[0])
    return master


def koguchi_to_df(master):
    """個口数マスタ dict → DataFrame"""
    rows = []
    for jan, entries in master.items():
        for lower, upper, koguchi in entries:
            rows.append({
                "JANコード":    jan,
                "数量（下限）": lower,
                "数量（上限）": upper if upper else None,
                "個口数":       str(koguchi),
            })
    cols = ["JANコード", "数量（下限）", "数量（上限）", "個口数"]
    return pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)


def df_to_koguchi(df):
    """DataFrame → 個口数マスタ dict"""
    master = {}
    for _, row in df.iterrows():
        jan       = str(row.get("JANコード", "") or "").strip()
        lower     = to_int(row.get("数量（下限）"))
        upper     = to_int(row.get("数量（上限）") or 0)
        raw       = str(row.get("個口数", "") or "").strip()
        koguchi   = "宅配" if raw == "宅配" else to_int(raw)
        if jan and lower and koguchi:
            master.setdefault(jan, []).append((lower, upper, koguchi))
    for jan in master:
        master[jan].sort(key=lambda x: x[0])
    return master


def load_koguchi_from_csv_bytes(file_bytes):
    """アップロードされたCSVバイト列から個口数マスタを読み込む。UTF-8/Shift-JIS 自動判定。"""
    text = None
    for enc in ("utf-8-sig", "utf-8", "shift_jis"):
        try:
            text = file_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return None, "文字コードを判定できませんでした（UTF-8 または Shift-JIS で保存してください）"

    reader = csv.DictReader(io.StringIO(text))
    cols = reader.fieldnames or []
    jan_col     = next((c for c in cols if "JAN" in c or "ＪＡＮ" in c or "SKU" in c), None)
    lower_col   = next((c for c in cols if "下限" in c or c.strip() == "数量"), None)
    upper_col   = next((c for c in cols if "上限" in c), None)
    koguchi_col = next((c for c in cols if "個口" in c), None)

    if not (jan_col and lower_col and koguchi_col):
        missing = [n for n, c in [("JANコード", jan_col), ("数量（下限）", lower_col), ("個口数", koguchi_col)] if not c]
        return None, f"必要なカラムが見つかりません: {', '.join(missing)}"

    master = {}
    for row in reader:
        jan     = (row.get(jan_col) or "").strip()
        lower   = to_int(row.get(lower_col) or "")
        upper   = to_int(row.get(upper_col, "") or "") if upper_col else 0
        raw     = str(row.get(koguchi_col, "") or "").strip()
        koguchi = "宅配" if raw == "宅配" else to_int(raw)
        if jan and lower and koguchi:
            master.setdefault(jan, []).append((lower, upper, koguchi))
    for jan in master:
        master[jan].sort(key=lambda x: x[0])

    if not master:
        return None, "有効なデータが見つかりませんでした"
    return master, None


def save_koguchi_to_github(master):
    """個口数マスタをGitHubリポジトリに直接コミットする。"""
    token = st.secrets.get("GITHUB_TOKEN", "")
    repo  = st.secrets.get("GITHUB_REPO", "")
    if not token or not repo:
        return False, "GITHUB_TOKEN / GITHUB_REPO がSecretsに設定されていません"

    rows = []
    for jan, entries in master.items():
        for lower, upper, koguchi in sorted(entries):
            rows.append({
                "JANコード":    jan,
                "数量（下限）": lower,
                "数量（上限）": upper if upper else "",
                "個口数":       koguchi,
            })
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["JANコード", "数量（下限）", "数量（上限）", "個口数"])
    writer.writeheader()
    writer.writerows(rows)
    content_bytes = buf.getvalue().encode("utf-8")

    url     = f"https://api.github.com/repos/{repo}/contents/koguchimaster.csv"
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}

    r = requests.get(url, headers=headers)
    sha = r.json().get("sha") if r.ok else None

    payload = {
        "message": "Update koguchimaster.csv via app",
        "content": base64.b64encode(content_bytes).decode(),
    }
    if sha:
        payload["sha"] = sha

    r = requests.put(url, json=payload, headers=headers)
    if r.ok:
        load_koguchi_from_file.clear()
        return True, None
    return False, r.json().get("message", "不明なエラー")


def calc_koguchi(items, koguchi_master):
    """None / int / '宅配' を返す。複数アイテムで1つでも宅配なら宅配優先。
    複数SKUが混在する注文は個口数判定対象外。"""
    if not koguchi_master:
        return None
    # 複数SKU混在の注文はスキップ
    distinct_skus = {item.get("SKUコード", "").strip() for item in items}
    if len(distinct_skus) > 1:
        return None
    total = 0
    found_any = False
    is_takuhai = False
    for item in items:
        jan     = item.get("SKUコード", "").strip()
        qty     = to_int(item.get("注文個数", ""))
        entries = koguchi_master.get(jan)
        if entries is None:
            continue
        found_any = True
        for lower, upper, k in entries:
            if lower <= qty and (upper == 0 or qty <= upper):
                if k == "宅配":
                    is_takuhai = True
                else:
                    total += k
                break
    if not found_any:
        return None
    if is_takuhai:
        return "宅配"
    if total <= 1:
        return None
    return total


# ── 変換処理 ─────────────────────────────────────────────
def convert(order_bytes, master, koguchi_master):
    text = order_bytes.decode("shift_jis", errors="replace")
    all_rows = [r for r in csv.DictReader(io.StringIO(text)) if any(r.values())]

    orders = OrderedDict()
    for row in all_rows:
        oid = row["モール注文番号"].strip()
        orders.setdefault(oid, []).append(row)

    today = today_midnight()
    output_rows = []
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

        worker_note = order_datetime(first.get("モール注文日時", ""))
        koguchi = calc_koguchi(items, koguchi_master)
        if koguchi == "宅配":
            worker_note += "【宅配変更】"
        elif koguchi:
            worker_note += f"【個口数{koguchi}】"

        for item in items:
            jan  = item.get("SKUコード", "").strip()
            prod = master.get(jan, {})
            if not prod:
                not_found.setdefault(jan, []).append(order_id)

            row = {
                "店舗伝票番号":       order_id,
                "受注日":             today,
                "受注郵便番号":       recv["郵便番号"],
                "受注住所１":         recv["住所１"],
                "受注住所２":         recv["住所２"],
                "受注名":             recv["名前"],
                "受注名カナ":         "",
                "受注電話番号":       recv["電話"],
                "受注メールアドレス": "",
                "発送郵便番号":       first.get("送付先郵便番号", ""),
                "発送先住所１":       ship_addr1,
                "発送先住所２":       ship_addr2,
                "発送先名":           first.get("送付先氏名", ""),
                "発送先カナ":         "",
                "発送電話番号":       first.get("送付先電話番号", ""),
                "支払方法":           "その他",
                "発送方法":           "ヤマト運輸",
                "商品計":             item_total or "",
                "税金":               tax or "",
                "発送料":             shipping or "",
                "手数料":             fee or "",
                "ポイント":           points or "",
                "その他費用":         coupon or "",
                "合計金額":           total or "",
                "ギフトフラグ":       "",
                "時間帯指定":         first.get("お届け指定時間帯", "").strip().lstrip("'"),
                "日付指定":           "",
                "作業者欄":           worker_note,
                "備考":               "",
                "商品名":             prod.get("商品名") or item.get("商品名", ""),
                "商品コード":         prod.get("商品コード") or jan,
                "商品価格":           item.get("商品単価", ""),
                "受注数量":           item.get("注文個数", ""),
                "商品オプション":     "",
                "出荷済フラグ":       "",
                "顧客区分":           "",
                "顧客コード":         "",
                "消費税率（%）":      item.get("税率", ""),
            }
            output_rows.append(row)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=OUT_HEADERS)
    writer.writeheader()
    writer.writerows(output_rows)
    csv_bytes = buf.getvalue().encode("shift_jis", errors="replace")

    return csv_bytes, len(orders), len(output_rows), not_found


# ── 出荷実績CSV生成 ──────────────────────────────────────
def convert_shipment(ne_bytes):
    text = ne_bytes.decode("cp932", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    cols = reader.fieldnames or []

    order_col    = next((c for c in cols if "受注番号" in c), None)
    method_col   = next((c for c in cols if "発送方法" in c), None)
    tracking_col = next((c for c in cols if "発送伝票番号" in c), None)

    missing = [n for n, c in [("受注番号", order_col), ("発送方法", method_col), ("発送伝票番号", tracking_col)] if not c]
    if missing:
        return None, 0, f"必要なカラムが見つかりません: {', '.join(missing)}"

    rows = []
    for row in reader:
        order_num = row[order_col].strip()
        if not order_num:
            continue
        raw_method   = row[method_col].strip()
        method       = "ネコポス" if "ネコポス" in raw_method else "ヤマト"
        tracking_raw = row[tracking_col].strip()
        tracking     = tracking_raw.split(",")[0].strip() if tracking_raw else ""
        rows.append({"モール注文番号": order_num, "配送方法": method, "送り状番号": tracking})

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["モール注文番号", "配送方法", "送り状番号"])
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8-sig"), len(rows), None


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

    # ── 商品マスタの読み込み ──────────────────────────────
    if "master" not in st.session_state:
        master, err = load_master_from_file()
        if err:
            st.session_state["master"] = None
            st.session_state["master_info"] = "未読み込み"
        else:
            st.session_state["master"] = master
            mtime = Path(MASTER_PATH).stat().st_mtime
            dt = datetime.fromtimestamp(mtime).strftime("%Y/%m/%d")
            st.session_state["master_info"] = f"{len(master):,} 件（更新日: {dt}）"

    # ── 個口数マスタの読み込み ────────────────────────────
    if "koguchi_master" not in st.session_state:
        km = load_koguchi_from_file()
        st.session_state["koguchi_master"] = km

    # ── サイドバー ────────────────────────────────────────
    with st.sidebar:
        # 商品マスタ
        st.header("⚙️ 商品マスタ")
        if st.session_state.get("master"):
            st.success(st.session_state.get("master_info", "読み込み済み"))
        else:
            st.error("マスタ未読み込み")
        st.caption("マスタを更新する場合のみアップロード")
        new_master = st.file_uploader("新しい商品マスタCSV（Shift-JIS）", type="csv", key="up_master")
        if new_master:
            master, err = load_master_from_upload(new_master.read())
            if err:
                st.error(err)
            else:
                st.session_state["master"] = master
                st.session_state["master_info"] = f"{len(master):,} 件（今回アップロード）"
                st.success(f"更新しました：{len(master):,} 件")

        st.divider()

        # 個口数マスタ
        st.header("📦 個口数マスタ")
        km = st.session_state.get("koguchi_master", {})
        rule_count = sum(len(v) for v in km.values())
        st.caption(f"現在 {rule_count} ルール登録済み")

        koguchi_csv = st.file_uploader(
            "CSVで一括更新",
            type="csv",
            key="up_koguchi",
            help="JANコード・数量（下限）・数量（上限）・個口数 の列を含むCSV",
        )
        if koguchi_csv:
            new_km, err = load_koguchi_from_csv_bytes(koguchi_csv.read())
            if err:
                st.error(err)
            else:
                st.session_state["koguchi_master"] = new_km
                km = new_km
                total_rules = sum(len(v) for v in new_km.values())
                st.success(f"読み込み完了：{total_rules} ルール　→ 下のボタンで保存してください")

        df = koguchi_to_df(km)
        edited_df = st.data_editor(
            df,
            num_rows="dynamic",
            use_container_width=True,
            column_config={
                "JANコード":    st.column_config.TextColumn("JANコード",    width="small"),
                "数量（下限）": st.column_config.NumberColumn("数量（下限）", width="small", min_value=1, step=1),
                "数量（上限）": st.column_config.NumberColumn("数量（上限）", width="small", min_value=0, step=1,
                                help="空白 or 0 = 上限なし"),
                "個口数":       st.column_config.TextColumn("個口数",        width="small", help="数字 or 宅配"),
            },
            key="koguchi_editor",
        )

        if st.button("💾 GitHubに保存", type="primary", use_container_width=True):
            new_km = df_to_koguchi(edited_df)
            ok, err = save_koguchi_to_github(new_km)
            if ok:
                st.session_state["koguchi_master"] = new_km
                st.success("保存しました")
            else:
                st.error(f"保存失敗: {err}")

    # ── タブ ──────────────────────────────────────────────
    tab1, tab2 = st.tabs(["① 汎用マスタCSV変換", "② 出荷実績CSV生成"])

    with tab1:
        st.caption("先方受注CSV → ネクストエンジン汎用マスタCSV")
        st.divider()
        st.link_button(
            "📂 受注CSVフォルダを開く（Google Drive）",
            "https://drive.google.com/drive/u/2/folders/1Xil5jgZvxk3A-3s-W-7eYrcMu4R8qvQX",
            use_container_width=True,
        )
        st.subheader("受注CSV")
        order_file = st.file_uploader("先方からの受注ファイル", type="csv", key="order_upload")
        st.divider()

        master         = st.session_state.get("master")
        koguchi_master = st.session_state.get("koguchi_master", {})
        can_convert    = order_file is not None and master is not None

        if not master:
            st.error("商品マスタが読み込まれていません。サイドバーからアップロードしてください。")

        if st.button("🔄 変換する", type="primary", disabled=not can_convert, key="btn_convert"):
            with st.spinner("変換中..."):
                csv_bytes, n_orders, n_rows, not_found = convert(order_file.read(), master, koguchi_master)

            if not_found:
                st.error(f"⚠️ 商品マスタに存在しないJANコードがあります（{len(not_found)} 件）")
                for jan, orders in not_found.items():
                    st.markdown(f"- **JAN: `{jan}`** → 注文番号: {', '.join(orders)}")
                st.warning("上記の商品は入力値のまま出力されています。商品マスタを最新版に更新してください。")
                st.divider()

            st.success(f"変換完了：{n_orders} 件の注文 / {n_rows} 行")
            today = datetime.now().strftime("%Y%m%d")
            st.download_button(
                label="⬇️ 変換済みCSVをダウンロード",
                data=csv_bytes,
                file_name=f"hanyo_master_{today}.csv",
                mime="text/csv",
                key="dl_hanyo",
            )
            st.link_button(
                "📋 ネクストエンジンに登録する →",
                "https://main.next-engine.com/Usercsv",
                use_container_width=True,
            )

    with tab2:
        st.caption("NEの出荷完了CSV → 先方への出荷実績CSV")
        st.divider()
        st.subheader("NE出荷完了CSV")
        ne_file = st.file_uploader(
            "ネクストエンジンからダウンロードした出荷完了CSV（Shift-JIS）",
            type="csv",
            key="ne_upload",
        )
        st.divider()

        if st.button("🔄 生成する", type="primary", disabled=ne_file is None, key="btn_shipment"):
            with st.spinner("生成中..."):
                csv_bytes, n_rows, err = convert_shipment(ne_file.read())
            if err:
                st.error(err)
            else:
                st.success(f"生成完了：{n_rows} 件")
                today = datetime.now().strftime("%Y%m%d")
                st.download_button(
                    label="⬇️ 出荷実績CSVをダウンロード",
                    data=csv_bytes,
                    file_name=f"{today}_[出荷代行]出荷実績.csv",
                    mime="text/csv",
                    key="dl_shipment",
                )
                st.link_button(
                    "📂 格納フォルダを開く（Google Drive）",
                    "https://drive.google.com/drive/u/2/folders/1jhsvohRf7FLg3vrj8A-8qyEzQlqsYXrJ",
                    use_container_width=True,
                )


if __name__ == "__main__":
    main()
