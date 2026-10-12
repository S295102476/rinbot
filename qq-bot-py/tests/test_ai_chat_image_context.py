"""Legacy image selection uses fake providers, sessions and object storage."""

import asyncio
from types import ModuleType, SimpleNamespace
import sys
from unittest.mock import AsyncMock, Mock

import nonebot
from nonebot.adapters.onebot.v11 import Message, MessageSegment
import pytest

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(driver="~fastapi", log_level="ERROR")

from plugins import ai_chat as module

_group_context_loader = module._get_group_context


def image_parts(messages):
    return [part["image_url"]["url"] for message in messages
            if isinstance(message.get("content"), list)
            for part in message["content"] if part.get("type") == "image_url"]


@pytest.fixture
def isolated(monkeypatch):
    # No startup hooks run and every entry point that could issue I/O is faked.
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda: []))), close=AsyncMock())
    monkeypatch.setattr(module, "get_session", AsyncMock(return_value=session))
    monkeypatch.setattr(module, "get_user_affinity", AsyncMock(return_value=0.0))
    monkeypatch.setattr(module, "get_persona_content", lambda: "演示人格")
    monkeypatch.setattr(module, "get_active_persona_id", lambda: "rin")
    monkeypatch.setattr(module, "get_active_persona_name", lambda: "凛")
    monkeypatch.setattr(module, "MODEL", "fixture-text")
    monkeypatch.setattr(module, "_get_active_model", lambda: "ag")
    monkeypatch.setattr(module, "_antigravity_available", lambda: True)
    chat_provider = AsyncMock(return_value='{"replies":["演示回答"]}')
    monkeypatch.setattr(module, "_call_antigravity_openai", chat_provider)
    for name in ("_call_fallback", "_call_primary", "_call_openai_responses"):
        monkeypatch.setattr(module, name, AsyncMock(side_effect=AssertionError("unexpected provider")))
    monkeypatch.setattr(module, "_get_group_context", AsyncMock(return_value=(
        "历史文字包含旧图聊天，不能替代新图。", [("oldest", "甲"), ("previous", "乙"), ("latest", "丙")]
    )))
    storage = Mock(side_effect=lambda key: ("image/jpeg", "b64-" + key))
    monkeypatch.setattr(module, "_read_group_img_b64", storage)

    downloads, bad_urls = [], set()
    class Client:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def get(self, url):
            downloads.append(url)
            if url in bad_urls:
                raise OSError("unreadable image")
            return SimpleNamespace(content=b"fake-image", headers={"content-type": "image/jpeg"},
                                   raise_for_status=lambda: None)
        def stream(self, *args, **kwargs):
            raise AssertionError("unexpected real vision provider path")
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda *args, **kwargs: Client())
    monkeypatch.setitem(module.ai_cfg, "vision", {"model": "gemini-fixture", "protocol": "gemini_native"})
    gemini = ModuleType("plugins.gemini_native")
    gemini.call_gemini_native = AsyncMock(return_value='{"replies":["看清了"]}')
    monkeypatch.setitem(sys.modules, gemini.__name__, gemini)
    return SimpleNamespace(session=session, provider=chat_provider, storage=storage,
                           downloads=downloads, bad_urls=bad_urls, vision=gemini.call_gemini_native)


@pytest.mark.parametrize("text", ["今天吃什么？", "我比较忙。", "对比这两个框架", "之前说过什么？"])
def test_ordinary_text_does_not_download_or_send_old_images(isolated, text):
    asyncio.run(module.chat(11, "演示", text, 100, persist_history=False))
    isolated.storage.assert_not_called()
    assert image_parts(isolated.provider.call_args.args[0]) == []


@pytest.mark.parametrize("text,key", [("这张图是什么？", "latest"), ("上一张图是什么？", "previous")])
def test_explicit_image_followup_reads_only_named_recent_image(isolated, text, key):
    asyncio.run(module.chat(11, "演示", text, 100, persist_history=False))
    isolated.storage.assert_called_once_with(key)
    assert image_parts(isolated.provider.call_args.args[0]) == ["data:image/jpeg;base64,b64-" + key]


