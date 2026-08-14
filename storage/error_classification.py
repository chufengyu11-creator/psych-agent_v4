"""Small cross-dialect classifier for SQLAlchemy integrity failures."""

from __future__ import annotations

import sqlite3
from enum import StrEnum

from sqlalchemy.exc import IntegrityError


class IntegrityErrorKind(StrEnum):
    """Constraint categories relevant to repository recovery decisions."""

    UNIQUE = "unique"
    FOREIGN_KEY = "foreign_key"
    NOT_NULL = "not_null"
    CHECK = "check"
    UNKNOWN = "unknown"


_POSTGRES_SQLSTATE_KINDS = {
    "23505": IntegrityErrorKind.UNIQUE,
    "23503": IntegrityErrorKind.FOREIGN_KEY,
    "23502": IntegrityErrorKind.NOT_NULL,
    "23514": IntegrityErrorKind.CHECK,
}
_SQLITE_CODE_KINDS = {
    sqlite3.SQLITE_CONSTRAINT_UNIQUE: IntegrityErrorKind.UNIQUE,
    sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY: IntegrityErrorKind.UNIQUE,
    sqlite3.SQLITE_CONSTRAINT_FOREIGNKEY: IntegrityErrorKind.FOREIGN_KEY,
    sqlite3.SQLITE_CONSTRAINT_NOTNULL: IntegrityErrorKind.NOT_NULL,
    sqlite3.SQLITE_CONSTRAINT_CHECK: IntegrityErrorKind.CHECK,
}
_SQLITE_NAME_KINDS = {
    "SQLITE_CONSTRAINT_UNIQUE": IntegrityErrorKind.UNIQUE,
    "SQLITE_CONSTRAINT_PRIMARYKEY": IntegrityErrorKind.UNIQUE,
    "SQLITE_CONSTRAINT_FOREIGNKEY": IntegrityErrorKind.FOREIGN_KEY,
    "SQLITE_CONSTRAINT_NOTNULL": IntegrityErrorKind.NOT_NULL,
    "SQLITE_CONSTRAINT_CHECK": IntegrityErrorKind.CHECK,
}


def classify_integrity_error(error: IntegrityError) -> IntegrityErrorKind:
    """Classify one failure without exposing its statement or bound parameters."""

    original = error.orig
    if original is None:
        return IntegrityErrorKind.UNKNOWN

    sqlstate = _first_error_attribute(original, "sqlstate")
    if not isinstance(sqlstate, str):
        sqlstate = _first_error_attribute(original, "pgcode")
    if isinstance(sqlstate, str):
        postgres_kind = _POSTGRES_SQLSTATE_KINDS.get(sqlstate)
        if postgres_kind is not None:
            return postgres_kind

    sqlite_errorcode = _first_error_attribute(original, "sqlite_errorcode")
    if isinstance(sqlite_errorcode, int):
        sqlite_kind = _SQLITE_CODE_KINDS.get(sqlite_errorcode)
        if sqlite_kind is not None:
            return sqlite_kind

    sqlite_errorname = _first_error_attribute(original, "sqlite_errorname")
    if isinstance(sqlite_errorname, str):
        sqlite_kind = _SQLITE_NAME_KINDS.get(sqlite_errorname)
        if sqlite_kind is not None:
            return sqlite_kind

    if isinstance(original, sqlite3.IntegrityError):
        return _classify_legacy_sqlite_message(str(original))
    return IntegrityErrorKind.UNKNOWN


def _first_error_attribute(error: BaseException, name: str) -> object | None:
    """Read a driver attribute from a short wrapped-exception chain."""

    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and len(seen) < 6:
        identity = id(current)
        if identity in seen:
            break
        seen.add(identity)
        value: object = getattr(current, name, None)
        if value is not None:
            return value
        current = current.__cause__ or current.__context__
    return None


def _classify_legacy_sqlite_message(message: str) -> IntegrityErrorKind:
    """Use SQLite's stable constraint prefixes only when codes are unavailable."""

    normalized = message.upper()
    if "UNIQUE CONSTRAINT FAILED" in normalized:
        return IntegrityErrorKind.UNIQUE
    if "FOREIGN KEY CONSTRAINT FAILED" in normalized:
        return IntegrityErrorKind.FOREIGN_KEY
    if "NOT NULL CONSTRAINT FAILED" in normalized:
        return IntegrityErrorKind.NOT_NULL
    if "CHECK CONSTRAINT FAILED" in normalized:
        return IntegrityErrorKind.CHECK
    return IntegrityErrorKind.UNKNOWN


__all__ = ["IntegrityErrorKind", "classify_integrity_error"]
