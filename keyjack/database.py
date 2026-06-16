from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Boolean, DateTime
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from .config import DATABASE_URL

engine = create_async_engine(DATABASE_URL)
SessionLocal = async_sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False
)

class Base(DeclarativeBase):
    pass

class ApiKey(Base):
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, index=True)
    provider = Column(String, default="openrouter")
    key_value = Column(String, unique=True, index=True)
    daily_limit = Column(Integer, default=100)
    current_usage = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    last_reset = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    def reset_if_needed(self):
        now = datetime.now(timezone.utc)
        # last_reset might be naive or aware depending on how SQLite stores it and how SQLAlchemy retrieves it
        # But we default to aware UTC.

        # Ensure last_reset is aware for comparison
        last_reset = self.last_reset
        if last_reset is None:
            self.last_reset = now
            return False

        if last_reset.tzinfo is None:
             last_reset = last_reset.replace(tzinfo=timezone.utc)

        if last_reset.date() < now.date():
            self.current_usage = 0
            self.last_reset = now
            self.is_active = True
            return True
        return False

async def get_db():
    async with SessionLocal() as session:
        yield session

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