def test_explicit_comparison_can_load_small_historical_set(isolated):
    asyncio.run(module.chat(11, "演示", "对比这几张图", 100, persist_history=False))
    assert isolated.storage.call_count == 3
    assert len(image_parts(isolated.provider.call_args.args[0])) == 3


def test_latest_history_failure_never_reads_earlier_candidate(isolated):
    isolated.storage.return_value = None
    isolated.storage.side_effect = None
    asyncio.run(module.chat(11, "演示", "这张图是什么？", 100, persist_history=False))
    isolated.storage.assert_called_once_with("latest")
    messages = isolated.provider.call_args.args[0]
    assert image_parts(messages) == []
    assert "不要用其他图片或先前描述代替" in messages[-1]["content"]


def test_explicit_empty_images_disable_historical_fallback(isolated):
    asyncio.run(module.chat(11, "演示", "这张图是什么？", 100, context_images=[], persist_history=False))
    isolated.storage.assert_not_called()
    assert image_parts(isolated.provider.call_args.args[0]) == []


def test_quoted_text_image_words_do_not_trigger_unrequested_history(isolated):
    asyncio.run(module.chat(11, "演示", "[引用某人: 对比上一张图]\n我知道了", 100,
                            persist_history=False, visual_intent="我知道了"))
    isolated.storage.assert_not_called()
    assert image_parts(isolated.provider.call_args.args[0]) == []


