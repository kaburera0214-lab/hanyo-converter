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
  2) 総合振込仕訳帳CSV … 振込実行分の支払（借方 買掛金等の取り崩し / 貸方 仮受金＝振込総額）

①と②の関係（受領サンプルで検証済み。tests/fixtures/ に現物あり）:
  ②の借方は、①の貸方（買掛金・未払金）をそのまま反転したもの。
  マスタに②専用の科目列は要らず、貸方列を借方へ写せばよい。
  貸方勘定科目が空の取引先は「計上せず支払時に費用計上する」型なので、借方列を使う。
  源泉徴収がある取引先は、貸方に 預り金/所得税 の行が別途1本立ち、
  借方金額 = 振込額 + 源泉税額 になる（振込は源泉を差し引いた額で行うため）。
"""
import csv
import io

# 買掛未払CSVの列（MFの仕訳帳インポート様式）
KAIKAKE_HEADER = ["取引No", "取引日", "借方勘定科目", "借方補助科目", "借方税区分",
                  "借方部門", "借方金額(円)", "貸方勘定科目", "貸方補助科目", "貸方税区分",
                  "貸方部門", "貸方金額(円)", "摘要", "タグ", "メモ"]

# 総合振込仕訳帳CSVの列。①と違い「メモ」列が無い14列（受領サンプルに合わせる）
SOUFURIKOMI_HEADER = KAIKAKE_HEADER[:-1]

# ②の固定値（貸方の振込総額行と、源泉徴収の預り金行）
FURIKOMI_KASHIKATA = {"勘定科目": "仮受金", "補助科目": "", "税区分": "対象外"}
FURIKOMI_TEKIYO = "総合振込"
GENSEN_KASHIKATA = {"勘定科目": "預り金", "補助科目": "所得税", "税区分": "対象外"}

# 取引先マスタ側で持つMF仕訳の項目（[マスタ]買掛未払.csv から取り込む）
MF_MASTER_FIELDS = ["借方勘定科目", "借方補助科目", "借方税区分",
                    "貸方勘定科目", "貸方補助科目", "貸方税区分", "摘要"]


def soufurikomi_karikata(master_row):
    """
    ②総合振込仕訳帳の借方（勘定科目・補助科目・税区分）を取引先マスタから決める。

    貸方勘定科目あり → 計上済みの買掛金・未払金を取り崩す（①の貸方をそのまま反転）
    貸方勘定科目なし → 計上していないので支払時に費用計上する（①の借方を使う）
    どちらも無い     → None（呼び出し側で「マスタに勘定科目が未設定」として弾く）
    """
    if str(master_row.get("貸方勘定科目", "")).strip():
        return {"借方勘定科目": master_row.get("貸方勘定科目", ""),
                "借方補助科目": master_row.get("貸方補助科目", ""),
                "借方税区分": master_row.get("貸方税区分", "")}
    if str(master_row.get("借方勘定科目", "")).strip():
        return {"借方勘定科目": master_row.get("借方勘定科目", ""),
                "借方補助科目": master_row.get("借方補助科目", ""),
                "借方税区分": master_row.get("借方税区分", "")}
    return None


def build_soufurikomi_records(transfer_records):
    """
    振込対象レコード（楽天総合振込CSVと同じもの）に、取引先マスタから借方科目と
    源泉税額を載せて、②の入力レコードに変換する。

    transfer_records : [{"会社名","金額","_master": 取引先マスタの行}]
    戻り値 : (records, skipped)
             skipped は [(会社名, 理由)]。勘定科目が引けなかった取引先で、
             **そのぶん仕訳が丸ごと抜ける**ため必ず画面に出すこと。
    """
    records, skipped = [], []
    for r in transfer_records:
        m = r.get("_master") or {}
        kari = soufurikomi_karikata(m)
        if not kari:
            skipped.append((r.get("会社名", ""), "マスタに勘定科目が未設定"))
            continue
        records.append(dict(
            kari,
            # ②の摘要は振込のカナ名。①（マスタの摘要列）とは別物
            摘要=(m.get("受取人口座名", "") or m.get("摘要", "") or r.get("会社名", "")),
            金額=to_int(r.get("金額", 0)),
            源泉税額=to_int(m.get("源泉税額", 0)),
            会社名=r.get("会社名", ""),
        ))
    return records, skipped


def to_int(v):
    """'1,021' や 1021.0、空欄を int に寄せる。読めない値は0。"""
    try:
        return int(round(float(str(v).replace(",", "").strip() or 0)))
    except (TypeError, ValueError):
        return 0


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


def furikomi_date(target_ym, exec_mmdd):
    """
    ②の取引日＝振込実行日を 'YYYY/MM/DD' で返す。

    対象月(YYYY-MM)の翌月に振り込む運用なので、年は対象月から決める
    （12月分を1月に振り込む場合は翌年になる）。
    例: 対象月 2026-05・実行日 0630 → '2026/06/30'
    """
    import datetime
    y, m = int(str(target_ym).split("-")[0]), int(str(target_ym).split("-")[1])
    mm, dd = int(str(exec_mmdd)[:2]), int(str(exec_mmdd)[2:4])
    if mm < m:                      # 年をまたいだ（12月分 → 翌年1月に振込）
        y += 1
    return datetime.date(y, mm, dd).strftime("%Y/%m/%d")


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


def build_soufurikomi_csv(records, torihiki_no_value, torihiki_date, every_row=False,
                          encoding="utf-8"):
    """
    総合振込仕訳帳CSV（MF仕訳インポート）のバイト列を作る。

    records : [{"借方勘定科目","借方補助科目","借方税区分","摘要","金額","源泉税額"}]
              金額 = 実際の振込額（＝楽天総合振込CSVに出る額。源泉は差し引かれている）
              源泉税額 = 無ければ0。あればその額だけ貸方に 預り金/所得税 の行が立つ
    torihiki_no_value : 取引No（経理シートの連番。MFは9桁以内の数字）
    torihiki_date     : 取引日（振込実行日。'2026/06/30' 形式の文字列 or date）
    every_row : True なら全行に取引No・取引日を出力（既定は1行目のみ＝運用シートと同じ）

    借方勘定科目が空のレコードがあれば ValueError。MFの必須項目が抜けたCSVは、
    取り込めてしまうと科目なしの仕訳が静かに1本増えるので、出す前に止める。
    （貸借は 借方＝振込額＋源泉、貸方＝仮受金＋預り金 と組み立て方で必ず一致するため、
     ここで検算しても何も見張れない。振込総額そのものの妥当性は画面で楽天CSVと突き合わせる）
    """
    if hasattr(torihiki_date, "strftime"):
        torihiki_date = torihiki_date.strftime("%Y/%m/%d")

    missing = [str(r.get("摘要", "") or "(摘要なし)") for r in records
               if not str(r.get("借方勘定科目", "")).strip()]
    if missing:
        raise ValueError("借方勘定科目が未設定の取引先があります（取引先マスタで設定してください）: "
                         + "、".join(missing))

    furikomi_total = sum(to_int(r.get("金額", 0)) for r in records)
    gensen_rows = [r for r in records if to_int(r.get("源泉税額", 0)) != 0]

    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(SOUFURIKOMI_HEADER)

    def row(first, kari=None, kashi=None, tekiyo=""):
        kari = kari or {}
        kashi = kashi or {}
        return [
            str(torihiki_no_value) if first else "",
            torihiki_date if first else "",
            kari.get("勘定科目", ""), kari.get("補助科目", ""), kari.get("税区分", ""),
            "", yen(kari["金額"]) if "金額" in kari else "",
            kashi.get("勘定科目", ""), kashi.get("補助科目", ""), kashi.get("税区分", ""),
            "", yen(kashi["金額"]) if "金額" in kashi else "",
            tekiyo, "",
        ]

    # 1行目: 貸方に振込総額（仮受金）。借方は空
    kashi = dict(FURIKOMI_KASHIKATA, 金額=furikomi_total)
    w.writerow(row(True, kashi=kashi, tekiyo=FURIKOMI_TEKIYO))

    # 2行目以降: 借方の内訳（1取引先1行）。源泉がある分は振込額に足し戻す
    for r in records:
        amount = to_int(r.get("金額", 0)) + to_int(r.get("源泉税額", 0))
        w.writerow(row(every_row, kari={
            "勘定科目": r.get("借方勘定科目", ""), "補助科目": r.get("借方補助科目", ""),
            "税区分": r.get("借方税区分", ""), "金額": amount,
        }, tekiyo=r.get("摘要", "")))

    # 末尾: 源泉徴収の預り金（取引先ごとに1行。摘要は借方明細と同じ）
    for r in gensen_rows:
        w.writerow(row(every_row, kashi=dict(GENSEN_KASHIKATA,
                                             金額=to_int(r.get("源泉税額", 0))),
                       tekiyo=r.get("摘要", "")))

    return buf.getvalue().encode(encoding, errors="replace")
