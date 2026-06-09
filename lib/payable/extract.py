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

EXTRACT_TOOL = {
    "name": "record_invoice",
    "description": "請求書から読み取った内容を構造化して記録する",
    "input_schema": {
        "type": "object",
        "properties": {
            "会社名": {"type": "string", "description": "請求元(取引先)の会社名。株式会社等の法人格は付けたまま"},
            "当月請求額": {"type": "integer", "description": "当月分の請求額(今回御買上・当月商品代金等、税込)。繰越を含めない当月発生分"},
            "今回請求額": {"type": "integer", "description": "今回御請求額(前月繰越を含む総額、税込)。請求書の最終支払額"},
            "前月繰越額": {"type": "integer", "description": "前月残・前回繰越額。無ければ0"},
            "消費税額": {"type": "integer", "description": "消費税額。不明なら0"},
            "請求日": {"type": "string", "description": "請求日・締日(YYYY/MM/DD)。無ければ空"},
            "支払期日": {"type": "string", "description": "支払期限(YYYY/MM/DD)。無ければ空"},
            "振込先銀行": {"type": "string", "description": "振込先の銀行名。無ければ空"},
            "振込先支店": {"type": "string", "description": "支店名。無ければ空"},
            "預金種目": {"type": "string", "enum": ["普通", "当座", ""], "description": "預金種目"},
            "口座番号": {"type": "string", "description": "口座番号(数字のみ)。無ければ空"},
            "口座名義": {"type": "string", "description": "口座名義。無ければ空"},
            "複数口座": {"type": "boolean", "description": "振込先口座が複数記載されている場合true"},
            "信頼度メモ": {"type": "string", "description": "読み取りが曖昧な点・注意事項を簡潔に"},
        },
        "required": ["会社名", "当月請求額", "今回請求額"],
    },
}

SYSTEM = (
    "あなたは日本のEC事業者のバックオフィス担当として、取引先から届いた請求書を読み取ります。"
    "請求書には『前月繰越』『前回御請求額』『今回御買上』などが混在することがあります。"
    "当月分(当月発生した請求)と、繰越を含む総額(最終支払額)を必ず区別してください。"
    "金額はカンマや円記号を除いた整数で返します。読み取れない項目は空文字または0にします。"
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
            data = dict(block.input)
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
