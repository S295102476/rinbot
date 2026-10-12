import asyncio
from types import SimpleNamespace

import nonebot
import pytest

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(driver="~fastapi", log_level="ERROR")

from nonebot.adapters.onebot.v11 import Message, MessageSegment
from plugins import group_chat, ai_chat, gemini_native


def test_hot_cache_persists_last_image_in_a_multi_image_message():
    message = Message([
        MessageSegment.image("first"), MessageSegment.text("中间文字"),
        MessageSegment.image("last"), MessageSegment.image(""),
    ])
    assert group_chat._latest_chat_image_url(message) == "last"
    assert group_chat._latest_chat_image_url(Message("普通闲聊")) == ""


@pytest.mark.parametrize("text,failed,expected", [
    ("这张好看吗", False, ["last"]),
    ("这两张图对比一下", False, ["first", "last"]),
    ("这张好看吗", True, ["last"]),
])
def test_passive_vision_selects_before_downloading_and_never_substitutes_old_image(
        monkeypatch, text, failed, expected):
    downloads, requests = [], []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url):
            downloads.append(url)
            return SimpleNamespace(status_code=500 if failed else 200,
                headers={"content-type": "image/jpeg"}, content=url.encode())

    async def context(group_id):
        return "群里几分钟前讨论过一张旧图，现在换话题"

    async def provider(model, messages):
        requests.append(messages)
        return '{"replies":["收到"]}'

    monkeypatch.setattr(group_chat.httpx, "AsyncClient", Client)
    monkeypatch.setattr(group_chat, "_get_group_context_text", context)
    monkeypatch.setitem(ai_chat.ai_cfg, "vision", {"protocol": "gemini_native", "model": "fixture-native"})
    monkeypatch.setattr(gemini_native, "call_gemini_native", provider)
    reply = asyncio.run(group_chat.vision_comment(10, ["first", "last"], text))
    assert downloads == expected
    assert len(requests) == (0 if failed else 1)
    assert (reply is None) == failed
    if requests:
        parts = requests[0][-1]["content"]
        assert len([part for part in parts if part["type"] == "image_url"]) == len(expected)
