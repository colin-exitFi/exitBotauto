"""
Velox - Configuration & Settings
All settings loaded from .env with sensible defaults.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root
_project_root = Path(__file__).resolve().parent.parent
load_dotenv(_project_root / ".env", override=True)


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _env_float(key: str, default: float = 0.0) -> float:
    return float(os.getenv(key, str(default)))


def _env_int(key: str, default: int = 0) -> int:
    return int(os.getenv(key, str(default)))


# ── API Keys ──────────────────────────────────────────────────────
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
OPENAI_API_KEY = _env("OPENAI_API_KEY")
PERPLEXITY_API_KEY = _env("PERPLEXITY_API_KEY")

# Twitter / X
X_BEARER_TOKEN = _env("X_BEARER_TOKEN")
X_CONSUMER_KEY = _env("X_CONSUMER_KEY")
X_CONSUMER_SECRET = _env("X_CONSUMER_SECRET")

# Alpaca
ALPACA_API_KEY = _env("ALPACA_API_KEY")
ALPACA_SECRET_KEY = _env("ALPACA_SECRET_KEY")
ALPACA_PAPER = _env("ALPACA_PAPER", "true").lower() in ("true", "1", "yes")

# Polygon
POLYGON_API_KEY = _env("POLYGON_API_KEY")

# ── Trading Parameters ────────────────────────────────────────────
TOTAL_CAPITAL = _env_float("TOTAL_CAPITAL", 25000)
DEPLOYED_CAPITAL = _env_float("DEPLOYED_CAPITAL", 25000)
POSITION_SIZE_PCT = _env_float("POSITION_SIZE_PCT", 2.0)
MAX_CONCURRENT_POSITIONS = _env_int("MAX_CONCURRENT_POSITIONS", 5)
MAX_DAILY_LOSS_PCT = _env_float("MAX_DAILY_LOSS_PCT", 3.0)
MAX_POSITION_LOSS_PCT = _env_float("MAX_POSITION_LOSS_PCT", 1.0)
TAKE_PROFIT_1_PCT = _env_float("TAKE_PROFIT_1_PCT", 1.5)
TAKE_PROFIT_2_PCT = _env_float("TAKE_PROFIT_2_PCT", 2.5)
TRAILING_STOP_PCT = _env_float("TRAILING_STOP_PCT", 0.5)
STOP_LOSS_PCT = _env_float("STOP_LOSS_PCT", 1.0)
MAX_HOLD_HOURS = _env_float("MAX_HOLD_HOURS", 4)
EOD_EXIT_TIME = _env("EOD_EXIT_TIME", "15:30")  # ET

# ── Scanner Parameters ────────────────────────────────────────────
MIN_PRICE = _env_float("MIN_PRICE", 5.0)
MAX_PRICE = _env_float("MAX_PRICE", 500.0)
MIN_VOLUME = _env_int("MIN_VOLUME", 1_000_000)
VOLUME_SPIKE_MULTIPLIER = _env_float("VOLUME_SPIKE_MULTIPLIER", 2.0)
MIN_MOMENTUM_PCT = _env_float("MIN_MOMENTUM_PCT", 2.0)
SCAN_INTERVAL_SECONDS = _env_int("SCAN_INTERVAL_SECONDS", 300)
SCAN_INTERVAL_FAST_SECONDS = _env_int("SCAN_INTERVAL_FAST_SECONDS", 60)
SCAN_INTERVAL_SLOW_SECONDS = _env_int("SCAN_INTERVAL_SLOW_SECONDS", 300)
SCAN_REGIME_HYSTERESIS_WINDOW = _env_int("SCAN_REGIME_HYSTERESIS_WINDOW", 3)
SCAN_REGIME_MIN_CONFIRMATIONS = _env_int("SCAN_REGIME_MIN_CONFIRMATIONS", 2)

# ── Sentiment Parameters ──────────────────────────────────────────
MIN_ENTRY_SENTIMENT = _env_float("MIN_ENTRY_SENTIMENT", 0.3)
EXIT_SENTIMENT_WARNING = _env_float("EXIT_SENTIMENT_WARNING", 0.3)
EXIT_SENTIMENT_CRITICAL = _env_float("EXIT_SENTIMENT_CRITICAL", 0.0)

# ── Dashboard ─────────────────────────────────────────────────────
DASHBOARD_HOST = _env("DASHBOARD_HOST", "0.0.0.0")
DASHBOARD_PORT = _env_int("DASHBOARD_PORT", 8421)
DASHBOARD_TOKEN = _env("DASHBOARD_TOKEN", "")

# ── Logging ───────────────────────────────────────────────────────
LOG_LEVEL = _env("LOG_LEVEL", "INFO")

# ── Risk Management (Advanced) ────────────────────────────────────
ATR_PERIOD = _env_int("ATR_PERIOD", 14)
ATR_STOP_MULTIPLIER = _env_float("ATR_STOP_MULTIPLIER", 1.5)
ATR_TRAIL_MULTIPLIER = _env_float("ATR_TRAIL_MULTIPLIER", 2.0)
WEEKLY_MAX_LOSS_PCT = _env_float("WEEKLY_MAX_LOSS_PCT", 5.0)
EXTENDED_HOURS_SIZE_MULT = _env_float("EXTENDED_HOURS_SIZE_MULT", 0.5)
MAX_PRICE_CHASE_PCT = _env_float("MAX_PRICE_CHASE_PCT", 0.5)
CASH_ACCOUNT = _env("CASH_ACCOUNT", "true").lower() in ("true", "1", "yes")
MAX_SECTOR_PCT = _env_float("MAX_SECTOR_PCT", 40.0)

# ── Consensus Engine ──────────────────────────────────────────────
CONSENSUS_ENABLED = _env("CONSENSUS_ENABLED", "true").lower() in ("true", "1", "yes")
CONSENSUS_MIN_CONFIDENCE = _env_int("CONSENSUS_MIN_CONFIDENCE", 70)
CONSENSUS_CACHE_SECONDS = _env_int("CONSENSUS_CACHE_SECONDS", 300)
CONSENSUS_MAX_CALLS_PER_HOUR = _env_int("CONSENSUS_MAX_CALLS_PER_HOUR", 60)
OPENAI_MODEL = _env("OPENAI_MODEL", "gpt-5.4")
CLAUDE_MODEL = _env("CLAUDE_MODEL", "claude-sonnet-4-5-20250929")
XAI_API_KEY = _env("XAI_API_KEY", "")
XAI_MODEL = _env("XAI_MODEL", "grok-4-fast-reasoning")
PERPLEXITY_MODEL = _env("PERPLEXITY_MODEL", "sonar-pro")

# ── Notifications ─────────────────────────────────────────────────
SLACK_WEBHOOK_URL = _env("SLACK_WEBHOOK_URL", "")