@pytest.mark.parametrize("explicit", [["data:image/jpeg;base64,current"],
                                     [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,current"}}]])
def test_context_images_are_used_without_adding_history(isolated, explicit):
    asyncio.run(module.chat(11, "演示", "这张图是什么？", 100, context_images=explicit, persist_history=False))
    isolated.storage.assert_not_called()
    assert image_parts(isolated.provider.call_args.args[0]) == ["data:image/jpeg;base64,current"]


@pytest.mark.parametrize("text,expected", [("看看", ["new-2"]), ("这几张图分别说说", ["new-1", "new-2"])])
def test_vision_selects_latest_current_image_unless_plural_is_explicit(isolated, text, expected):
    images = [{"url": "new-1"}, {"url": "new-2"}]
    asyncio.run(module.chat_with_vision(11, "演示", text, images, 100))
    assert isolated.downloads == expected
    isolated.storage.assert_not_called()
    assert len(image_parts(isolated.vision.call_args.args[1])) == len(expected)


def test_current_this_image_does_not_add_previous_images(isolated):
    asyncio.run(module.chat_with_vision(11, "演示", "这张图是什么？", [{"url": "new"}], 100))
    isolated.storage.assert_not_called()
    assert len(image_parts(isolated.vision.call_args.args[1])) == 1


def test_plural_wording_with_only_one_current_image_does_not_add_old_images(isolated):
    asyncio.run(module.chat_with_vision(11, "演示", "这几张图分别说说", [{"url": "new"}], 100))
    isolated.storage.assert_not_called()
    assert len(image_parts(isolated.vision.call_args.args[1])) == 1


def test_old_quoted_comparison_wording_does_not_change_current_intent(isolated):
    asyncio.run(module.chat_with_vision(11, "演示", "[引用某人: 对比上一张图]\n这是什么？", [
        {"url": "quoted", "source": "referenced"}, {"url": "new"}], 100,
        visual_intent="这是什么？"))
    assert isolated.downloads == ["quoted"]
    isolated.storage.assert_not_called()
    assert len(image_parts(isolated.vision.call_args.args[1])) == 1


def test_quoted_image_has_priority_over_current_image(isolated):
    images = [{"url": "quoted", "source": "referenced"}, {"url": "new"}]
    asyncio.run(module.chat_with_vision(11, "演示", "这是哪里？", images, 100))
    assert isolated.downloads == ["quoted"]
    isolated.storage.assert_not_called()


def test_explicit_current_comparison_adds_old_image_only_on_request(isolated):
    asyncio.run(module.chat_with_vision(11, "演示", "和上一张图对比一下", [{"url": "new"}], 100))
    assert isolated.downloads == ["new"]
    isolated.storage.assert_called_once_with("latest")
    parts = image_parts(isolated.vision.call_args.args[1])
    assert parts[0] == "data:image/jpeg;base64,b64-latest"
    assert len(parts) == 2


def test_failed_quoted_image_is_not_replaced_with_current_or_history(isolated):
    isolated.bad_urls.add("quoted")
    response = asyncio.run(module.chat_with_vision(11, "演示", "这张图是什么？", [
        {"url": "quoted", "source": "referenced"}, {"url": "new"}], 100))
    assert "没能读到" in response
    assert isolated.downloads == ["quoted"]
    isolated.storage.assert_not_called()
    isolated.provider.assert_not_awaited()
    isolated.vision.assert_not_awaited()


def test_failed_latest_current_does_not_try_older_current_image(isolated):
    isolated.bad_urls.add("new-2")
    response = asyncio.run(module.chat_with_vision(11, "演示", "看看", [{"url": "new-1"}, {"url": "new-2"}], 100))
    assert "没能读到" in response
    assert isolated.downloads == ["new-2"]
    isolated.storage.assert_not_called()
    isolated.vision.assert_not_awaited()


def test_existing_base64_vision_input_does_not_redownload(isolated):
    asyncio.run(module.chat_with_vision(11, "演示", "看看", ["data:image/jpeg;base64,current"], 100))
    assert isolated.downloads == []
    assert image_parts(isolated.vision.call_args.args[1]) == ["data:image/jpeg;base64,current"]


@pytest.mark.parametrize("model", ["gemini-compatible-model", "gpt-compatible-model", "vendor/vision"])
def test_vision_honors_separate_chat_completions_connection_without_model_guessing(isolated, monkeypatch, model):
    from plugins import model_transport
    requests = []
    async def complete(connection, messages, **options):
        requests.append((connection, messages, options))
        return '{"replies":["看清了"]}', {}
    monkeypatch.setattr(model_transport, "complete", complete)
    monkeypatch.setitem(module.ai_cfg, "vision", {
        "protocol": "chat_completions", "api_url": "https://vision.invalid/v1/chat/completions",
        "api_key": "test-vision-key", "model": model, "proxy": "",
    })
    reply = asyncio.run(module.chat_with_vision(11, "演示", "这是什么？", [{"url": "new"}], 100))
    assert "看清了" in reply
    assert len(requests) == 1
    connection, messages, options = requests[0]
    assert connection["api_url"] == "https://vision.invalid/v1/chat/completions"
    assert connection["api_key"] == "test-vision-key"
    assert options == {"model": model, "timeout": 120}
    assert connection["proxy"] == ""
    assert len(image_parts(messages)) == 1
    isolated.vision.assert_not_awaited()
    isolated.storage.assert_not_called()


def test_vision_native_overrides_do_not_use_primary_credentials_or_cache(isolated, monkeypatch):
    from plugins import model_routes
    requests = []
    async def dispatch(route, messages, timeout, search):
        requests.append((route, messages, timeout, search))
        return '{"replies":["看清了"]}', {}
    monkeypatch.setattr(model_routes, "_dispatch", dispatch)
    monkeypatch.setitem(module.ai_cfg, "vision", {
        "protocol": "gemini_native", "api_url": "https://vision-native.invalid/v1beta/models/fixture:generateContent",
        "api_key": "test-native-vision-key", "model": "fixture-native", "proxy": "",
    })
    reply = asyncio.run(module.chat_with_vision(11, "演示", "看看", ["data:image/jpeg;base64,current"], 100))
    assert "看清了" in reply
    route, messages, timeout, search = requests[0]
    assert (route.protocol, route.model) == ("google", "fixture-native")
    assert route.api_key == "test-native-vision-key"
    assert route.base_url == "https://vision-native.invalid/v1beta"
    assert timeout == 120 and not search
    assert image_parts(messages) == ["data:image/jpeg;base64,current"]
    assert "演示人格" in messages[0]["content"]
    isolated.vision.assert_not_awaited()


def test_vision_inherits_primary_explicit_protocol_and_keeps_vision_model(isolated, monkeypatch):
    from plugins import model_transport
    transport = AsyncMock(return_value=('{"replies":["看清了"]}', {}))
    monkeypatch.setattr(model_transport, "complete", transport)
    monkeypatch.setattr(module, "PRIMARY_PROTOCOL", "chat_completions")
    monkeypatch.setitem(module.ai_cfg, "vision", {"model": "gemini-compatible-vision"})
    asyncio.run(module.chat_with_vision(11, "演示", "看看", ["data:image/jpeg;base64,current"], 100))
    assert transport.call_args.kwargs["model"] == "gemini-compatible-vision"
    assert transport.call_args.args[0]["api_key"] == module.ai_cfg["api_key"]
    isolated.vision.assert_not_awaited()


def test_vision_provider_failure_does_not_fall_back_to_old_image_chat(isolated):
    isolated.vision.side_effect = RuntimeError("provider unavailable")
    response = asyncio.run(module.chat_with_vision(11, "演示", "这张图是什么？", [{"url": "new"}], 100))
    assert "没能识别" in response
    isolated.storage.assert_not_called()
    isolated.provider.assert_not_awaited()


def test_group_image_refs_can_exclude_current_message(isolated, monkeypatch):
    rows = [SimpleNamespace(id=2, message_id=200, is_bot=False, has_image=True, nickname="新图用户",
                            content="新图", image_url="new"),
            SimpleNamespace(id=1, message_id=100, is_bot=False, has_image=True, nickname="旧图用户",
                            content="旧图", image_url="old")]
    isolated.session.execute.return_value = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))
    monkeypatch.setattr(module, "_get_group_context", _group_context_loader)
    text, refs = asyncio.run(module._get_group_context(100, exclude_image_message_ids={200}))
    assert "新图用户" in text
    assert refs == [("old", "旧图用户")]


@pytest.mark.parametrize("raw,ref", [("", ""), ("[CQ:image,file=first][CQ:image,file=last]", "cached-first")])
def test_missing_latest_and_ambiguous_multi_image_cache_keep_failure_placeholder(isolated, monkeypatch, raw, ref):
    rows = [SimpleNamespace(id=2, message_id=200, is_bot=False, has_image=True, nickname="新图用户",
                            content="新图", image_url=ref, raw_message=raw),
            SimpleNamespace(id=1, message_id=100, is_bot=False, has_image=True, nickname="旧图用户",
                            content="旧图", image_url="old", raw_message="")]
    isolated.session.execute.return_value = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))
    text, refs = asyncio.run(_group_context_loader(100))
    assert refs == [("old", "旧图用户"), ("", "新图用户")]
    monkeypatch.setattr(module, "_get_group_context", AsyncMock(return_value=(text, refs)))
    isolated.session.execute.return_value = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
    asyncio.run(module.chat(11, "演示", "这张图是谁？", 100, persist_history=False))
    isolated.storage.assert_not_called()
    messages = isolated.provider.call_args.args[0]
    assert image_parts(messages) == []
    assert "读取失败" in messages[-1]["content"]


