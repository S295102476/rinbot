import yaml
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

with open("config.yaml", "r", encoding="utf-8") as f:
    _cfg = yaml.safe_load(f)["database"]

from sqlalchemy.engine import URL
_url = URL.create("mysql+aiomysql", username=_cfg["user"], password=_cfg["password"],
                  host=_cfg["host"], port=int(_cfg["port"]), database=_cfg["database"],
                  query={"charset": "utf8mb4"})

engine = create_async_engine(_url, pool_size=5, max_overflow=10, pool_recycle=1800)
SessionFactory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_session() -> AsyncSession:
    return SessionFactory()


# ---------- 共享 ORM 模型 ----------
from datetime import date, datetime
from sqlalchemy import BigInteger, Integer, Float, String, Text, Date, DateTime, Boolean, select, func, delete, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column


class Blacklist(Base):
    """用户黑名单 — 被加入的 QQ 号对 bot 完全不可见"""
    __tablename__ = "blacklist"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    reason: Mapped[str] = mapped_column(String(200), default="")  # 手动/auto_spam
    nickname: Mapped[str] = mapped_column(String(100), default="")   # 封禁时的用户昵称
    group_id: Mapped[int] = mapped_column(BigInteger, nullable=True)  # 在哪个群被封禁
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class ChatHistory(Base):
    """对话历史 — 按 user_id 隔离"""
    __tablename__ = "chat_history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class GroupMessage(Base):
    """群聊消息记录 — 带昵称"""
    __tablename__ = "group_messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, index=True)
    message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    user_id: Mapped[int] = mapped_column(BigInteger)
    nickname: Mapped[str] = mapped_column(String(100), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    raw_message: Mapped[str] = mapped_column(Text, default="")
    at_users: Mapped[str] = mapped_column(Text, default="[]")
    has_image: Mapped[bool] = mapped_column(Boolean, default=False)
    image_url: Mapped[str] = mapped_column(Text, default="")   # 第一张图的 URL，用于上下文传图
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)  # 是否是 bot 自己的发言
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class AgentRun(Base):
    """审计 Agent 的决策与工具结果，不保存提示词中的密钥或完整消息。"""
    __tablename__ = "agent_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(20), default="group", index=True)
    group_id: Mapped[int] = mapped_column(BigInteger, default=0, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, default=0, index=True)
    action: Mapped[str] = mapped_column(String(20), default="ignore")
    status: Mapped[str] = mapped_column(String(20), default="ok")
    iterations: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class AgentContextSummary(Base):
    """Persisted rolling summary for one Agent-enabled group."""
    __tablename__ = "agent_context_summaries"
    group_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    covered_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class AgentMemoryEpisode(Base):
    """Durable, bounded summary of one older segment in an Agent group."""
    __tablename__ = "agent_memory_episodes"
    __table_args__ = (
        UniqueConstraint("group_id", "end_message_id", name="uq_agent_memory_episode_end"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, index=True)
    start_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    end_message_id: Mapped[int] = mapped_column(BigInteger, default=0, index=True)
    participant_ids: Mapped[str] = mapped_column(Text, default="[]")
    summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class AgentPersonFact(Base):
    """A source-backed, group-scoped durable fact about one participant."""
    __tablename__ = "agent_person_facts"
    __table_args__ = (
        UniqueConstraint(
            "group_id", "user_id", "fingerprint", name="uq_agent_person_fact_fingerprint"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), default="")
    category: Mapped[str] = mapped_column(String(32), default="general")
    fact: Mapped[str] = mapped_column(String(500), default="")
    importance: Mapped[int] = mapped_column(Integer, default=1)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    source_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class AgentGlobalPersonFact(Base):
    """Stable, source-backed fact shared by Agent groups for one user."""
    __tablename__ = "agent_global_person_facts"
    __table_args__ = (
        UniqueConstraint("user_id", "fingerprint", name="uq_agent_global_fact_fingerprint"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), default="")
    category: Mapped[str] = mapped_column(String(32), default="general")
    fact: Mapped[str] = mapped_column(String(500), default="")
    importance: Mapped[int] = mapped_column(Integer, default=1)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    source_group_id: Mapped[int] = mapped_column(BigInteger, default=0, index=True)
    source_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class AgentPersonaAffinity(Base):
    """Affinity shared across groups but isolated by active persona."""
    __tablename__ = "agent_persona_affinities"
    __table_args__ = (
        UniqueConstraint("persona_id", "user_id", name="uq_agent_persona_affinity_user"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    persona_id: Mapped[str] = mapped_column(String(64), default="rin", index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    affinity_score: Mapped[float] = mapped_column(Float, default=0.0)
    last_delta: Mapped[float] = mapped_column(Float, default=0.0)
    last_reason: Mapped[str] = mapped_column(String(300), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class AgentPersonaRelationshipState(Base):
    """Per-persona, per-group interaction counters and relation metadata."""
    __tablename__ = "agent_persona_relationship_states"
    __table_args__ = (
        UniqueConstraint(
            "persona_id", "group_id", "user_id",
            name="uq_agent_persona_relationship_group_user",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    persona_id: Mapped[str] = mapped_column(String(64), default="rin", index=True)
    group_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    explicit_interaction_count: Mapped[int] = mapped_column(Integer, default=0)
    affinity_score: Mapped[float] = mapped_column(Float, default=0.0)
    last_delta: Mapped[float] = mapped_column(Float, default=0.0)
    last_reason: Mapped[str] = mapped_column(String(300), default="")
    last_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_interaction_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class AgentPersonaSwitch(Base):
    """Append-only audit trail for persona changes."""
    __tablename__ = "agent_persona_switches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    from_persona_id: Mapped[str] = mapped_column(String(64), default="")
    to_persona_id: Mapped[str] = mapped_column(String(64), index=True)
    actor_user_id: Mapped[int] = mapped_column(BigInteger, default=0, index=True)
    note: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)


class DutyRosterEntry(Base):
    """Global daily persona assignment used by the duty roster scheduler."""
    __tablename__ = "duty_roster_entries"

    duty_date: Mapped[date] = mapped_column(Date, primary_key=True)
    persona_id: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(20), default="manual")
    actor_user_id: Mapped[int] = mapped_column(BigInteger, default=0)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class AgentRelationshipState(Base):
    """Structured per-group interaction state; never inferred from sign-in points."""
    __tablename__ = "agent_relationship_states"
    __table_args__ = (
        UniqueConstraint("group_id", "user_id", name="uq_agent_relationship_group_user"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    explicit_interaction_count: Mapped[int] = mapped_column(Integer, default=0)
    affinity_score: Mapped[float] = mapped_column(Float, default=0.0)
    last_delta: Mapped[float] = mapped_column(Float, default=0.0)
    last_reason: Mapped[str] = mapped_column(String(300), default="")
    last_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_interaction_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class AgentFollowup(Base):
    """A persisted question waiting for a user's answer or timed reminder."""
    __tablename__ = "agent_followups"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    source_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    question: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    stage: Mapped[int] = mapped_column(Integer, default=0)
    next_due_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


# Agent 群的持久化上下文窗口；热缓存和摘要层在此基础上工作。
GROUP_MESSAGE_HISTORY_KEEP = 500


async def prune_group_messages(
    group_id: int,
    keep: int = GROUP_MESSAGE_HISTORY_KEEP,
    session: AsyncSession | None = None,
) -> None:
    """Keep only the latest group messages for one group."""

    async def _prune(active_session: AsyncSession, should_commit: bool) -> None:
        group_id_int = int(group_id)
        count = (await active_session.execute(
            select(func.count()).select_from(GroupMessage)
            .where(GroupMessage.group_id == group_id_int)
        )).scalar() or 0
        overage = int(count) - int(keep)
        if overage <= 0:
            return

        old_ids = (await active_session.execute(
            select(GroupMessage.id)
            .where(GroupMessage.group_id == group_id_int)
            .order_by(GroupMessage.id.asc())
            .limit(overage)
        )).scalars().all()
        if not old_ids:
            return

        await active_session.execute(delete(GroupMessage).where(GroupMessage.id.in_(old_ids)))
        if should_commit:
            await active_session.commit()

    if session is not None:
        await _prune(session, False)
        return

    managed_session = await get_session()
    try:
        await _prune(managed_session, True)
    finally:
        await managed_session.close()


class UserMemory(Base):
    """用户记忆"""
    __tablename__ = "user_memory"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    content: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


# ---------- 表情包元数据 ----------

class MemeItem(Base):
    """MinIO 表情包对象的可管理元数据。图片本体仍存 MinIO。"""
    __tablename__ = "meme_items"
    __table_args__ = (
        UniqueConstraint("bucket", "object_name", name="uq_meme_bucket_object"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bucket: Mapped[str] = mapped_column(String(80), default="memes", index=True)
    object_name: Mapped[str] = mapped_column(String(255), index=True)
    md5: Mapped[str] = mapped_column(String(64), default="", index=True)
    content_type: Mapped[str] = mapped_column(String(80), default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    emotion: Mapped[str] = mapped_column(String(30), default="", index=True)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    source_group_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    source_user_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    send_count: Mapped[int] = mapped_column(Integer, default=0)
    last_sent_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    classified_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    tags: Mapped[str] = mapped_column(Text, default="[]")
    note: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


# ---------- Wiki 数据模型 ----------

class WikiCard(Base):
    """杀戮尖塔2 卡牌数据"""
    __tablename__ = "wiki_cards"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    card_id: Mapped[str] = mapped_column(String(120), index=True)
    name: Mapped[str] = mapped_column(String(100), index=True)
    color: Mapped[str] = mapped_column(String(50), default="")
    rarity: Mapped[str] = mapped_column(String(50), default="")
    card_type: Mapped[str] = mapped_column(String(50), default="")
    cost: Mapped[str] = mapped_column(String(20), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    description_raw: Mapped[str] = mapped_column(Text, default="")
    upgrade_ref: Mapped[str] = mapped_column(String(120), default="")
    compendium_order: Mapped[int] = mapped_column(Integer, default=0)
    image: Mapped[str] = mapped_column(String(200), default="")
    page: Mapped[str] = mapped_column(String(200), default="")


class WikiRelic(Base):
    """杀戮尖塔2 遗物数据"""
    __tablename__ = "wiki_relics"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    relic_id: Mapped[str] = mapped_column(String(120), index=True)
    name: Mapped[str] = mapped_column(String(100), index=True)
    pool: Mapped[str] = mapped_column(String(50), default="")
    tier: Mapped[str] = mapped_column(String(50), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    description_raw: Mapped[str] = mapped_column(Text, default="")
    flavor: Mapped[str] = mapped_column(Text, default="")
    ancient: Mapped[str] = mapped_column(String(50), default="")
    compendium_order: Mapped[int] = mapped_column(Integer, default=0)
    image: Mapped[str] = mapped_column(String(200), default="")
    page: Mapped[str] = mapped_column(String(200), default="")


class WikiPotion(Base):
    """杀戮尖塔2 药水数据"""
    __tablename__ = "wiki_potions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    potion_id: Mapped[str] = mapped_column(String(120), index=True)
    name: Mapped[str] = mapped_column(String(100), index=True)
    color: Mapped[str] = mapped_column(String(50), default="")
    tier: Mapped[str] = mapped_column(String(50), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    description_raw: Mapped[str] = mapped_column(Text, default="")
    compendium_order: Mapped[int] = mapped_column(Integer, default=0)
    image: Mapped[str] = mapped_column(String(200), default="")
    page: Mapped[str] = mapped_column(String(200), default="")


class WikiModifier(Base):
    """杀戮尖塔2 每日挑战词条数据"""
    __tablename__ = "wiki_modifiers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    modifier_id: Mapped[str] = mapped_column(String(120), index=True)
    name: Mapped[str] = mapped_column(String(100), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    image: Mapped[str] = mapped_column(String(200), default="")
    kind: Mapped[str] = mapped_column(String(20), default="")


class WikiMonster(Base):
    """杀戮尖塔2 怪物数据"""
    __tablename__ = "wiki_monsters"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    monster_id: Mapped[str] = mapped_column(String(120), index=True)
    name: Mapped[str] = mapped_column(String(100), index=True)
    image: Mapped[str] = mapped_column(String(200), default="")
    min_hp: Mapped[str] = mapped_column(String(20), default="")
    max_hp: Mapped[str] = mapped_column(String(20), default="")
    ascender_min_hp: Mapped[str] = mapped_column(String(20), default="")
    ascender_max_hp: Mapped[str] = mapped_column(String(20), default="")
    tier: Mapped[str] = mapped_column(String(50), default="")
    power: Mapped[str] = mapped_column(String(50), default="")
    stage: Mapped[str] = mapped_column(String(100), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    page: Mapped[str] = mapped_column(String(200), default="")


# ---------- GBVSR 帧数数据 ----------

class GBVSRFrameMove(Base):
    """GBVSR 角色帧数与本地 hitbox 图片数据"""
    __tablename__ = "gbvsr_frame_moves"
    __table_args__ = (
        UniqueConstraint("character", "input_name", name="uq_gbvsr_character_input"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    character: Mapped[str] = mapped_column(String(80), index=True)
    section: Mapped[str] = mapped_column(String(80), default="")
    input_name: Mapped[str] = mapped_column(String(50), index=True)
    move_name: Mapped[str] = mapped_column(String(120), default="")
    damage: Mapped[str] = mapped_column(String(120), default="")
    guard: Mapped[str] = mapped_column(String(80), default="")
    startup: Mapped[str] = mapped_column(String(120), default="")
    active: Mapped[str] = mapped_column(String(120), default="")
    recovery: Mapped[str] = mapped_column(String(120), default="")
    on_block: Mapped[str] = mapped_column(String(120), default="")
    on_hit: Mapped[str] = mapped_column(String(120), default="")
    on_counter_hit: Mapped[str] = mapped_column(String(120), default="")
    level: Mapped[str] = mapped_column(String(50), default="")
    invuln: Mapped[str] = mapped_column(String(200), default="")
    combo_limit_scaling: Mapped[str] = mapped_column(String(50), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    notes_zh: Mapped[str] = mapped_column(Text, default="")
    image_paths: Mapped[str] = mapped_column(Text, default="[]")
    hitbox_paths: Mapped[str] = mapped_column(Text, default="[]")
    aliases: Mapped[str] = mapped_column(Text, default="[]")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class GBVSROverview(Base):
    """GBVSR 角色基础概览数据"""
    __tablename__ = "gbvsr_overviews"
    __table_args__ = (
        UniqueConstraint("character", name="uq_gbvsr_overview_character"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    character: Mapped[str] = mapped_column(String(80), index=True)
    display_name: Mapped[str] = mapped_column(String(120), default="")
    health: Mapped[str] = mapped_column(String(50), default="")
    backdash: Mapped[str] = mapped_column(String(120), default="")
    jump_startup: Mapped[str] = mapped_column(String(50), default="")
    walk_speed: Mapped[str] = mapped_column(String(50), default="")
    backwalk_speed: Mapped[str] = mapped_column(String(50), default="")
    initial_dash_speed: Mapped[str] = mapped_column(String(50), default="")
    dash_acceleration: Mapped[str] = mapped_column(String(50), default="")
    jump_height: Mapped[str] = mapped_column(String(50), default="")
    forward_jump_distance: Mapped[str] = mapped_column(String(50), default="")
    backward_jump_distance: Mapped[str] = mapped_column(String(50), default="")
    superjump_height: Mapped[str] = mapped_column(String(50), default="")
    forward_superjump_distance: Mapped[str] = mapped_column(String(50), default="")
    backward_superjump_distance: Mapped[str] = mapped_column(String(50), default="")
    cl_proximity_range: Mapped[str] = mapped_column(String(50), default="")
    cm_proximity_range: Mapped[str] = mapped_column(String(50), default="")
    ch_proximity_range: Mapped[str] = mapped_column(String(50), default="")
    portrait_path: Mapped[str] = mapped_column(String(300), default="")
    source: Mapped[str] = mapped_column(String(300), default="")
    raw_data: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


# ---------- 娶群友 ----------

from sqlalchemy import Date, UniqueConstraint
from datetime import date


class DailyWife(Base):
    """每日娶群友记录"""
    __tablename__ = "daily_wife"
    __table_args__ = (UniqueConstraint("user_id", "group_id", "date", name="uq_wife_per_day"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    group_id: Mapped[int] = mapped_column(BigInteger, index=True)
    wife_id: Mapped[int] = mapped_column(BigInteger)
    wife_nickname: Mapped[str] = mapped_column(String(100), default="")
    date: Mapped[date] = mapped_column(Date)


# ---------- 涩图记录 ----------

class SetuRecord(Base):
    """涩图发送记录 — 用于去重"""
    __tablename__ = "setu_record"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pid: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    uid: Mapped[int] = mapped_column(BigInteger, default=0)
    title: Mapped[str] = mapped_column(String(200), default="")
    author: Mapped[str] = mapped_column(String(100), default="")
    url: Mapped[str] = mapped_column(String(500), default="")
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


# ---------- 排行榜缓存 ----------

class RankingCache(Base):
    """Pixiv 排行榜图片缓存 — 下载一次上传 MinIO，多群复用 URL"""
    __tablename__ = "ranking_cache"
    __table_args__ = (UniqueConstraint("rank_date", "mode", "rank_pos", name="uq_ranking_mode_daily"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rank_date: Mapped[date] = mapped_column(Date, index=True)
    mode: Mapped[str] = mapped_column(String(20), default="day", index=True)  # day/week/month/week_original/week_rookie
    rank_pos: Mapped[int] = mapped_column(Integer)          # 排名 1~N
    pid: Mapped[int] = mapped_column(BigInteger, index=True)
    uid: Mapped[int] = mapped_column(BigInteger, default=0)
    title: Mapped[str] = mapped_column(String(200), default="")
    author: Mapped[str] = mapped_column(String(100), default="")
    total_view: Mapped[int] = mapped_column(Integer, default=0)
    total_bookmarks: Mapped[int] = mapped_column(Integer, default=0)
    pixiv_url: Mapped[str] = mapped_column(String(500), default="")
    minio_url: Mapped[str] = mapped_column(String(1000), default="")  # 预签名 URL
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


# ---------- 🦌 鹿历 ----------

class DeerMark(Base):
    """鹿历打标记录 — 每人每群每天一条，upsert 覆盖。"""
    __tablename__ = "deer_mark"
    __table_args__ = (UniqueConstraint("user_id", "group_id", "year", "month", "day", name="uq_deer_per_day"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    group_id: Mapped[int] = mapped_column(BigInteger, index=True)
    year: Mapped[int] = mapped_column(Integer)
    month: Mapped[int] = mapped_column(Integer)
    day: Mapped[int] = mapped_column(Integer)
    mark: Mapped[str] = mapped_column(String(10))  # 'check' 或 'cross'
    # 补签记录可通过另一种补签命令改正；正常当天记录不可改。
    is_backfill: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


# ---------- 禁言风控 ----------

class UserRisk(Base):
    """用户风控值 — 全局（跨群共享）"""
    __tablename__ = "user_risk"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    risk_score: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class UserAffinity(Base):
    """用户好感度 — 全局（跨群共享），范围 -100 到 100。"""
    __tablename__ = "user_affinity"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    affinity_score: Mapped[float] = mapped_column(Float, default=0.0)
    last_delta: Mapped[float] = mapped_column(Float, default=0.0)
    last_reason: Mapped[str] = mapped_column(String(300), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class UserMuteRecord(Base):
    """用户禁言次数 — 按群隔离，每周一归零"""
    __tablename__ = "user_mute_record"
    __table_args__ = (UniqueConstraint("user_id", "group_id", name="uq_mute_user_group"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    group_id: Mapped[int] = mapped_column(BigInteger, index=True)
    mute_count: Mapped[int] = mapped_column(Integer, default=0)
    last_muted_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
