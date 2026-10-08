# -*- coding: utf-8 -*-
"""商品名・キャッチコピーの部分更新を他項目に広げない。"""
from lib.autopage import rms_items


def test_patch_title_tagline_sends_only_two_fields(monkeypatch):
    called = {}

    def fake_patch(path, payload):
        called.update(path=path, payload=payload)
        return {"ok": True}

    monkeypatch.setattr(rms_items.rms_api, "patch", fake_patch)
    rms_items.patch_title_tagline("abc-1", "新しい商品名", "新しいキャッチコピー")

    assert called == {
        "path": "/es/2.0/items/manage-numbers/abc-1",
        "payload": {"title": "新しい商品名", "tagline": "新しいキャッチコピー"},
    }
