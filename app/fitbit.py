"""Fitbit API client with OAuth2 and token persistence."""
import base64
import json
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from .config import settings

# Token storage
TOKEN_FILE = Path("/app/data/tokens.json")

FITBIT_AUTH_URL = "https://www.fitbit.com/oauth2/authorize"
FITBIT_TOKEN_URL = "https://api.fitbit.com/oauth2/token"
FITBIT_API_BASE = "https://api.fitbit.com"


class FitbitClient:
    """Fitbit API client with automatic token refresh."""

    def __init__(self):
        self._tokens: dict | None = None

    @property
    def tokens(self) -> dict | None:
        """Load tokens from file if not cached."""
        if self._tokens is None:
            self._tokens = self._load_tokens()
        return self._tokens

    def _load_tokens(self) -> dict | None:
        """Load tokens from persistent storage."""
        if TOKEN_FILE.exists():
            try:
                return json.loads(TOKEN_FILE.read_text())
            except (json.JSONDecodeError, IOError):
                return None
        return None

    def _save_tokens(self, tokens: dict):
        """Save tokens to persistent storage."""
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(json.dumps(tokens, indent=2))
        self._tokens = tokens

    def get_auth_url(self) -> str:
        """Generate Fitbit OAuth2 authorization URL."""
        params = {
            "client_id": settings.fitbit_client_id,
            "response_type": "code",
            "scope": "weight profile",
            "redirect_uri": settings.fitbit_redirect_uri,
        }
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{FITBIT_AUTH_URL}?{query}"

    def _get_basic_auth(self) -> str:
        """Generate Basic Auth header for token requests."""
        credentials = f"{settings.fitbit_client_id}:{settings.fitbit_client_secret}"
        encoded = base64.b64encode(credentials.encode()).decode()
        return f"Basic {encoded}"

    async def exchange_code(self, code: str) -> dict:
        """Exchange authorization code for tokens."""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                FITBIT_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": settings.fitbit_redirect_uri,
                },
                headers={
                    "Authorization": self._get_basic_auth(),
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )

        if response.status_code != 200:
            raise Exception(f"Token exchange failed: {response.text}")

        tokens = response.json()
        self._save_tokens(tokens)
        return tokens

    async def refresh_tokens(self) -> dict | None:
        """Refresh access token using refresh token."""
        if not self.tokens or not self.tokens.get("refresh_token"):
            return None

        async with httpx.AsyncClient() as client:
            response = await client.post(
                FITBIT_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self.tokens["refresh_token"],
                },
                headers={
                    "Authorization": self._get_basic_auth(),
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )

        if response.status_code != 200:
            return None

        tokens = response.json()
        self._save_tokens(tokens)
        return tokens

    async def _request(self, method: str, endpoint: str, **kwargs) -> dict:
        """Make authenticated API request with auto-refresh."""
        if not self.tokens:
            raise Exception("Not authenticated")

        headers = {
            "Authorization": f"Bearer {self.tokens['access_token']}",
            **kwargs.pop("headers", {}),
        }

        async with httpx.AsyncClient() as client:
            response = await client.request(
                method,
                f"{FITBIT_API_BASE}{endpoint}",
                headers=headers,
                **kwargs,
            )

        # Token expired - try refresh
        if response.status_code == 401:
            new_tokens = await self.refresh_tokens()
            if not new_tokens:
                raise Exception("Token refresh failed")

            headers["Authorization"] = f"Bearer {new_tokens['access_token']}"
            async with httpx.AsyncClient() as client:
                response = await client.request(
                    method,
                    f"{FITBIT_API_BASE}{endpoint}",
                    headers=headers,
                    **kwargs,
                )

        if response.status_code != 200:
            raise Exception(f"API request failed: {response.text}")

        return response.json()

    async def get_weight_range(
        self, start_date: datetime, end_date: datetime
    ) -> list[dict]:
        """Get weight entries for date range."""
        start = start_date.strftime("%Y-%m-%d")
        end = end_date.strftime("%Y-%m-%d")
        data = await self._request(
            "GET", f"/1/user/-/body/log/weight/date/{start}/{end}.json"
        )
        return data.get("weight", [])

    async def get_weight_goal(self) -> dict | None:
        """Get weight goal."""
        try:
            data = await self._request("GET", "/1/user/-/body/log/weight/goal.json")
            return data.get("goal")
        except Exception:
            return None

    async def get_profile(self) -> dict:
        """Get user profile."""
        data = await self._request("GET", "/1/user/-/profile.json")
        return data.get("user", {})

    def is_authenticated(self) -> bool:
        """Check if we have valid tokens."""
        return self.tokens is not None and "access_token" in self.tokens

    def clear_tokens(self):
        """Remove stored tokens."""
        if TOKEN_FILE.exists():
            TOKEN_FILE.unlink()
        self._tokens = None


# Singleton instance
fitbit_client = FitbitClient()
