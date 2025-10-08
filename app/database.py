"""
Database configuration and helpers for the scavenger hunt app.

This module defines the SQLAlchemy engine, session maker and a convenience
dependency for FastAPI routes to obtain a database session.  It uses a
SQLite database located in the project directory (`db.sqlite3`) for
simplicity.  In production you may want to switch to PostgreSQL or
another relational database.
"""

from __future__ import annotations

import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session


# Determine the database URL.  Use a file-based SQLite database by default.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'db.sqlite3')}")


# Create the SQLAlchemy engine.  The `check_same_thread` flag is required for
# SQLite when used with multiple threads (as FastAPI is asynchronous by
# default).  For other databases (e.g. PostgreSQL) you can remove
# `connect_args`.
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    echo=False,
)


# Create a configured "SessionLocal" class.  Each FastAPI request will
# instantiate a new SessionLocal instance via the `get_db` dependency.  The
# session is disposed at the end of the request.
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Session:
    """Provide a transactional scope for database access.

    This function is used as a FastAPI dependency.  It yields a new
    `Session` object and ensures that the session is closed after the
    request finishes.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()