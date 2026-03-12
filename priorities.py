"""Fetch Erez's current priorities from published Simplenote."""

import logging
import os
import urllib.request
import re

logger = logging.getLogger(__name__)

PRIORITIES_URL = os.environ.get("PRIORITIES_URL", "")


def fetch_priorities() -> str:
    """Fetch the current priorities list from Simplenote."""
    try:
        req = urllib.request.Request(PRIORITIES_URL, headers={"User-Agent": "Claudette/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8")

        # Extract text content from the HTML — Simplenote published pages
        # have the content in a <div class="note-content"> or similar
        # Strip HTML tags for a clean text version
        text = re.sub(r"<[^>]+>", "\n", html)
        # Clean up whitespace
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        # Skip header/footer boilerplate — find the actual note content
        content = "\n".join(lines)

        logger.info(f"Fetched priorities ({len(content)} chars)")
        return content
    except Exception as e:
        logger.warning(f"Could not fetch priorities: {e}")
        return ""
