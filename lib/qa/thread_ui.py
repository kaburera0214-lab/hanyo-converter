# -*- coding: utf-8 -*-
"""
質問・回答管理（pages 2〜5）の会話表示UI。

Notionの「会話ログ」プロパティは
    【Q】… / 【A】… / 【追加Q｜YYYY-MM-DD HH:MM】… / 【追加A｜…】…
という1本のテキストで保存されている。ラリーが増えるほど
「フラットな貼り付け」「過去ログの引用の入れ子」で読めなくなるため、
ここでターンに分解してチャット風（吹き出し）に描画する。

使い方（各ページの先頭で1回）:
    from lib.qa.thread_ui import inject_qa_styles, render_thread, render_text_block
    inject_qa_styles()
"""
import html as _html
import re

import streamlit as st

# 【Q】/【A】/【追加Q｜日時】/【追加A｜日時】
MARKER_RE = re.compile(r"^【(追加Q|追加A|Q|A)(?:[｜|]\s*([^】]*))?】[ 　]*", re.M)
# 行頭の引用記号（>, ＞）
QUOTE_LINE_RE = re.compile(r"^[ 　]*[>＞]+[ 　]?")
# 区切り線（----- など）。これで囲まれた範囲は貼り付けた過去ログとみなす
SEPARATOR_RE = re.compile(r"^[ 　]*[-−ー–—=＝_]{5,}[ 　]*$")
URL_RE = re.compile(r"https?://[^\s<>\"'）」】、。]+")

SPEAKER = {"Q": "インハナさん", "追加Q": "インハナさん", "A": "パピー", "追加A": "パピー"}
KIND_LABEL = {"Q": "最初の質問", "A": "回答", "追加Q": "追加質問", "追加A": "追加回答"}
# 重複判定の対象にする最小文字数（「以上です。」のような短文は消さない）
DEDUPE_MIN_LEN = 8


def inject_qa_styles():
    """会話UIのCSS。ページの先頭で1回だけ呼ぶ。

    色は半透明のオーバーレイだけで作り、文字色はStreamlitのテーマを継承する。
    こうしておくとライト／ダークどちらのテーマでも読める。
    """
    st.markdown(
        """<style>
.qa-thread { display:flex; flex-direction:column; gap:10px; margin:2px 0 10px; }
.qa-turn { display:flex; flex-direction:column; }
.qa-turn.qa-a { align-items:flex-end; }
.qa-meta { font-size:12px; opacity:.62; margin:0 2px 3px; }
.qa-badge { font-size:11px; padding:1px 7px; border-radius:999px; margin-left:6px;
            white-space:nowrap; background:rgba(230,170,40,.22);
            border:1px solid rgba(230,170,40,.45); opacity:1; }
.qa-bubble { max-width:86%; border:1px solid; border-radius:10px; padding:10px 12px;
             font-size:14px; line-height:1.75; word-break:break-word; white-space:normal; }
.qa-turn.qa-q .qa-bubble { background:rgba(128,128,128,.10); border-color:rgba(128,128,128,.26);
                           border-top-left-radius:2px; }
.qa-turn.qa-a .qa-bubble { background:rgba(70,140,235,.11); border-color:rgba(70,140,235,.32);
                           border-top-right-radius:2px; }
.qa-turn.qa-latest .qa-bubble { border-color:rgba(230,170,40,.75);
                                box-shadow:0 0 0 2px rgba(230,170,40,.16); }
.qa-bubble p { margin:0 0 6px; }
.qa-bubble p:last-child { margin-bottom:0; }
.qa-quote { margin:6px 0 2px; }
.qa-quote > summary { font-size:12px; opacity:.62; cursor:pointer; list-style:none; }
.qa-quote > summary::-webkit-details-marker { display:none; }
.qa-quote > summary::before { content:"▸ "; }
.qa-quote[open] > summary::before { content:"▾ "; }
.qa-quote-body { margin-top:5px; padding-left:9px; font-size:13px; line-height:1.7; opacity:.68;
                 border-left:3px solid rgba(128,128,128,.35); }
.qa-note { font-size:12px; opacity:.55; text-align:center; margin:2px 0; }
.qa-panel { background:rgba(128,128,128,.07); border:1px solid rgba(128,128,128,.24);
            border-radius:8px; padding:11px 13px; font-size:14px; line-height:1.75;
            word-break:break-word; }
.qa-panel-label { font-size:12px; opacity:.62; margin:0 0 3px 2px; }
.qa-bubble a, .qa-panel a { word-break:break-all; }
</style>""",
        unsafe_allow_html=True,
    )


def parse_conversation(log):
    """会話ログをターンのリストに分解する。

    戻り値: (turns, preamble)
      turns   … [{"kind","role","speaker","timestamp","body"}, …]
      preamble… マーカーより前の文字列（「（古い会話を省略）」など）
    """
    if not log or not log.strip():
        return [], ""
    matches = list(MARKER_RE.finditer(log))
    if not matches:
        return [], log.strip()
    preamble = log[: matches[0].start()].strip()
    turns = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(log)
        kind = m.group(1)
        turns.append({
            "kind": kind,
            "role": "q" if kind.endswith("Q") else "a",
            "speaker": SPEAKER.get(kind, ""),
            "timestamp": (m.group(2) or "").strip(),
            "body": log[m.end():end].strip(),
        })
    return turns, preamble


