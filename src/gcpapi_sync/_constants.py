"""Defaults and limits of the Garmin Connect Personal API."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Final

DEFAULT_BASE_URL: Final = "https://personal.connectapi.garmin.com"
KEY_EXCHANGE_PATH: Final = "/personalapitoken-service/personal-api/personal-api-key/token"

# Same names and locations as Garmin's own client, so both can share one key and token.
API_KEY_ENV_VAR: Final = "GARMIN_PERSONAL_API_KEY"
BASE_URL_ENV_VAR: Final = "GARMIN_PERSONAL_API_BASE_URL"
DEFAULT_API_KEY_FILE: Final = Path("~/.config/garmin/connect-personal-api-key")
DEFAULT_TOKEN_CACHE_FILE: Final = Path("~/.config/garmin/connect-personal-api-bearer-token.json")

DEFAULT_TIMEOUT: Final = 30.0
# Refresh tokens slightly before they expire so in-flight requests never race expiry.
TOKEN_REFRESH_MARGIN: Final = timedelta(minutes=5)
# Garmin documents a 24 hour lifetime; used only when a token carries no ``exp`` claim.
FALLBACK_TOKEN_LIFETIME: Final = timedelta(hours=24)

# Maximum inclusive number of days per request. Garmin answers larger ranges with
# HTTP 400 (or HTTP 500 for daily summaries, whose documented one-year limit fails
# in practice beyond roughly 90 days). Longer ranges are split into windows.
MAX_DAYS_STEPS: Final = 28
MAX_DAYS_STRESS: Final = 28
MAX_DAYS_HYDRATION: Final = 31
MAX_DAYS_BODY_BATTERY: Final = 31
MAX_DAYS_SLEEP: Final = 31
MAX_DAYS_DAILY_SUMMARIES: Final = 31
MAX_DAYS_HRV: Final = 365

# The activity search fails with HTTP 500 for pages larger than about 45 activities,
# including the server's own default of 99. The client always sends an explicit limit.
DEFAULT_ACTIVITY_PAGE_SIZE: Final = 20
MAX_ACTIVITY_PAGE_SIZE: Final = 45

# Refuse to decompress implausibly large activity archives.
MAX_ACTIVITY_FILE_SIZE: Final = 512 * 1024 * 1024
