"""Pure tests for cross-dialect IntegrityError classification."""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy.exc import IntegrityError

from storage.error_classification import (
    IntegrityErrorKind,
    classify_integrity_error,
)


class DriverError(Exception):
    """Artificial driver error exposing optional database metadata."""

    def __init__(
        self,
        *,
        sqlstate: str | None = None,
        sqlite_errorcode: int | None = None,
        sqlite_errorname: str | None = None,
    ) -> None:
        super().__init__("artificial driver constraint failure")
        self.sqlstate = sqlstate
        self.sqlite_errorcode = sqlite_errorcode
        self.sqlite_errorname = sqlite_errorname


def _integrity_error(original: BaseException) -> IntegrityError:
    """Wrap a safe artificial driver exception as SQLAlchemy would."""

    return IntegrityError("artificial statement", {}, original)


@pytest.mark.parametrize(
    ("sqlstate", "expected"),
    [
        ("23505", IntegrityErrorKind.UNIQUE),
        ("23503", IntegrityErrorKind.FOREIGN_KEY),
        ("23502", IntegrityErrorKind.NOT_NULL),
        ("23514", IntegrityErrorKind.CHECK),
    ],
)
def test_postgres_sqlstate_classification(
    sqlstate: str,
    expected: IntegrityErrorKind,
) -> None:
    """PostgreSQL constraint classes should use structured SQLSTATE values."""

    error = _integrity_error(DriverError(sqlstate=sqlstate))

    assert classify_integrity_error(error) is expected


@pytest.mark.parametrize(
    ("sqlite_errorcode", "expected"),
    [
        (sqlite3.SQLITE_CONSTRAINT_UNIQUE, IntegrityErrorKind.UNIQUE),
        (sqlite3.SQLITE_CONSTRAINT_FOREIGNKEY, IntegrityErrorKind.FOREIGN_KEY),
        (sqlite3.SQLITE_CONSTRAINT_NOTNULL, IntegrityErrorKind.NOT_NULL),
        (sqlite3.SQLITE_CONSTRAINT_CHECK, IntegrityErrorKind.CHECK),
    ],
)
def test_sqlite_extended_error_code_classification(
    sqlite_errorcode: int,
    expected: IntegrityErrorKind,
) -> None:
    """SQLite extended codes should classify without parsing business values."""

    error = _integrity_error(DriverError(sqlite_errorcode=sqlite_errorcode))

    assert classify_integrity_error(error) is expected


def test_sqlite_error_name_and_legacy_message_fallbacks() -> None:
    """Controlled SQLite fallbacks should support older driver shapes."""

    named = _integrity_error(
        DriverError(sqlite_errorname="SQLITE_CONSTRAINT_PRIMARYKEY")
    )
    legacy = _integrity_error(
        sqlite3.IntegrityError("FOREIGN KEY constraint failed")
    )

    assert classify_integrity_error(named) is IntegrityErrorKind.UNIQUE
    assert classify_integrity_error(legacy) is IntegrityErrorKind.FOREIGN_KEY


def test_unknown_integrity_error_is_never_treated_as_duplicate() -> None:
    """Unrecognized driver failures must remain unknown and be re-raised."""

    error = _integrity_error(DriverError(sqlstate="23999"))

    assert classify_integrity_error(error) is IntegrityErrorKind.UNKNOWN
