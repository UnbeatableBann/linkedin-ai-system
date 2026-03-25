"""
app/api/oauth_callback.py
──────────────────────────
Receives the Zernio OAuth callback after LinkedIn authorisation.

When using headless mode, Zernio redirects to this URL after the user
authorises LinkedIn. The URL contains the connect_token as a query param.

This endpoint just acknowledges the callback — the actual polling happens
in the onboarding conversation handler.
"""

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from app.core.logging import get_logger

router = APIRouter()
logger = get_logger(__name__)


@router.get("/oauth/callback")
async def oauth_callback(
    connect_token: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> HTMLResponse:
    """
    Zernio OAuth callback endpoint.

    After the user authorises LinkedIn in their browser, Zernio redirects here.
    The connect_token is in the query string.

    We show a simple HTML page telling the user to go back to the bot.
    The bot conversation handles the rest.
    """
    if error:
        logger.warning("oauth.callback.error", error=error)
        html = _page(
            title="Connection Failed",
            emoji="❌",
            message="Something went wrong connecting your LinkedIn account.",
            detail=f"Error: {error}",
            instruction="Go back to the bot and try /reconnect to start again.",
        )
        return HTMLResponse(content=html, status_code=400)

    if connect_token:
        logger.info("oauth.callback.success")
        html = _page(
            title="LinkedIn Connected!",
            emoji="✅",
            message="Your LinkedIn account has been successfully authorised.",
            detail="",
            instruction="Go back to Telegram or WhatsApp and send any message to continue setup.",
        )
        return HTMLResponse(content=html, status_code=200)

    html = _page(
        title="Callback Received",
        emoji="🔄",
        message="We received your callback.",
        detail="",
        instruction="Go back to the bot and send any message to continue.",
    )
    return HTMLResponse(content=html, status_code=200)


def _page(title: str, emoji: str, message: str, detail: str, instruction: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; display: flex;
          align-items: center; justify-content: center; min-height: 100vh; margin: 0;
          background: #f8fafc; color: #1e293b; }}
  .card {{ background: white; border-radius: 16px; padding: 48px; max-width: 440px;
           text-align: center; box-shadow: 0 4px 24px rgba(0,0,0,0.08); }}
  .emoji {{ font-size: 64px; margin-bottom: 16px; }}
  h1 {{ font-size: 24px; font-weight: 600; margin: 0 0 12px; }}
  p {{ font-size: 16px; color: #64748b; margin: 8px 0; line-height: 1.5; }}
  .instruction {{ margin-top: 24px; background: #f1f5f9; border-radius: 8px;
                  padding: 16px; font-size: 14px; color: #475569; }}
</style>
</head>
<body>
  <div class="card">
    <div class="emoji">{emoji}</div>
    <h1>{title}</h1>
    <p>{message}</p>
    {'<p style="color:#94a3b8;font-size:14px">' + detail + '</p>' if detail else ''}
    <div class="instruction">👆 {instruction}</div>
  </div>
</body>
</html>"""
