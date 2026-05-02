"""Database connection pool management."""

from __future__ import annotations

import logging
import os
from typing import Optional

from config.schema import StorageSettings
from storage.base import BaseStorage
from storage.postgres import PostgresStorage

logger = logging.getLogger(__name__)

_storage: Optional[BaseStorage] = None


def create_storage(settings: Optional[StorageSettings] = None) -> BaseStorage:
    """Factory: create storage backend from settings."""
    if settings is None:
        settings = StorageSettings(
            connection_string=os.getenv("DATABASE_URL", ""),
        )

    connection_string = settings.connection_string
    if not connection_string:
        connection_string = os.getenv("DATABASE_URL", "")

    return PostgresStorage(
        connection_string=connection_string,
        pool_min=settings.pool_min,
        pool_max=settings.pool_max,
        similarity_threshold=settings.similarity_threshold,
    )


async def get_storage(settings: Optional[StorageSettings] = None) -> BaseStorage:
    """Get or create and initialize the global storage instance."""
    global _storage
    if _storage is None:
        _storage = create_storage(settings)
        await _storage.initialize()
    return _storage


async def close_storage() -> None:
    """Close the global storage instance."""
    global _storage
    if _storage is not None:
        await _storage.close()
        _storage = None
