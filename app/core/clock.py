from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

SEOUL = ZoneInfo("Asia/Seoul")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def kst_today() -> date:
    return utc_now().astimezone(SEOUL).date()


def get_today() -> date:
    """Dependency overridden by tests; not exposed as an HTTP input."""
    return kst_today()
