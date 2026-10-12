import os
import pytest
import pytest_asyncio
import aiosqlite
from pathlib import Path

from src.config import config

@pytest.fixture(scope="function")
def mock_db_path(tmp_path):
    """Provides an isolated database path in a temporary directory."""
    return str(tmp_path / "test.db")

@pytest_asyncio.fixture(scope="function", autouse=True)
async def init_test_db(mock_db_path):
    """
    Initializes the database schema and FTS5 indexes in the temporary DB.
    Uses monkeypatching on the `config` singleton to achieve zero-refactor isolation.
    """
    original_db = config.database_path
    original_user = config.telegram_allowed_user_id
    
    # Monkeypatch the frozen dataclass singleton in-place for this test
    object.__setattr__(config, "database_path", mock_db_path)
    object.__setattr__(config, "telegram_allowed_user_id", 123456)
    
    from src.storage.database import init_db
    from src.storage.search import init_search_index, init_chat_history_search_index
    
    # 1. Initialize schema and indexes
    await init_db()
    await init_search_index()
    await init_chat_history_search_index()
    
    # 2. Seed dummy data (10 chat_history, 5 lab_notes)
    from src.storage.database import generate_id
    async with aiosqlite.connect(mock_db_path) as db:
        for i in range(1, 11):
            await db.execute(
                "INSERT INTO chat_history (id, role, content) VALUES (?, ?, ?)", 
                (generate_id(f"msg_{i}"), "user" if i % 2 != 0 else "assistant", f"Pesan dummy ke-{i}")
            )
        for i in range(1, 6):
            await db.execute(
                "INSERT INTO lab_notes (topic, tags, content) VALUES (?, ?, ?)", 
                (f"Topik {i}", "test", f"Isi lab note {i}")
            )
        await db.commit()
    
    yield mock_db_path
    
    # Restore original config values after the test
    object.__setattr__(config, "database_path", original_db)
    object.__setattr__(config, "telegram_allowed_user_id", original_user)