def _normalize(line):
    """重複判定用の正規化（引用記号・記号・空白を落とす）。"""
    s = QUOTE_LINE_RE.sub("", line)
    s = s.lstrip("・-−ー–—*➡→＞>　 ").strip()
    return re.sub(r"[ 　]+", "", s)


def split_body(body, seen):
    """本文を「本文」と「引用（過去ログ）」のセグメントに分ける。

    引用とみなすのは次の3つ。
      1) 行頭が > / ＞ の行
      2) ----- のような区切り線で囲まれた範囲（チャットの貼り付け）
      3) 同じスレッドの前のターンに既に出てきた行（入れ子の再掲）

    seen は「これまでのターンに出た行」の集合。呼び出し側で使い回すと
    ラリーが進むほど再掲がたたまれる。
    """
    segments = []
    in_sep_block = False

    def push(kind, text):
        if segments and segments[-1][0] == kind:
            segments[-1][1].append(text)  # 空行は段落の区切りとしてそのまま残す
        elif text.strip():
            segments.append([kind, [text]])

    for line in body.split("\n"):
        if SEPARATOR_RE.match(line):
            in_sep_block = not in_sep_block
            continue
        norm = _normalize(line)
        if not norm:
            push("quote" if in_sep_block else "main", "")
            continue
        is_quote = (
            in_sep_block
            or bool(QUOTE_LINE_RE.match(line))
            or (len(norm) >= DEDUPE_MIN_LEN and norm in seen)
        )
        push("quote" if is_quote else "main", QUOTE_LINE_RE.sub("", line).rstrip())
        if len(norm) >= DEDUPE_MIN_LEN:
            seen.add(norm)

    return [(kind, "\n".join(lines).strip("\n")) for kind, lines in segments]


def text_to_html(text):
    """プレーンテキストを安全にHTML化（Markdown解釈を避ける・URLはリンク化）。"""
    escaped = _html.escape(text)
    escaped = URL_RE.sub(lambda m: f'<a href="{m.group(0)}" target="_blank">{m.group(0)}</a>', escaped)
    return escaped.replace("\n", "<br>")


def _quote_html(text):
    lines = [l for l in text.split("\n") if l.strip()]
    summary = f"引用・過去のやり取り（{len(lines)}行）を表示"
    return (
        f'<details class="qa-quote"><summary>{summary}</summary>'
        f'<div class="qa-quote-body">{text_to_html(text)}</div></details>'
    )


def render_thread(log, *, highlight_last=False, fallback_q="", fallback_a=""):
    """会話ログを吹き出しスレッドで描画する。

    highlight_last … 最後のターンを「未対応」として強調する（再質問など）
    fallback_q/a   … 会話ログが無い質問向け。質問本文・回答本文から1往復を組み立てる
    """
    turns, preamble = parse_conversation(log)
    if not turns and (fallback_q or fallback_a):
        turns = []
        if fallback_q:
            turns.append({"kind": "Q", "role": "q", "speaker": SPEAKER["Q"], "timestamp": "", "body": fallback_q})
        if fallback_a:
            turns.append({"kind": "A", "role": "a", "speaker": SPEAKER["A"], "timestamp": "", "body": fallback_a})
    if not turns:
        if preamble:
            st.markdown(f'<div class="qa-panel">{text_to_html(preamble)}</div>', unsafe_allow_html=True)
        return

    parts = ['<div class="qa-thread">']
    if preamble:
        parts.append(f'<div class="qa-note">{text_to_html(preamble)}</div>')

    seen = set()
    for i, t in enumerate(turns):
        is_last = i == len(turns) - 1
        classes = f"qa-turn qa-{t['role']}"
        if highlight_last and is_last:
            classes += " qa-latest"
        meta = f"{t['speaker']}・{KIND_LABEL.get(t['kind'], '')}"
        if t["timestamp"]:
            meta += f"　{t['timestamp']}"
        badge = '<span class="qa-badge">未対応</span>' if (highlight_last and is_last) else ""

        inner = []
        for kind, text in split_body(t["body"], seen):
            if kind == "quote":
                inner.append(_quote_html(text))
            else:
                inner.append(f"<p>{text_to_html(text)}</p>")
        if not inner:
            inner.append('<p class="qa-note">（本文なし）</p>')

        parts.append(
            f'<div class="{classes}">'
            f'<div class="qa-meta">{_html.escape(meta)}{badge}</div>'
            f'<div class="qa-bubble">{"".join(inner)}</div>'
            f"</div>"
        )
    parts.append("</div>")
    st.markdown("".join(parts), unsafe_allow_html=True)


def question_no(number):
    """質問番号の表示形。Notionのユニークid（DB内で一意・削除しても再利用されない）。"""
    return f"#{number}" if number else "（番号なし）"


def render_text_block(text, label=None):
    """質問本文などのプレーンテキストを、Markdown解釈させずに枠内表示する。

    st.markdown に生テキストを渡すと「---」の直前行が見出し化して
    文字サイズがバラバラになるため、こちらを使う。
    """
    if label:
        st.markdown(f'<div class="qa-panel-label">{_html.escape(label)}</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="qa-panel">{text_to_html(text or "")}</div>', unsafe_allow_html=True)
