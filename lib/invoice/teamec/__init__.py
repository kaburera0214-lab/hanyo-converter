"""TeamEC新体系。クライアントは既存のTeam-ECのまま、対象月で料金の版を切り替える。"""

CLIENT = "Team-EC"
APPLIES_FROM = (2026, 9)


def applies(client_name, year, month) -> bool:
    """請求書ページの分岐判定。旧体系の月・他クライアントは既存処理のまま。"""
    return client_name == CLIENT and (int(year), int(month)) >= APPLIES_FROM
