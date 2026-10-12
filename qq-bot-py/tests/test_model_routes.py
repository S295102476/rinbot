"""Purpose routing tests use synthetic credentials and fake transports only."""
import asyncio
from dataclasses import replace
import json
import pytest
import httpx
from plugins import model_routes as routes


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(routes, "_CONFIG", {"ai": {
        "antigravity": {"model": "custom-chat", "api_key": "test-ag-key", "api_url": "https://ag.invalid/v1/chat/completions"},
        "google": {"api_key": "test-google-key", "base_url": "https://google.invalid/v1beta"},
        "openai_responses": {"api_key": "test-gpt-key", "base_url": "https://gpt.invalid/v1", "model": "gpt-5.6-sol"}},
        "agent": {"model_routes": {}}})
    monkeypatch.setattr(routes, "_OVERRIDES", {})
    monkeypatch.setattr(routes, "_VERSIONS", {})
    monkeypatch.setattr(routes, "_SLOTS", {})


def test_legacy_chat_model_never_changes_other_purposes():
    assert routes.resolve("group_chat").model == "custom-chat"
    routes._OVERRIDES["group_chat"] = {"model": "new-chat"}
    assert routes.resolve("group_chat").model == "new-chat"
    assert routes.resolve("memory").model == "gemini-3.8-flash-high"
    assert routes.resolve("emotion").model == "gemini-3.8-flash-low"
    assert routes.resolve("expert").model == "gpt-5.6-sol"
    public = json.dumps([routes.public_route(role) for role in routes.DEFAULTS])
    assert "test-ag-key" not in public and "ag.invalid" not in public
    assert "test-gpt-key" not in repr(routes.resolve("expert"))


def test_provider_switch_does_not_reuse_other_provider_secret():
    routes._CONFIG["agent"]["model_routes"]["group_chat"] = {
        "provider": "antigravity", "api_key": "test-special-ag", "base_url": "https://special.invalid"}
    routes._OVERRIDES["group_chat"] = {"provider": "google", "protocol": "google", "model": "a-model"}
    result = routes.resolve("group_chat")
    assert result.api_key == "test-google-key"
    assert result.base_url == "https://google.invalid/v1beta"


@pytest.mark.parametrize("role", ["group_chat", "memory", "emotion", "meme"])
@pytest.mark.parametrize("model", ["gemini-compatible-name", "gpt-compatible-name", "vendor/custom-chat"])
def test_public_primary_routes_inherit_configured_model_with_explicit_protocol(role, model):
    routes._CONFIG["ai"].update(model=model, api_key="test-primary-key",
                              api_url="https://primary.invalid/v1/chat/completions")
    routes._CONFIG["agent"]["model_routes"][role] = {"provider": "primary", "protocol": "openai"}
    route = routes.resolve(role)
    assert (route.provider, route.protocol, route.model) == ("primary", "openai", model)
    assert route.api_key == "test-primary-key"
    assert route.base_url == "https://primary.invalid/v1/chat/completions"


def test_empty_primary_model_does_not_inherit_private_provider_default():
    routes._CONFIG["ai"]["model"] = ""
    routes._CONFIG["agent"]["model_routes"]["memory"] = {"provider": "primary", "protocol": "openai", "model": ""}
    assert routes.resolve("memory").model == ""
    routes._CONFIG["ai"]["model"] = "configured-primary"
    assert routes.resolve("memory").model == "configured-primary"


def test_primary_model_overrides_remain_separate_from_shared_model():
    routes._CONFIG["ai"]["model"] = "shared-primary"
    routes._CONFIG["agent"]["model_routes"]["memory"] = {"provider": "primary", "protocol": "openai", "model": "dedicated-memory"}
    routes._OVERRIDES["emotion"] = {"provider": "primary", "protocol": "openai"}
    assert routes.resolve("memory").model == "dedicated-memory"
    assert routes.resolve("emotion").model == "shared-primary"
    routes._OVERRIDES["memory"] = {"model": "console-memory"}
    assert routes.resolve("memory").model == "console-memory"
    assert routes.resolve("group_chat").model == "custom-chat"


def test_explicit_optional_responses_routes_inherit_deployers_model():
    routes._CONFIG["ai"]["openai_responses"]["model"] = "custom-expert-model"
    routes._CONFIG["agent"]["model_routes"]["expert"] = {
        "provider": "openai_responses", "protocol": "responses", "enabled": False,
    }
    route = routes.resolve("expert")
    assert route.model == "custom-expert-model"
    assert route.protocol == "responses" and not route.enabled
    # Legacy private configs without an explicit purpose keep existing defaults.
    assert routes.resolve("development").model == "gpt-5.6-sol"


def test_public_example_uses_own_primary_and_disables_optional_paid_routes(monkeypatch):
    from pathlib import Path
    import yaml
    template = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.example.yaml").read_text(encoding="utf-8"))
    template["ai"].update(api_url="https://public-provider.invalid/v1/chat/completions",
                          api_key="test-public-key", model="public-model")
    monkeypatch.setattr(routes, "_CONFIG", template)
    for role in ("group_chat", "memory", "emotion", "meme"):
        route = routes.resolve(role)
        assert (route.provider, route.protocol, route.model) == ("primary", "openai", "public-model")
        assert route.api_key == "test-public-key" and route.enabled
    for role in ("embedding", "expert", "development"):
        assert not routes.resolve(role).enabled
    assert not template["agent"]["memory_v2"]["enabled"]
    assert template["agent"]["game_tools"]["enabled"]


