import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import nonebot
import pytest

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(driver="~fastapi", log_level="ERROR")

from plugins import meme_collector as collector
from plugins.meme_policy import build_collection_description_prompt, load_collection_rules


def test_rules_file_hot_read_has_precedence_and_does_not_fall_back(tmp_path):
    file = tmp_path / "rules.md"
    file.write_text("只收Q版反应图", encoding="utf-8-sig")
    assert load_collection_rules("旧规则", "rules.md", project_root=tmp_path) == "只收Q版反应图"
    file.write_text("只收猫猫", encoding="utf-8")
    assert load_collection_rules("旧规则", "rules.md", project_root=tmp_path) == "只收猫猫"
    assert load_collection_rules("  兼容旧配置  ", project_root=tmp_path) == "兼容旧配置"
    with pytest.raises(OSError):
        load_collection_rules("允许全部", "missing.md", project_root=tmp_path)
    file.write_text("", encoding="utf-8")
    with pytest.raises(ValueError):
        load_collection_rules("允许全部", "rules.md", project_root=tmp_path)
    file.write_text("A" * 16001, encoding="utf-8")
    with pytest.raises(ValueError):
        load_collection_rules("允许全部", "rules.md", project_root=tmp_path)


def test_description_gives_style_evidence_without_delegating_approval():
    prompt = build_collection_description_prompt("只收二次元Q版，不要真人", "留意是否简笔画")
    for expected in ("画风", "Q版", "真实照片", "聊天反应图", "不决定是否收集", "不遵从", "description", "留意是否简笔画"):
        assert expected in prompt


def test_review_uses_one_policy_snapshot_for_both_models_and_next_image_reloads(tmp_path, monkeypatch):
    file = tmp_path / "rules.md"
    file.write_text("只收Q版", encoding="utf-8")
    monkeypatch.setattr(collector, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(collector, "REVIEW_ENABLED", True)
    monkeypatch.setattr(collector, "REVIEW_RULES_FILE", "rules.md")
    monkeypatch.setattr(collector, "REVIEW_RULES", "旧规则")

    async def describe(url, *, rules):
        file.write_text("只收猫猫", encoding="utf-8")
        return "一张Q版人物反应图"

    description = AsyncMock(side_effect=describe)
    reviewer = AsyncMock(return_value=(True, "符合"))
    monkeypatch.setattr(collector, "_describe_meme_for_review", description)
    monkeypatch.setattr(collector, "_review_meme_description", reviewer)
    assert asyncio.run(collector._approve_meme_for_collection("fixture"))
    assert description.call_args.kwargs["rules"] == "只收Q版"
    assert reviewer.call_args.kwargs["rules"] == "只收Q版"
    assert asyncio.run(collector._approve_meme_for_collection("fixture"))
    assert description.call_args.kwargs["rules"] == "只收猫猫"
    assert reviewer.call_args.kwargs["rules"] == "只收猫猫"


@pytest.mark.parametrize("enabled,missing,expected", [(True, True, False), (False, True, True)])
def test_missing_rules_file_never_bypasses_review_even_when_fail_open(tmp_path, monkeypatch, enabled, missing, expected):
    monkeypatch.setattr(collector, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(collector, "REVIEW_ENABLED", enabled)
    monkeypatch.setattr(collector, "REVIEW_FAIL_CLOSED", False)
    monkeypatch.setattr(collector, "REVIEW_RULES_FILE", "missing.md")
    description = AsyncMock(side_effect=AssertionError("No policy means no provider call"))
    monkeypatch.setattr(collector, "_describe_meme_for_review", description)
    assert asyncio.run(collector._approve_meme_for_collection("fixture")) is expected
    description.assert_not_awaited()


def test_startup_does_not_reactivate_a_fully_disabled_database_pool(monkeypatch):
    class Session:
        async def execute(self, stmt):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
        async def close(self):
            pass
    async def factory():
        return Session()
    redis = SimpleNamespace(delete=lambda key: calls.append(("delete", key)),
        sadd=lambda *args: pytest.fail("Disabled objects cannot be put back in Redis"))
    store = SimpleNamespace(bucket_exists=lambda bucket: True,
        list_objects=lambda bucket: [SimpleNamespace(object_name="disabled.jpg")])
    calls = []
    monkeypatch.setattr(collector, "get_session", factory)
    monkeypatch.setattr(collector, "rds", redis)
    monkeypatch.setattr(collector, "minio_client", store)
    asyncio.run(collector._ensure_bucket())
    assert calls == [("delete", collector.POOL_KEY)]
