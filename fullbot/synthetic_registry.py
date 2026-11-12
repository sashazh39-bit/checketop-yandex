# -*- coding: utf-8 -*-
"""Register PDFs emitted by the generator bot in the checker database.

Only SHA-256, source label and UTC timestamp are stored.  No PDF bytes,
receipt fields, Telegram user IDs or PII leave the generator process.
Registration is best-effort and never blocks document delivery.
"""
from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("synthetic-registry")

DEFAULT_DB = "/opt/checker-bot/data/checker.sqlite3"


def register_generated_pdf(
    pdf_bytes: bytes,
    *,
    source: str = "mts-bot",
    db_path: str | os.PathLike[str] | None = None,
) -> str | None:
    """Return SHA-256 after registration, or ``None`` when disabled/invalid."""
    if not isinstance(pdf_bytes, (bytes, bytearray)) or not pdf_bytes.startswith(b"%PDF-"):
        return None
    target = str(db_path or os.environ.get("SYNTHETIC_REGISTRY_DB", DEFAULT_DB))
    path = Path(target)
    if not path.parent.exists():
        # Local development without the checker installed: intentional no-op.
        return None
    digest = hashlib.sha256(bytes(pdf_bytes)).hexdigest()
    connection = None
    try:
        connection = sqlite3.connect(path, timeout=10)
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS generated_documents (
                file_sha256 TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO generated_documents
                (file_sha256, source, created_at)
            VALUES (?, ?, ?)
            """,
            (
                digest,
                source,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        connection.commit()
        logger.info("registered generated PDF sha256=%s source=%s", digest[:12], source)
        return digest
    except Exception:
        logger.exception("failed to register generated PDF")
        return None
    finally:
        if connection is not None:
            connection.close()
