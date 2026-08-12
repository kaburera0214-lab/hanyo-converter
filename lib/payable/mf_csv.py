# -*- coding: utf-8 -*-
"""
MFクラウド会計 仕訳インポート用CSVの生成（買掛・支払まわり）。

MFの仕訳帳インポート仕様（公式サポート）:
  - 取引Noは「9桁以内の数字」→ 本システムは 1万台＋MMDD の5桁を使う
    （例: 振込実行が2026/08/31なら前月末日07/31 → 取引No 10731）
  - 必須項目は 取引日・勘定科目・金額
  - 複合仕訳は「取引No」「取引日」が同一の行が結合される
    （運用中のシートに合わせ、既定では1行目にだけ取引No・取引日を入れる）

対象CSV:
  1) 買掛未払CSV     … 当月発生分の計上（借方 仕入高等 / 貸方 買掛金・未払金）
  2) 総合振込仕訳帳CSV … 振込実行分の支払（未実装）
"""
import csv
import io

# 買掛未払CSVの列（MFの仕訳帳インポート様式）
KAIKAKE_HEADER = ["取引No", "取引日", "借方勘定科目", "借方補助科目", "借方税区分",
                  "借方部門", "借方金額(円)", "貸方勘定科目", "貸方補助科目", "貸方税区分",
                  "貸方部門", "貸方金額(円)", "摘要", "タグ", "メモ"]

# 取引先マスタ側で持つMF仕訳の項目（[マスタ]買掛未払.csv から取り込む）
MF_MASTER_FIELDS = ["借方勘定科目", "借方補助科目", "借方税区分",
                    "貸方勘定科目", "貸方補助科目", "貸方税区分", "摘要"]


def month_end(year, month):
    """指定年月の末日を date で返す。"""
    import calendar
    import datetime
    return datetime.date(year, month, calendar.monthrange(year, month)[1])


def prev_month_end(d):
    """指定日の前月末日。2026-08-31 → 2026-07-31。"""
    import datetime
    if isinstance(d, str):
        s = d.replace("/", "-").strip()
        d = datetime.date.fromisoformat(s)
    first = d.replace(day=1)
    return first - datetime.timedelta(days=1)


def torihiki_no(d, prefix=1):
    """取引日から取引No（1万台＋MMDD の5桁）を作る。2026/07/31 → '10731'。"""
    return f"{prefix}{d.month:02d}{d.day:02d}"


def yen(v):
    """MFの金額表記（カンマ区切り）。マイナス（赤伝）もそのまま。"""
    try:
        n = int(round(float(str(v).replace(",", ""))))
    except (TypeError, ValueError):
        n = 0
    return f"{n:,}"


def read_mf_master_csv(file_or_bytes):
    """
    [マスタ]買掛未払.csv（MFの仕訳ひな形）を読み、取引先ごとの勘定科目設定を返す。
    摘要＝取引先名として扱い、シート上の並び順も保持する（CSVの行順を再現するため）。
    戻り値: [{"摘要":.., "借方勘定科目":.., ..., "MF並び順": n}, ...]
    """
    if isinstance(file_or_bytes, (bytes, bytearray)):
        raw = bytes(file_or_bytes)
    elif hasattr(file_or_bytes, "read"):
        raw = file_or_bytes.read()
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
    else:
        with open(file_or_bytes, "rb") as fp:
            raw = fp.read()
    text = None
    for enc in ("utf-8-sig", "utf-8", "cp932"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("cp932", errors="replace")
    rows, order = [], 0
    for row in csv.DictReader(io.StringIO(text)):
        clean = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
        name = clean.get("摘要", "")
        if not name:
            continue
        order += 1
        rec = {"摘要": name, "MF並び順": order}
        for f in MF_MASTER_FIELDS:
            if f != "摘要":
                rec[f] = clean.get(f, "")
        rows.append(rec)
    return rows


def build_kaikake_csv(records, torihiki_no_value, torihiki_date, every_row=False,
                      encoding="utf-8"):
    """
    買掛未払CSV（MF仕訳インポート）のバイト列を作る。

    records : [{"借方勘定科目","借方補助科目","借方税区分","貸方勘定科目",
                "貸方補助科目","貸方税区分","摘要","金額"}]
    torihiki_no_value : 取引No（例 '10731'）
    torihiki_date     : 取引日（'2026/07/31' 形式の文字列 or date）
    every_row : True なら全行に取引No・取引日を出力（既定は1行目のみ＝運用シートと同じ）
    """
    if hasattr(torihiki_date, "strftime"):
        torihiki_date = torihiki_date.strftime("%Y/%m/%d")
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(KAIKAKE_HEADER)
    for i, r in enumerate(records):
        amount = yen(r.get("金額", 0))
        first = (i == 0) or every_row
        w.writerow([
            str(torihiki_no_value) if first else "",
            torihiki_date if first else "",
            r.get("借方勘定科目", ""), r.get("借方補助科目", ""), r.get("借方税区分", ""),
            "", amount,
            r.get("貸方勘定科目", ""), r.get("貸方補助科目", ""), r.get("貸方税区分", ""),
            "", amount,
            r.get("摘要", ""), "", "",
        ])
    return buf.getvalue().encode(encoding, errors="replace")