@pytest.mark.parametrize("quoted,current,expected", [
    ([{"url": "quoted"}], [{"url": "one"}, {"url": "two"}], ["quoted"]),
    ([], [{"url": "one"}, {"url": "two"}], ["two"]),
])
def test_handler_passes_only_selected_image_to_vision(isolated, monkeypatch, quoted, current, expected):
    bot = SimpleNamespace(self_id="500", get_group_member_info=AsyncMock(return_value={"nickname": "演示"}))
    event = SimpleNamespace(user_id=11, group_id=100, message_id=123, reply=None, to_me=True,
                            raw_message="", message=Message([MessageSegment.text("看看"), *[
                                MessageSegment.image(item["url"]) for item in current]]))
    monkeypatch.setattr(module, "_chat_trigger_kind", lambda event, bot: "direct_at")
    monkeypatch.setattr(module, "mark_group_trigger", lambda group: False)
    monkeypatch.setattr(module, "_is_group_context_enabled", lambda group: True)
    monkeypatch.setattr(module, "resolve_reply_context", AsyncMock(return_value=(SimpleNamespace(
        image_refs=quoted, message_id=99) if quoted else None)))
    monkeypatch.setattr(module, "format_reply_context", lambda context, text: text)
    vision = AsyncMock(return_value=None)
    monkeypatch.setattr(module, "chat_with_vision", vision)
    groups = ModuleType("plugins.group_chat")
    groups.reset_repeat_state = lambda group: None
    monkeypatch.setitem(sys.modules, groups.__name__, groups)
    asyncio.run(module.handle_chat(bot, event))
    selected = vision.call_args.args[3]
    assert [item["url"] for item in selected] == expected


