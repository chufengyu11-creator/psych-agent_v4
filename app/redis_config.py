"""Safe parsing helpers for provider-independent Redis URLs."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import unquote, urlsplit


@dataclass(frozen=True)
class RedisEndpoint:
    """Non-secret Redis endpoint metadata safe for diagnostics."""

    scheme: str
    host: str
    port: int
    database: int
    tls: bool
    has_authentication: bool


def parse_redis_url(redis_url: str) -> RedisEndpoint:
    """Validate a Redis URL and return only non-secret connection metadata."""

    value = redis_url.strip()
    if not value:
        raise ValueError("REDIS_URL is empty")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("REDIS_URL has an invalid port") from exc
    if parsed.scheme not in {"redis", "rediss"}:
        raise ValueError("REDIS_URL scheme must be redis or rediss")
    if not parsed.hostname:
        raise ValueError("REDIS_URL must include a host")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("REDIS_URL port must be between 1 and 65535")
    if parsed.fragment:
        raise ValueError("REDIS_URL must not include a fragment")

    database_path = parsed.path.lstrip("/")
    if "/" in database_path or (database_path and not database_path.isdigit()):
        raise ValueError("REDIS_URL database index must be a non-negative integer")
    database = int(database_path) if database_path else 0
    return RedisEndpoint(
        scheme=parsed.scheme,
        host=parsed.hostname,
        port=port if port is not None else 6379,
        database=database,
        tls=parsed.scheme == "rediss",
        has_authentication=parsed.username is not None or parsed.password is not None,
    )


def has_placeholder_redis_password(redis_url: str) -> bool:
    """Return whether a production Redis URL contains an obvious placeholder."""

    password = urlsplit(redis_url.strip()).password
    if password is None:
        return False
    return unquote(password).strip().casefold() in {
        "replace-me",
        "changeme",
        "password",
    }


__all__ = ["RedisEndpoint", "has_placeholder_redis_password", "parse_redis_url"]
