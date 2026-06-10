# -*- coding: utf-8 -*-
"""
Claude APIで請求書(PDF/画像)から支払いに必要な項目を抽出する。

- テキスト埋め込みPDFもスキャン画像PDFも、Claudeのdocument(PDF)入力で処理可能。
- モデルは用途で使い分け(既定はhaiku、読みにくい場合はsonnetに切替)。
- 金額は「当月請求額(今回買上・当月分)」と「今回御請求額(繰越含む総額)」を
  別々に取得する。突合は当月分で行うため。
"""
import base64
import json

HAIKU = "claude-haiku-4-5"
SONNET = "claude-sonnet-4-6"

# Claudeのツール定義はプロパティのキーがASCII(^[a-zA-Z0-9_.-]{1,64}$)である
# 必要があるため、英語キーで定義し、抽出後に日本語キーへ変換する。
EXTRACT_TOOL = {
    "name": "record_invoice",
    "description": "請求書から読み取った内容を構造化して記録する",
    "input_schema": {
        "type": "object",
        "properties": {
            "company": {"type": "string", "description": "請求元(取引先)の会社名。株式会社等の法人格は付けたまま"},
            "current_amount": {"type": "integer", "description": "当月分の請求額(今回御買上・当月商品代金等、税込)。繰越を含めない当月発生分"},
            "total_amount": {"type": "integer", "description": "今回御請求額(前月繰越を含む総額、税込)。請求書の最終支払額"},
            "carryover": {"type": "integer", "description": "前月残・前回繰越額。無ければ0"},
            "tax": {"type": "integer", "description": "消費税額の合計。不明なら0"},
            "tax_breakdown": {"type": "string", "description": "税率別の内訳。軽減税率(8%)と標準税率(10%)が混在する場合は必ず分けて『10%:税抜4500/税450, 8%:税抜1000/税80』のように記載。単一税率なら『10%:税抜X/税Y』。無ければ空"},
            "reduced_tax": {"type": "boolean", "description": "軽減税率(8%等)の対象品目が含まれる場合true"},
            "bill_date": {"type": "string", "description": "請求日・締日(YYYY/MM/DD)。無ければ空"},
            "due_date": {"type": "string", "description": "支払期限(YYYY/MM/DD)。無ければ空"},
            "bank": {"type": "string", "description": "振込先の銀行名。無ければ空"},
            "branch": {"type": "string", "description": "支店名。無ければ空"},
            "account_type": {"type": "string", "enum": ["普通", "当座", ""], "description": "預金種目"},
            "account_number": {"type": "string", "description": "口座番号(数字のみ)。無ければ空"},
            "account_holder": {"type": "string", "description": "口座名義。無ければ空"},
            "multiple_accounts": {"type": "boolean", "description": "振込先口座が複数記載されている場合true"},
            "note": {"type": "string", "description": "読み取りが曖昧な点・注意事項を簡潔に"},
        },
        "required": ["company", "current_amount", "total_amount"],
    },
}

# 英語キー -> 日本語キー(アプリ内で使う名称)
_KEY_MAP = {
    "company": "会社名", "current_amount": "当月請求額", "total_amount": "今回請求額",
    "carryover": "前月繰越額", "tax": "消費税額", "bill_date": "請求日",
    "due_date": "支払期日", "bank": "振込先銀行", "branch": "振込先支店",
    "account_type": "預金種目", "account_number": "口座番号",
    "account_holder": "口座名義", "multiple_accounts": "複数口座", "note": "信頼度メモ",
    "tax_breakdown": "税内訳", "reduced_tax": "軽減税率",
}


def _to_jp(data):
    """ツール出力(英語キー)を日本語キーの辞書に変換する。"""
    return {_KEY_MAP.get(k, k): v for k, v in data.items()}

SYSTEM = (
    "あなたは日本のEC事業者のバックオフィス担当として、取引先から届いた請求書を読み取ります。"
    "請求書には『前月繰越』『前回御請求額』『今回御買上』などが混在することがあります。"
    "当月分(当月発生した請求)と、繰越を含む総額(最終支払額)を必ず区別してください。"
    "金額はカンマや円記号を除いた整数で返します。読み取れない項目は空文字または0にします。"
    "食品等で軽減税率(8%)と標準税率(10%)が混在する請求書では、税率別の内訳を必ず分けて記載してください。"
)


def _media_type(filename):
    fn = (filename or "").lower()
    if fn.endswith(".pdf"):
        return "application/pdf"
    if fn.endswith(".png"):
        return "image/png"
    if fn.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if fn.endswith(".webp"):
        return "image/webp"
    if fn.endswith(".gif"):
        return "image/gif"
    return "application/pdf"


def _content_block(file_bytes, media_type):
    b64 = base64.standard_b64encode(file_bytes).decode("ascii")
    if media_type == "application/pdf":
        return {"type": "document",
                "source": {"type": "base64", "media_type": media_type, "data": b64}}
    return {"type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": b64}}


def get_client():
    import streamlit as st
    import anthropic
    key = "".join(c for c in st.secrets["ANTHROPIC_API_KEY"]
                  if c.isprintable() and ord(c) < 128)
    return anthropic.Anthropic(api_key=key)


def extract_invoice(file_bytes, filename, model=HAIKU):
    """
    請求書1ファイルから項目を抽出して dict を返す。
    失敗時は {"_error": "..."} を含む dict。
    """
    client = get_client()
    media_type = _media_type(filename)
    try:
        msg = client.messages.create(
            model=model,
            max_tokens=1024,
            system=SYSTEM,
            tools=[EXTRACT_TOOL],
            tool_choice={"type": "tool", "name": "record_invoice"},
            messages=[{
                "role": "user",
                "content": [
                    _content_block(file_bytes, media_type),
                    {"type": "text", "text": "この請求書を record_invoice ツールで記録してください。"},
                ],
            }],
        )
    except Exception as e:  # noqa: BLE001
        return {"_error": f"AI呼び出しエラー: {e}", "_model": model}

    for block in msg.content:
        if getattr(block, "type", None) == "tool_use":
            data = _to_jp(dict(block.input))
            data["_model"] = model
            data["_file"] = filename
            return data
    return {"_error": "構造化出力が得られませんでした", "_model": model,
            "_raw": "".join(getattr(b, "text", "") for b in msg.content)}


def extract_with_fallback(file_bytes, filename):
    """haikuで試し、エラーや会社名欠落ならsonnetで再試行する。"""
    data = extract_invoice(file_bytes, filename, model=HAIKU)
    if data.get("_error") or not data.get("会社名"):
        data2 = extract_invoice(file_bytes, filename, model=SONNET)
        if not data2.get("_error"):
            return data2
    return data
