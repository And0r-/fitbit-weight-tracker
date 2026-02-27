"""Oura Ring API client with OAuth2 token refresh."""
import json
import logging
from datetime import date, datetime
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

OURA_API_BASE = "https://api.ouraring.com/v2/usercollection"
OURA_TOKEN_URL = "https://api.ouraring.com/oauth/token"

# Config paths (host-level config, not container)
OURA_TOKEN_FILE = Path.home() / ".config" / "oura" / "tokens.json"
OURA_CREDENTIALS_FILE = Path.home() / ".config" / "oura" / "credentials.json"


class OuraClient:
    """Oura Ring API client with automatic token refresh."""

    def __init__(self):
        self._tokens: dict | None = None
        self._credentials: dict | None = None

    @property
    def tokens(self) -> dict | None:
        if self._tokens is None:
            self._tokens = self._load_json(OURA_TOKEN_FILE)
        return self._tokens

    @property
    def credentials(self) -> dict | None:
        if self._credentials is None:
            self._credentials = self._load_json(OURA_CREDENTIALS_FILE)
        return self._credentials

    @staticmethod
    def _load_json(path: Path) -> dict | None:
        if path.exists():
            try:
                return json.loads(path.read_text())
            except (json.JSONDecodeError, IOError):
                return None
        return None

    def _save_tokens(self, tokens: dict):
        OURA_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        OURA_TOKEN_FILE.write_text(json.dumps(tokens, indent=2))
        self._tokens = tokens

    def is_authenticated(self) -> bool:
        return self.tokens is not None and "access_token" in self.tokens

    async def refresh_tokens(self) -> dict | None:
        """Refresh access token using refresh token."""
        if not self.tokens or not self.tokens.get("refresh_token"):
            return None
        if not self.credentials:
            logger.error("No Oura credentials found for token refresh")
            return None

        async with httpx.AsyncClient() as client:
            response = await client.post(
                OURA_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self.tokens["refresh_token"],
                    "client_id": self.credentials["client_id"],
                    "client_secret": self.credentials["client_secret"],
                },
            )

        if response.status_code != 200:
            logger.error(f"Oura token refresh failed: {response.text}")
            return None

        tokens = response.json()
        self._save_tokens(tokens)
        logger.info("Oura tokens refreshed successfully")
        return tokens

    async def _request(self, endpoint: str, params: dict | None = None) -> dict:
        """Make authenticated GET request with auto-refresh on 401."""
        if not self.tokens:
            raise Exception("Oura not authenticated")

        headers = {"Authorization": f"Bearer {self.tokens['access_token']}"}

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{OURA_API_BASE}/{endpoint}",
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
                    f"{OURA_API_BASE}/{endpoint}",
                    headers=headers,
                    params=params,
                )

        if response.status_code != 200:
            raise Exception(f"Oura API error ({response.status_code}): {response.text}")

        return response.json()

    async def get_heartrate(self, day: str) -> dict:
        """Get heart rate data for a specific date (YYYY-MM-DD)."""
        return await self._request("heartrate", {
            "start_datetime": f"{day}T00:00:00+00:00",
            "end_datetime": f"{day}T23:59:59+00:00",
        })

    async def get_sleep(self, day: str) -> dict:
        """Get sleep data for a specific date."""
        return await self._request("sleep", {
            "start_date": day,
            "end_date": day,
        })

    async def get_readiness(self, day: str) -> dict:
        """Get daily readiness for a specific date."""
        return await self._request("daily_readiness", {
            "start_date": day,
            "end_date": day,
        })

    async def get_stress(self, day: str) -> dict:
        """Get daily stress for a specific date."""
        return await self._request("daily_stress", {
            "start_date": day,
            "end_date": day,
        })

    async def get_spo2(self, day: str) -> dict:
        """Get daily SpO2 for a specific date."""
        return await self._request("daily_spo2", {
            "start_date": day,
            "end_date": day,
        })

    async def get_daily(self, day: str) -> dict:
        """Get combined daily view (sleep, readiness, stress, spo2)."""
        results = {}
        for name, coro in [
            ("sleep", self.get_sleep(day)),
            ("readiness", self.get_readiness(day)),
            ("stress", self.get_stress(day)),
            ("spo2", self.get_spo2(day)),
        ]:
            try:
                results[name] = await coro
            except Exception as e:
                results[name] = {"error": str(e)}
        return {"date": day, **results}


# Singleton
oura_client = OuraClient()