def test_unresolved_reply_disables_current_and_historical_image_fallback(isolated, monkeypatch):
    bot = SimpleNamespace(self_id="500", get_group_member_info=AsyncMock(return_value={"nickname": "演示"}))
    event = SimpleNamespace(user_id=11, group_id=100, message_id=123, reply=None, to_me=True,
                            raw_message="[CQ:reply,id=99]这张图是谁？", message=Message([
                                MessageSegment.reply(99), MessageSegment.text("这张图是谁？"),
                                MessageSegment.image("unrelated-new")]))
    monkeypatch.setattr(module, "_chat_trigger_kind", lambda event, bot: "direct_at")
    monkeypatch.setattr(module, "mark_group_trigger", lambda group: False)
    monkeypatch.setattr(module, "_is_group_context_enabled", lambda group: True)
    monkeypatch.setattr(module, "resolve_reply_context", AsyncMock(return_value=None))
    chat = AsyncMock(return_value=None)
    vision = AsyncMock(return_value=None)
    monkeypatch.setattr(module, "chat", chat)
    monkeypatch.setattr(module, "chat_with_vision", vision)
    groups = ModuleType("plugins.group_chat")
    groups.reset_repeat_state = lambda group: None
    monkeypatch.setitem(sys.modules, groups.__name__, groups)
    asyncio.run(module.handle_chat(bot, event))
    vision.assert_not_awaited()
    assert chat.call_args.kwargs["context_images"] == []
    assert "引用的图片未能获取" in chat.call_args.args[2]


def test_known_but_unreadable_quote_never_selects_current_image(isolated):
    assert module._select_image_inputs([{"url": "new"}], [{"url": ""}], "看这个") == []


def test_real_quote_api_failure_returns_empty_context_but_never_uses_another_image(isolated, monkeypatch):
    from plugins.chat_coordination import resolve_reply_context
    bot = SimpleNamespace(self_id="500", get_group_member_info=AsyncMock(return_value={"nickname": "演示"}),
                          call_api=AsyncMock(side_effect=OSError("get_msg failed")))
    event = SimpleNamespace(user_id=11, group_id=100, message_id=123, reply=None, to_me=True,
                            raw_message="[CQ:reply,id=99]这张图是谁？", message=Message([
                                MessageSegment.reply(99), MessageSegment.text("这张图是谁？"),
                                MessageSegment.image("unrelated-new")]))
    monkeypatch.setattr(module, "_chat_trigger_kind", lambda event, bot: "direct_at")
    monkeypatch.setattr(module, "mark_group_trigger", lambda group: False)
    monkeypatch.setattr(module, "_is_group_context_enabled", lambda group: True)
    monkeypatch.setattr(module, "resolve_reply_context", resolve_reply_context)
    chat = AsyncMock(return_value=None)
    vision = AsyncMock(return_value=None)
    monkeypatch.setattr(module, "chat", chat)
    monkeypatch.setattr(module, "chat_with_vision", vision)
    groups = ModuleType("plugins.group_chat")
    groups.reset_repeat_state = lambda group: None
    monkeypatch.setitem(sys.modules, groups.__name__, groups)
    asyncio.run(module.handle_chat(bot, event))
    bot.call_api.assert_awaited_once_with("get_msg", message_id=99)
    vision.assert_not_awaited()
    assert chat.call_args.kwargs["context_images"] == []
    assert "引用的图片未能获取" in chat.call_args.args[2]
