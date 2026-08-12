# -*- coding: utf-8 -*-
"""
楽天銀行 総合振込インポートCSVの生成。

サンプル(取引先情報.xlsx「マスタ」から実際に作られているCSV)で確定した仕様:
  ヘッダ: サービス区分,実行日,受取人銀行番号,受取人支店番号,受取人預金種目,
          受取人口座番号,受取人口座名,金額,顧客番号
  - サービス区分 : "3"(総合振込)固定
  - 実行日       : MMDD (例 0430)。4桁ゼロ埋め
  - 受取人銀行番号: 4桁ゼロ埋め
  - 受取人支店番号: 3桁ゼロ埋め
  - 受取人預金種目: 普通→"1" / 当座→"2"
  - 受取人口座番号: 7桁ゼロ埋め
  - 受取人口座名  : 全角カナ(マスタの値をそのまま)
  - 金額         : 円・カンマなし
  - 顧客番号     : 4桁ゼロ埋めの連番(出力順)
  文字コード: Shift-JIS(cp932)、改行: CRLF
  ヘッダ行  : 楽天銀行のインポートでは不要なため出力しない(列順の参考としてHEADERは残す)
"""

HEADER = ["サービス区分", "実行日", "受取人銀行番号", "受取人支店番号",
          "受取人預金種目", "受取人口座番号", "受取人口座名", "金額", "顧客番号"]

SHUMOKU_CODE = {"普通": "1", "当座": "2", "1": "1", "2": "2"}


def shumoku_code(value):
    return SHUMOKU_CODE.get(str(value).strip(), "1")


def build_row(*, 銀行番号, 支店番号, 預金種目, 口座番号, 受取人口座名, 金額,
              実行日, 顧客番号):
    """1明細を楽天CSVの9フィールドのリストにする。"""
    return [
        "3",
        str(実行日).zfill(4),
        str(銀行番号).strip().zfill(4),
        str(支店番号).strip().zfill(3),
        shumoku_code(預金種目),
        str(口座番号).strip().zfill(7),
        str(受取人口座名).strip(),
        str(int(round(float(金額)))),
        str(顧客番号).zfill(4),
    ]


def build_csv_text(records, 実行日, start_kokyaku=2, include_header=False):
    """
    records: [{銀行番号,支店番号,預金種目,口座番号,受取人口座名,金額, (会社名)}]
    実行日 : "MMDD" もしくは datetime/date
    顧客番号は出力順に start_kokyaku から連番(サンプルが0002始まりのため既定2)。
    戻り値: CSV文字列(明細のみ、CRLF)。
    楽天銀行のインポートはヘッダ行不要のため、既定ではヘッダを出力しない。
    """
    import datetime
    if isinstance(実行日, (datetime.date, datetime.datetime)):
        実行日 = 実行日.strftime("%m%d")
    実行日 = str(実行日).zfill(4)

    lines = [",".join(HEADER)] if include_header else []
    for i, r in enumerate(records):
        row = build_row(
            銀行番号=r.get("銀行番号", ""),
            支店番号=r.get("支店番号", ""),
            預金種目=r.get("預金種目", ""),
            口座番号=r.get("口座番号", ""),
            受取人口座名=r.get("受取人口座名", ""),
            金額=r.get("金額", 0),
            実行日=実行日,
            顧客番号=start_kokyaku + i,
        )
        lines.append(",".join(row))
    return "\r\n".join(lines) + "\r\n"


def build_csv_bytes(records, 実行日, start_kokyaku=2, include_header=False):
    """Shift-JIS(cp932)エンコードしたbytesを返す(楽天インポート用・ヘッダなし)。"""
    text = build_csv_text(records, 実行日, start_kokyaku=start_kokyaku,
                          include_header=include_header)
    return text.encode("cp932")
