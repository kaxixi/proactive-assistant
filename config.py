import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"), override=True)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
DROPBOX_ACCESS_TOKEN = os.environ.get("DROPBOX_ACCESS_TOKEN", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514")
DIGEST_HOUR = int(os.environ.get("DIGEST_HOUR", "5"))
DIGEST_MINUTE = int(os.environ.get("DIGEST_MINUTE", "30"))
ENABLE_EMAIL = os.environ.get("ENABLE_EMAIL", "true").lower() in ("true", "1", "yes")


def get_anthropic_client():
    """Shared Anthropic client with bumped retries for transient 429/5xx (e.g. 529 overload).
    SDK default is 2 retries; 6 covers a ~30-60s overload window with jittered backoff."""
    import anthropic
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, max_retries=6)
