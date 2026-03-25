"""
app/zernio/health.py
─────────────────────
LinkedIn account token health check before scheduling.

Called by publish tasks before attempting to push to Zernio,
so we catch expired LinkedIn tokens early and notify the user
instead of discovering it at publish time.
"""

from app.core.logging import get_logger
from app.db.models import UserRow
from app.zernio.client import ZernioError, make_zernio_client

logger = get_logger(__name__)


async def assert_account_healthy(user: UserRow) -> None:
    """
    Check the user's Zernio-linked LinkedIn account is still valid.

    Raises ZernioError with a user-friendly message if:
      - No Zernio API key stored
      - No LinkedIn account connected
      - LinkedIn token is expired or revoked
    """
    if not user.zernio_api_key_enc:
        raise ZernioError(
            "No Zernio API key on file. Please run /start to reconnect.",
            status_code=None,
        )

    if not user.zernio_account_id:
        raise ZernioError(
            "No LinkedIn account connected. Please run /reconnect to link LinkedIn.",
            status_code=None,
        )

    client = make_zernio_client(user)
    healthy = await client.check_account_health(user.zernio_account_id)

    if not healthy:
        raise ZernioError(
            "Your LinkedIn connection has expired or been revoked.\n\n"
            "Run /reconnect to re-authorise LinkedIn, then reschedule your post.",
            status_code=401,
        )

    logger.debug("zernio.health.ok", user_id=str(user.id))
