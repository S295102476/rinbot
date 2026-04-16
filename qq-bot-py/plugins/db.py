import yaml
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

with open("config.yaml", "r", encoding="utf-8") as f:
    _cfg = yaml.safe_load(f)["database"]

_url = (
    f"mysql+aiomysql://{_cfg['user']}:{_cfg['password']}"
    f"@{_cfg['host']}:{_cfg['port']}/{_cfg['database']}"
    "?charset=utf8mb4"
)

engine = create_async_engine(_url, pool_size=5, max_overflow=10, pool_recycle=1800)
SessionFactory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_session() -> AsyncSession:
    return SessionFactory()


# ---------- 共享 ORM 模型 ----------
from datetime import datetime
from sqlalchemy import BigInteger, Integer, String, Text, DateTime, Boolean
from sqlalchemy.orm import Mapped, mapped_column


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
    user_id: Mapped[int] = mapped_column(BigInteger)
    nickname: Mapped[str] = mapped_column(String(100), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    has_image: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class UserMemory(Base):
    """用户记忆"""
    __tablename__ = "user_memory"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    content: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


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