def test_missing_primary_model_fails_before_dispatch_with_actionable_config_path(monkeypatch):
    routes._CONFIG["ai"].update(model="", api_key="test-primary-key",
                              api_url="https://primary.invalid/v1/chat/completions")
    routes._CONFIG["agent"]["model_routes"]["memory"] = {"provider": "primary", "protocol": "openai"}
    async def unexpected_network(*args, **kwargs):
        raise AssertionError("incomplete config must never dispatch")
    monkeypatch.setattr(routes, "_dispatch", unexpected_network)
    with pytest.raises(routes.RouteError, match="configure ai credentials, endpoint and model"):
        asyncio.run(routes.complete("memory", []))


@pytest.mark.parametrize("patch", [{"api_key":"secret"}, {"base_url":"https://evil.invalid"},
    {"timeout":True}, {"concurrency":0}, {"model":"newline\nkey"},
    {"provider":"google", "protocol":"responses"}])
def test_public_edits_have_strict_types(patch):
    with pytest.raises(ValueError):
        routes.validate_patch(patch)


def test_native_tools_are_normalized_for_host_validation():
    data = {"choices": [{"message": {"content": "already played", "tool_calls": [
        {"function": {"name":"minigame_start", "arguments":'{"source_message_id":12,"game":"gomoku"}'}}
    ]}}]}
    result = json.loads(routes._openai_result(data))
    assert result["action"] == "tool"
    assert result["tool_calls"][0]["arguments"]["source_message_id"] == 12


def test_openai_protocol_uses_role_model_and_explicit_output_budget(monkeypatch):
    seen = []
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            seen.append((url, kwargs))
            return httpx.Response(200, json={"choices":[{"message":{"content":"happy"}}]}, request=httpx.Request("POST",url))
    monkeypatch.setattr(routes.httpx, "AsyncClient", Client)
    result = asyncio.run(routes._dispatch(routes.resolve("emotion"), [{"role":"user","content":"test"}], 15, False))
    assert result[0] == "happy"
    assert seen[0][1]["json"]["model"] == "gemini-3.8-flash-low"
    assert seen[0][1]["json"]["max_tokens"] == 128
    assert "tools" not in seen[0][1]["json"]


def test_timeout_cancels_provider_and_releases_concurrency(monkeypatch):
    cancelled=[]
    async def dispatch(*args):
        try: await asyncio.Event().wait()
        finally: cancelled.append(True)
    monkeypatch.setattr(routes,"_dispatch",dispatch)
    async def run():
        for _ in range(2):
            with pytest.raises(TimeoutError):
                await routes.complete("emotion", [], timeout=0.01)
        assert len(cancelled)==2
        assert all(slot._value == 2 for slot in routes._SLOTS.values())
    asyncio.run(run())


def test_errors_never_echo_upstream_secret(monkeypatch):
    async def dispatch(*args):
        raise RuntimeError("provider echoed test-ag-key and private chat")
    monkeypatch.setattr(routes,"_dispatch",dispatch)
    with pytest.raises(routes.RouteError) as error:
        asyncio.run(routes.complete("emotion", []))
    assert "test-ag-key" not in str(error.value) and "private chat" not in str(error.value)


def test_route_update_and_legacy_console_commit_are_atomic(monkeypatch):
    from plugins import console_state as state
    from test_console_state import AsyncSessionAdapter
    from sqlalchemy import create_engine, select
    from sqlalchemy.pool import StaticPool
    engine=create_engine("sqlite://",poolclass=StaticPool)
    state.db.Base.metadata.create_all(engine,tables=state.CONSOLE_TABLES)
    async def factory(): return AsyncSessionAdapter(engine)
    monkeypatch.setattr(state.db,"get_session",factory)
    store=state.SettingsStore({},factory)
    async def run():
        await routes.update("memory",{"model":"separate"},actor="test",version=0)
        with pytest.raises(state.VersionConflict):
            await routes.update("memory",{"model":"lost"},actor="test",version=0)
        await store.update_global({"model":"legacy-chat"},version=0)
        async with await factory() as session:
            row=(await session.execute(select(state.ConsoleSetting).where(state.ConsoleSetting.key=="route:group_chat"))).scalar_one()
            assert json.loads(row.values_json)["model"]=="legacy-chat"
        await routes.load()
        assert routes.resolve("memory").model=="separate"
        assert routes.resolve("group_chat").model=="legacy-chat"
        value=await routes.update("group_chat",{"timeout":None,"model":None,"provider":None,"protocol":None},actor="test",version=routes.resolve("group_chat").version)
        assert value["timeout"]==90
        for role,patch in [("embedding",{"max_output_tokens":3072}),
                           ("group_chat",{"protocol":"embedding"})]:
            with pytest.raises(ValueError):
                await routes.update(role,patch,actor="test",version=routes.resolve(role).version)
    asyncio.run(run())
