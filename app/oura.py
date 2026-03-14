"""Oura Ring API client with OAuth2 and token persistence."""
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from .config import settings

logger = logging.getLogger(__name__)

TOKEN_FILE = Path("/app/data/oura_tokens.json")

OURA_AUTH_URL = "https://cloud.ouraring.com/oauth/authorize"
OURA_TOKEN_URL = "https://api.ouraring.com/oauth/token"
OURA_API_BASE = "https://api.ouraring.com/v2/usercollection"


class OuraClient:
    """Oura Ring API client with automatic token refresh."""

    def __init__(self):
        self._tokens: dict | None = None

    @property
    def tokens(self) -> dict | None:
        if self._tokens is None:
            self._tokens = self._load_tokens()
        return self._tokens

    def _load_tokens(self) -> dict | None:
        if TOKEN_FILE.exists():
            try:
                return json.loads(TOKEN_FILE.read_text())
            except (json.JSONDecodeError, IOError):
                return None
        return None

    def _save_tokens(self, tokens: dict):
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(json.dumps(tokens, indent=2))
        self._tokens = tokens

    def get_auth_url(self) -> str:
        params = {
            "client_id": settings.oura_client_id,
            "response_type": "code",
            "scope": "daily heartrate session personal spo2 workout stress",
            "redirect_uri": settings.oura_redirect_uri,
        }
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{OURA_AUTH_URL}?{query}"

    async def exchange_code(self, code: str) -> dict:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                OURA_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": settings.oura_client_id,
                    "client_secret": settings.oura_client_secret,
                    "redirect_uri": settings.oura_redirect_uri,
                },
            )

        if response.status_code != 200:
            raise Exception(f"Oura token exchange failed: {response.text}")

        tokens = response.json()
        self._save_tokens(tokens)
        return tokens

    async def refresh_tokens(self) -> dict | None:
        if not self.tokens or not self.tokens.get("refresh_token"):
            return None

        async with httpx.AsyncClient() as client:
            response = await client.post(
                OURA_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self.tokens["refresh_token"],
                    "client_id": settings.oura_client_id,
                    "client_secret": settings.oura_client_secret,
                },
            )

        if response.status_code != 200:
            logger.error(f"Oura token refresh failed: {response.text}")
            return None

        tokens = response.json()
        self._save_tokens(tokens)
        return tokens

    async def _request(self, endpoint: str, params: dict | None = None) -> dict:
        if not self.tokens:
            raise Exception("Oura not authenticated")

        headers = {"Authorization": f"Bearer {self.tokens['access_token']}"}

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{OURA_API_BASE}{endpoint}",
                headers=headers,
                params=params,
            )

        if response.status_code == 401:
            new_tokens = await self.refresh_tokens()
            if not new_tokens:
                raise Exception("Oura token refresh failed")

            headers["Authorization"] = f"Bearer {new_tokens['access_token']}"
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{OURA_API_BASE}{endpoint}",
                    headers=headers,
                    params=params,
                )

        if response.status_code != 200:
            raise Exception(f"Oura API failed: {response.text}")

        return response.json()

    async def _request_paginated(self, endpoint: str, params: dict | None = None) -> list[dict]:
        """Fetch all pages of a paginated endpoint."""
        all_data = []
        params = dict(params) if params else {}

        while True:
            result = await self._request(endpoint, params)
            all_data.extend(result.get("data", []))
            next_token = result.get("next_token")
            if not next_token:
                break
            params["next_token"] = next_token

        return all_data

    def _local_today(self) -> str:
        """Get today's date in the configured timezone."""
        return datetime.now(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d")

    def _local_date(self, days_ago: int = 0) -> str:
        """Get a date N days ago in the configured timezone."""
        dt = datetime.now(ZoneInfo(settings.timezone)) - timedelta(days=days_ago)
        return dt.strftime("%Y-%m-%d")

    async def get_sleep(self, start_date: str, end_date: str) -> list[dict]:
        return await self._request_paginated(
            "/sleep", {"start_date": start_date, "end_date": end_date}
        )

    async def get_daily_sleep(self, start_date: str, end_date: str) -> list[dict]:
        return await self._request_paginated(
            "/daily_sleep", {"start_date": start_date, "end_date": end_date}
        )

    async def get_daily_readiness(self, start_date: str, end_date: str) -> list[dict]:
        return await self._request_paginated(
            "/daily_readiness", {"start_date": start_date, "end_date": end_date}
        )

    async def get_heart_rate(self, start_date: str, end_date: str) -> list[dict]:
        return await self._request_paginated(
            "/heartrate", {"start_datetime": f"{start_date}T00:00:00+01:00", "end_datetime": f"{end_date}T23:59:59+01:00"}
        )

    async def get_daily_stress(self, start_date: str, end_date: str) -> list[dict]:
        return await self._request_paginated(
            "/daily_stress", {"start_date": start_date, "end_date": end_date}
        )

    async def get_daily_spo2(self, start_date: str, end_date: str) -> list[dict]:
        return await self._request_paginated(
            "/daily_spo2", {"start_date": start_date, "end_date": end_date}
        )

    async def get_workouts(self, start_date: str, end_date: str) -> list[dict]:
        return await self._request_paginated(
            "/workout", {"start_date": start_date, "end_date": end_date}
        )

    def is_authenticated(self) -> bool:
        return self.tokens is not None and "access_token" in self.tokens

    def clear_tokens(self):
        if TOKEN_FILE.exists():
            TOKEN_FILE.unlink()
        self._tokens = None


oura_client = OuraClient()
