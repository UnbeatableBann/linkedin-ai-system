"""
app/zernio/client.py
────────────────────
Async httpx wrapper for the Zernio API.

One ZernioClient is created per request using the user's own API key.
Never share a client between users — each user has their own Zernio account.

All methods raise ZernioError on non-2xx responses with a human-readable
message so the calling code can forward it to the user.
"""

from dataclasses import dataclass
from typing import Any

import httpx

from app.core.logging import get_logger

logger = get_logger(__name__)

ZERNIO_BASE_URL = "https://zernio.com/api/v1"
DEFAULT_TIMEOUT = 20.0


@dataclass
class ZernioProfile:
    id: str
    name: str


@dataclass
class ZernioAccount:
    id: str
    platform: str
    name: str


@dataclass
class ZernioOrg:
    id: str
    name: str
    type: str  # "personal" | "organization"


@dataclass
class ZernioPost:
    id: str
    status: str
    scheduled_for: str | None = None


class ZernioError(Exception):
    """Raised for Zernio API errors. Always has a user-friendly message."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class ZernioClient:
    """
    Per-user Zernio API client.

    Usage:
        client = ZernioClient(api_key=decrypt(user.zernio_api_key_enc))
        profile = await client.create_profile("My Brand")
    """

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    # ── Validation ──────────────────────────────────────────────────────────

    async def validate_key(self) -> list[ZernioAccount]:
        """
        Validate the API key and return connected accounts.
        Used during onboarding to verify the user's key works.
        Raises ZernioError if the key is invalid.
        """
        data = await self._get("/accounts")
        accounts = data.get("accounts", [])
        return [
            ZernioAccount(id=a["_id"], platform=a["platform"], name=a.get("name", ""))
            for a in accounts
        ]

    async def check_account_health(self, account_id: str) -> bool:
        """
        Check if a connected LinkedIn account's token is still valid.
        Returns True if healthy, False if token needs reauthorisation.
        """
        try:
            data = await self._get("/accounts/health")
            for account in data.get("accounts", []):
                if account.get("_id") == account_id:
                    return account.get("healthy", False)
            return False
        except ZernioError:
            return False

    # ── Profiles ────────────────────────────────────────────────────────────

    async def create_profile(self, name: str, description: str = "") -> ZernioProfile:
        """Create a Zernio profile for this user."""
        data = await self._post("/profiles", {"name": name, "description": description})
        profile = data["profile"]
        return ZernioProfile(id=profile["_id"], name=profile["name"])

    # ── OAuth / Connect ─────────────────────────────────────────────────────

    async def get_linkedin_oauth_url(
        self, profile_id: str, redirect_url: str
    ) -> str:
        """
        Start the LinkedIn OAuth flow in headless mode.
        Returns the authUrl to send to the user.
        """
        data = await self._get(
            f"/connect/linkedin",
            params={
                "profileId": profile_id,
                "headless": "true",
                "redirect_url": redirect_url,
            },
        )
        auth_url = data.get("authUrl")
        if not auth_url:
            raise ZernioError("Zernio did not return an auth URL. Please try again.")
        return auth_url

    async def get_linkedin_connect_status(self) -> dict[str, Any]:
        """
        Poll for the connect_token after the user completes LinkedIn OAuth.
        Returns the raw response — caller checks for connect_token presence.
        """
        return await self._get("/connect/linkedin/status")

    async def list_linkedin_orgs(self, connect_token: str) -> list[ZernioOrg]:
        """
        List LinkedIn organisations (personal + company pages) the user can post from.
        Requires the connect_token from the OAuth callback.
        """
        headers = {**self._headers, "X-Connect-Token": connect_token}
        data = await self._get("/connect/linkedin/organizations", extra_headers=headers)
        orgs = data.get("organizations", [])
        return [
            ZernioOrg(
                id=o["_id"],
                name=o.get("name", "Personal Account"),
                type=o.get("type", "personal"),
            )
            for o in orgs
        ]

    async def select_linkedin_org(self, connect_token: str, org_id: str) -> ZernioAccount:
        """
        Select which LinkedIn org/personal to connect.
        Returns the created ZernioAccount with its ID.
        """
        headers = {**self._headers, "X-Connect-Token": connect_token}
        data = await self._post_with_headers(
            "/connect/linkedin/select-organization",
            {"organizationId": org_id},
            headers=headers,
        )
        account = data.get("account", {})
        return ZernioAccount(
            id=account["_id"],
            platform="linkedin",
            name=account.get("name", "LinkedIn"),
        )

    # ── Posts ───────────────────────────────────────────────────────────────

    async def schedule_post(
        self,
        content: str,
        account_id: str,
        scheduled_for: str,  # ISO datetime string
        timezone: str,
    ) -> ZernioPost:
        """
        Schedule a LinkedIn post via Zernio.
        scheduled_for: ISO 8601 datetime, e.g. "2024-04-07T09:00:00"
        timezone: IANA timezone, e.g. "Asia/Kolkata"
        """
        data = await self._post(
            "/posts",
            {
                "content": content,
                "scheduledFor": scheduled_for,
                "timezone": timezone,
                "platforms": [{"platform": "linkedin", "accountId": account_id}],
            },
        )
        post = data["post"]
        return ZernioPost(
            id=post["_id"],
            status=post.get("status", "scheduled"),
            scheduled_for=post.get("scheduledFor"),
        )

    async def publish_now(self, content: str, account_id: str) -> ZernioPost:
        """Publish a post immediately (no scheduling)."""
        data = await self._post(
            "/posts",
            {
                "content": content,
                "publishNow": True,
                "platforms": [{"platform": "linkedin", "accountId": account_id}],
            },
        )
        post = data["post"]
        return ZernioPost(id=post["_id"], status=post.get("status", "published"))

    async def cancel_post(self, zernio_post_id: str) -> None:
        """Cancel a scheduled post before it publishes."""
        await self._delete(f"/posts/{zernio_post_id}")

    async def get_post_status(self, zernio_post_id: str) -> ZernioPost:
        """Get the current status of a post."""
        data = await self._get(f"/posts/{zernio_post_id}")
        post = data["post"]
        return ZernioPost(
            id=post["_id"],
            status=post.get("status", "unknown"),
            scheduled_for=post.get("scheduledFor"),
        )

    # ── Private HTTP helpers ────────────────────────────────────────────────

    async def _get(
        self,
        path: str,
        params: dict[str, str] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = {**self._headers, **(extra_headers or {})}
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            try:
                resp = await client.get(
                    f"{ZERNIO_BASE_URL}{path}",
                    headers=headers,
                    params=params or {},
                )
                return _handle_response(resp)
            except httpx.TimeoutException:
                raise ZernioError("Zernio API timed out. Please try again in a moment.")

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            try:
                resp = await client.post(
                    f"{ZERNIO_BASE_URL}{path}",
                    headers=self._headers,
                    json=body,
                )
                return _handle_response(resp)
            except httpx.TimeoutException:
                raise ZernioError("Zernio API timed out. Please try again.")

    async def _post_with_headers(
        self, path: str, body: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            try:
                resp = await client.post(
                    f"{ZERNIO_BASE_URL}{path}",
                    headers=headers,
                    json=body,
                )
                return _handle_response(resp)
            except httpx.TimeoutException:
                raise ZernioError("Zernio API timed out. Please try again.")

    async def _delete(self, path: str) -> None:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            try:
                resp = await client.delete(
                    f"{ZERNIO_BASE_URL}{path}",
                    headers=self._headers,
                )
                if resp.status_code not in (200, 204):
                    _handle_response(resp)
            except httpx.TimeoutException:
                raise ZernioError("Zernio API timed out.")


def _handle_response(resp: httpx.Response) -> dict[str, Any]:
    """Parse response and raise ZernioError for non-2xx status codes."""
    if resp.status_code == 401:
        raise ZernioError(
            "Your Zernio API key is invalid or expired. "
            "Please check your key at zernio.com → Settings → API Keys.",
            status_code=401,
        )
    if resp.status_code == 429:
        raise ZernioError(
            "Zernio rate limit reached. Please wait a minute and try again.",
            status_code=429,
        )
    if resp.status_code >= 500:
        raise ZernioError(
            "Zernio is experiencing issues right now. Your post is safe — I'll retry automatically.",
            status_code=resp.status_code,
        )
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("message", resp.text)
        except Exception:
            detail = resp.text
        clean_detail = _sanitize_error_detail(detail)
        raise ZernioError(f"Zernio error: {clean_detail}", status_code=resp.status_code)

    try:
        return resp.json()
    except Exception:
        return {}


def make_zernio_client(user: "UserRow") -> ZernioClient:
    """
    Convenience factory that decrypts the user's stored API key
    and returns a ready-to-use ZernioClient.
    """
    from app.core.encryption import decrypt
    from app.db.models import UserRow

    if not user.zernio_api_key_enc:
        raise ZernioError("No Zernio API key on file. Please run /start to set one up.")

    api_key = decrypt(user.zernio_api_key_enc)
    return ZernioClient(api_key=api_key)


def _sanitize_error_detail(detail: Any) -> str:
    """Normalize API error text for safe user-facing messages."""
    text = str(detail or "Request rejected by Zernio.")

    lower = text.lower()
    if "<!doctype html" in lower or "<html" in lower:
        return "Unexpected response from Zernio. Please try again in a moment."

    text = " ".join(text.split())
    if len(text) > 220:
        text = text[:220].rstrip() + "..."

    return text
