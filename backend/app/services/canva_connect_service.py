from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx


class CanvaConnectService:
    """Canva Connect OAuth + asset/design/export integration."""

    API_BASE = "https://api.canva.com/rest/v1"
    AUTHORIZE_URL = "https://www.canva.com/api/oauth/authorize"

    # Production redirect URI.
    # Railway variable takes priority, with the production URL as fallback.
    REDIRECT_URI = os.getenv(
        "CANVA_CONNECT_REDIRECT_URI",
        "https://agentic-content-generator-production.up.railway.app/api/canva/connect/oauth/callback",
    ).strip()

    SCOPES = [
        "asset:write",
        "design:content:write",
        "design:content:read",
        "design:meta:read",
    ]

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)

        self.token_file = (
            self.base_dir / ".canva_connect_tokens.json"
        )

        self._oauth_state: str | None = None
        self._code_verifier: str | None = None

    @property
    def client_id(self) -> str:
        """
        Support the Railway variable names currently used by the application.
        Also support the older CANVA_CONNECT_* names for backward compatibility.
        """
        return (
            os.getenv("CANVA_CLIENT_ID", "").strip()
            or os.getenv("CANVA_CONNECT_CLIENT_ID", "").strip()
        )

    @property
    def client_secret(self) -> str:
        """
        Support the Railway variable names currently used by the application.
        Also support the older CANVA_CONNECT_* names for backward compatibility.
        """
        return (
            os.getenv("CANVA_CLIENT_SECRET", "").strip()
            or os.getenv("CANVA_CONNECT_CLIENT_SECRET", "").strip()
        )

    def _require_credentials(self) -> None:
        if not self.client_id:
            raise RuntimeError(
                "CANVA_CLIENT_ID is not configured in the backend environment."
            )

        if not self.client_secret:
            raise RuntimeError(
                "CANVA_CLIENT_SECRET is not configured in the backend environment."
            )

        if not self.REDIRECT_URI:
            raise RuntimeError(
                "CANVA_CONNECT_REDIRECT_URI is not configured in the backend environment."
            )

    def _read_tokens(self) -> dict:
        if not self.token_file.exists():
            return {}

        try:
            value = json.loads(
                self.token_file.read_text(
                    encoding="utf-8"
                )
            )

            return value if isinstance(value, dict) else {}

        except (
            OSError,
            json.JSONDecodeError,
            TypeError,
        ):
            return {}

    def _write_tokens(self, tokens: dict) -> None:
        self.token_file.write_text(
            json.dumps(
                tokens,
                indent=2,
            ),
            encoding="utf-8",
        )

        try:
            os.chmod(
                self.token_file,
                0o600,
            )
        except OSError:
            pass

    def authorization_url(self) -> str:
        self._require_credentials()

        verifier = secrets.token_urlsafe(64)

        challenge = (
            base64.urlsafe_b64encode(
                hashlib.sha256(
                    verifier.encode("ascii")
                ).digest()
            )
            .decode("ascii")
            .rstrip("=")
        )

        state = secrets.token_urlsafe(48)

        self._code_verifier = verifier
        self._oauth_state = state

        query = urlencode(
            {
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "scope": " ".join(self.SCOPES),
                "response_type": "code",
                "client_id": self.client_id,
                "state": state,
                "redirect_uri": self.REDIRECT_URI,
            }
        )

        return f"{self.AUTHORIZE_URL}?{query}"

    async def exchange_code(
        self,
        code: str,
        state: str | None,
    ) -> dict:

        self._require_credentials()

        if not self._oauth_state or not secrets.compare_digest(
            self._oauth_state,
            str(state or ""),
        ):
            raise RuntimeError(
                "Canva OAuth state validation failed. Start authorization again."
            )

        if not self._code_verifier:
            raise RuntimeError(
                "Canva OAuth PKCE verifier is missing. Start authorization again."
            )

        auth = httpx.BasicAuth(
            self.client_id,
            self.client_secret,
        )

        data = {
            "grant_type": "authorization_code",
            "code_verifier": self._code_verifier,
            "code": code,
            "redirect_uri": self.REDIRECT_URI,
        }

        async with httpx.AsyncClient(
            timeout=60
        ) as client:

            response = await client.post(
                f"{self.API_BASE}/oauth/token",
                auth=auth,
                data=data,
            )

        if response.status_code >= 400:

            try:
                detail = response.json()
            except Exception:
                detail = response.text

            raise RuntimeError(
                f"Canva Connect token exchange failed: {detail}"
            )

        payload = response.json()

        expires_in = int(
            payload.get("expires_in") or 14400
        )

        payload["expires_at"] = (
            int(time.time())
            + max(60, expires_in)
        )

        self._write_tokens(payload)

        self._oauth_state = None
        self._code_verifier = None

        return payload

    async def _refresh(
        self,
        tokens: dict,
    ) -> str:

        refresh_token = str(
            tokens.get("refresh_token") or ""
        ).strip()

        if not refresh_token:
            raise RuntimeError(
                "Canva authorization has expired. Please reconnect Canva."
            )

        auth = httpx.BasicAuth(
            self.client_id,
            self.client_secret,
        )

        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }

        async with httpx.AsyncClient(
            timeout=60
        ) as client:

            response = await client.post(
                f"{self.API_BASE}/oauth/token",
                auth=auth,
                data=data,
            )

        if response.status_code >= 400:

            try:
                detail = response.json()
            except Exception:
                detail = response.text

            raise RuntimeError(
                f"Canva Connect token refresh failed: {detail}"
            )

        payload = response.json()

        payload["expires_at"] = (
            int(time.time())
            + int(payload.get("expires_in") or 14400)
        )

        self._write_tokens(payload)

        return str(
            payload["access_token"]
        )

    async def access_token(self) -> str:

        self._require_credentials()

        tokens = self._read_tokens()

        token = str(
            tokens.get("access_token") or ""
        ).strip()

        expires_at = int(
            tokens.get("expires_at") or 0
        )

        if (
            token
            and expires_at
            > int(time.time()) + 60
        ):
            return token

        if tokens.get("refresh_token"):
            return await self._refresh(tokens)

        raise RuntimeError(
            "Canva Connect is not authorized. "
            "Connect Canva before creating a design."
        )

    async def status(self) -> dict:

        tokens = self._read_tokens()

        expires_at = int(
            tokens.get("expires_at") or 0
        )

        return {
            "configured": bool(
                self.client_id
                and self.client_secret
                and self.REDIRECT_URI
            ),
            "authenticated": bool(
                tokens.get("access_token")
                and (
                    expires_at > int(time.time())
                    or tokens.get("refresh_token")
                )
            ),
            "scopes": str(
                tokens.get("scope") or ""
            ).split(),
        }

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs,
    ) -> dict:

        token = await self.access_token()

        headers = dict(
            kwargs.pop("headers", {}) or {}
        )

        headers["Authorization"] = (
            f"Bearer {token}"
        )

        async with httpx.AsyncClient(
            timeout=120
        ) as client:

            response = await client.request(
                method,
                f"{self.API_BASE}{path}",
                headers=headers,
                **kwargs,
            )

        if response.status_code >= 400:

            try:
                detail = response.json()
            except Exception:
                detail = response.text

            raise RuntimeError(
                f"Canva Connect API "
                f"{response.status_code}: {detail}"
            )

        return response.json()

    # ---------------------------------------------------------------
    # KEEP THE REST OF YOUR EXISTING SERVICE BELOW THIS POINT
    # ---------------------------------------------------------------
