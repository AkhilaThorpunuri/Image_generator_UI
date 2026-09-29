from __future__ import annotations

import asyncio
import json
import math
import os
import struct
import webbrowser
import base64
import hashlib
import secrets
import time
from typing import Any
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse, urlencode

import httpx
from mcp import Client
from mcp.client.auth import (
    AuthorizationCodeResult,
    OAuthClientProvider,
    TokenStorage,
)
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)
from pydantic import AnyUrl


CANVA_MCP_URL = "https://mcp.canva.com/mcp"

CANVA_REDIRECT_URI = (
    "http://127.0.0.1:8000/api/canva/oauth/callback"
)

CANVA_CONNECT_API_URL = "https://api.canva.com/rest/v1"
CANVA_CONNECT_AUTHORIZE_URL = "https://www.canva.com/api/oauth/authorize"
CANVA_CONNECT_TOKEN_URL = f"{CANVA_CONNECT_API_URL}/oauth/token"
CANVA_CONNECT_REDIRECT_URI = os.getenv(
    "CANVA_CONNECT_REDIRECT_URI",
    "https://agentic-content-generator-production.up.railway.app/api/canva/connect/oauth/callback",
).strip()
CANVA_CONNECT_SCOPES = os.getenv(
    "CANVA_CONNECT_SCOPES",
    "asset:read asset:write design:content:read design:content:write design:meta:read",
).strip()
CANVA_CONNECT_TOKEN_FILE = Path(
    os.getenv(
        "CANVA_CONNECT_TOKEN_FILE",
        str(Path(__file__).resolve().parents[2] / ".canva_connect_tokens.json"),
    )
)


class CanvaTokenStorage(TokenStorage):
    """
    Temporary in-memory OAuth storage.

    Stores:
    - OAuth tokens
    - OAuth client registration information
    """

    def __init__(self) -> None:
        self._tokens: OAuthToken | None = None
        self._client_info: OAuthClientInformationFull | None = None

    async def get_tokens(self) -> OAuthToken | None:
        return self._tokens

    async def set_tokens(
        self,
        tokens: OAuthToken,
    ) -> None:
        self._tokens = tokens

    async def get_client_info(
        self,
    ) -> OAuthClientInformationFull | None:
        return self._client_info

    async def set_client_info(
        self,
        client_info: OAuthClientInformationFull,
    ) -> None:
        self._client_info = client_info


class CanvaMCPService:
    """
    Backend Canva MCP service.

    Handles:

    - Canva OAuth
    - MCP Streamable HTTP
    - Canva MCP tool discovery
    - Canva MCP tool execution
    """

    def __init__(self) -> None:
        self.server_url = CANVA_MCP_URL

        self.storage = CanvaTokenStorage()

        self.oauth_provider: OAuthClientProvider | None = None

        self.authorization_url: str | None = None

        self.oauth_started = False
        self.oauth_completed = False

        self._oauth_lock = asyncio.Lock()
        self._authorization_lock = asyncio.Lock()

        self._oauth_callback_future: (
            asyncio.Future[AuthorizationCodeResult] | None
        ) = None

        # Canva Connect OAuth is separate from Canva MCP OAuth. It is used only
        # to upload local generated image bytes and create a design from the
        # resulting private Canva asset. No public URL/tunnel is required.
        self.connect_authorization_url: str | None = None
        self.connect_oauth_state: str | None = None
        self.connect_code_verifier: str | None = None

    # ================================================================
    # OAuth provider
    # ================================================================

    def create_oauth_provider(
        self,
    ) -> OAuthClientProvider:
        """
        Create and retain the Canva OAuth provider.
        """

        client_metadata = OAuthClientMetadata(
            client_name="Image Generator UI",
            redirect_uris=[
                AnyUrl(CANVA_REDIRECT_URI),
            ],
            grant_types=[
                "authorization_code",
                "refresh_token",
            ],
            response_types=[
                "code",
            ],
        )

        provider = OAuthClientProvider(
            server_url=self.server_url,
            client_metadata=client_metadata,
            storage=self.storage,
            redirect_handler=self.open_authorization_url,
            callback_handler=self.callback_handler,
        )

        # Canva rejects a token request that uses HTTP Basic authentication
        # while also sending client_id in the form body. MCP SDK 2.2.0 keeps
        # client_id in the body for client_secret_basic, even though it removes
        # client_secret. Normalize the request after the SDK prepares auth so
        # Canva receives exactly one client authentication method.
        original_prepare_token_auth = provider.context.prepare_token_auth

        def canva_prepare_token_auth(
            data: dict[str, str],
            headers: dict[str, str] | None = None,
        ) -> tuple[dict[str, str], dict[str, str]]:
            prepared_data, prepared_headers = original_prepare_token_auth(
                data,
                headers,
            )
            auth_method = (
                provider.context.client_info.token_endpoint_auth_method
                if provider.context.client_info is not None
                else None
            )
            if auth_method == "client_secret_basic":
                prepared_data = {
                    key: value
                    for key, value in prepared_data.items()
                    if key != "client_id"
                }
            return prepared_data, prepared_headers

        provider.context.prepare_token_auth = canva_prepare_token_auth

        self.oauth_provider = provider

        return provider

    # ================================================================
    # OAuth browser redirect
    # ================================================================

    async def open_authorization_url(
        self,
        authorization_url: str,
    ) -> None:
        """
        Open the Canva OAuth authorization URL.
        """

        async with self._authorization_lock:
            self.authorization_url = authorization_url
            self.oauth_started = True
            self.oauth_completed = False

            print()
            print("=" * 80)
            print("CANVA MCP AUTHORIZATION")
            print("=" * 80)
            print()
            print("Authorization URL:")
            print()
            print(authorization_url)
            print()

            try:
                webbrowser.open(
                    authorization_url
                )
            except Exception as exc:
                print(
                    "Unable to automatically open browser:",
                    repr(exc),
                )

    # ================================================================
    # OAuth callback
    # ================================================================

    async def callback_handler(
        self,
    ) -> AuthorizationCodeResult:
        """
        Wait for the FastAPI Canva OAuth callback.
        """

        if (
            self._oauth_callback_future is None
            or self._oauth_callback_future.done()
        ):
            loop = asyncio.get_running_loop()

            self._oauth_callback_future = (
                loop.create_future()
            )

        print()
        print("=" * 80)
        print("WAITING FOR CANVA OAUTH CALLBACK")
        print("=" * 80)
        print()
        print(
            "Waiting for Canva to redirect to:"
        )
        print(
            CANVA_REDIRECT_URI
        )
        print()

        try:
            result = await asyncio.wait_for(
                self._oauth_callback_future,
                timeout=600,
            )

            self.oauth_completed = True

            print()
            print("=" * 80)
            print("CANVA OAUTH CALLBACK RECEIVED")
            print("=" * 80)
            print()

            return result

        except asyncio.TimeoutError as exc:
            self.oauth_completed = False

            raise RuntimeError(
                "Timed out waiting for the Canva OAuth callback."
            ) from exc

        finally:
            self._oauth_callback_future = None

    async def complete_oauth_callback(
        self,
        code: str | None,
        state: str | None,
        iss: str | None = None,
        error: str | None = None,
        error_description: str | None = None,
    ) -> dict[str, Any]:
        """
        Receive OAuth callback parameters from FastAPI.
        """

        if error:
            message = (
                error_description
                or error
                or "Canva OAuth authorization failed."
            )

            if (
                self._oauth_callback_future is not None
                and not self._oauth_callback_future.done()
            ):
                self._oauth_callback_future.set_exception(
                    RuntimeError(message)
                )

            self.oauth_completed = False

            return {
                "success": False,
                "error": error,
                "error_description": error_description,
                "message": message,
            }

        if not code:
            message = (
                "Canva OAuth callback did not contain "
                "an authorization code."
            )

            if (
                self._oauth_callback_future is not None
                and not self._oauth_callback_future.done()
            ):
                self._oauth_callback_future.set_exception(
                    RuntimeError(message)
                )

            self.oauth_completed = False

            return {
                "success": False,
                "message": message,
            }

        result = AuthorizationCodeResult(
            code=code,
            state=state,
            iss=iss,
        )

        if (
            self._oauth_callback_future is None
            or self._oauth_callback_future.done()
        ):
            return {
                "success": False,
                "message": (
                    "Received Canva OAuth callback, but there "
                    "is no active OAuth authorization request."
                ),
            }

        self._oauth_callback_future.set_result(
            result
        )

        return {
            "success": True,
            "message": (
                "Canva OAuth callback received successfully."
            ),
        }

    # ================================================================
    # Callback URL helper
    # ================================================================

    async def complete_oauth_callback_url(
        self,
        callback_url: str,
    ) -> dict[str, Any]:
        """
        Process a complete Canva callback URL.
        """

        parsed = urlparse(
            callback_url
        )

        params = parse_qs(
            parsed.query
        )

        def first_value(
            name: str,
        ) -> str | None:
            values = params.get(name)

            if not values:
                return None

            return values[0]

        return await self.complete_oauth_callback(
            code=first_value("code"),
            state=first_value("state"),
            iss=first_value("iss"),
            error=first_value("error"),
            error_description=first_value(
                "error_description"
            ),
        )

    # ================================================================
    # Authentication
    # ================================================================

    def is_authenticated(self) -> bool:
        return self.storage._tokens is not None

    # ================================================================
    # List Canva MCP tools
    # ================================================================

    async def list_tools(
        self,
    ) -> list[dict[str, Any]]:
        """
        Connect to Canva MCP and list available tools.
        """

        async with self._oauth_lock:

            if self.oauth_provider is None:
                self.create_oauth_provider()

            assert self.oauth_provider is not None

            async with httpx.AsyncClient(
                auth=self.oauth_provider,
                timeout=httpx.Timeout(
                    30.0,
                    read=300.0,
                ),
            ) as http_client:

                transport = streamable_http_client(
                    self.server_url,
                    http_client=http_client,
                )

                async with Client(
                    transport
                ) as client:

                    response = await client.list_tools()

                    tools: list[
                        dict[str, Any]
                    ] = []

                    for tool in response.tools:
                        tools.append(
                            {
                                "name": tool.name,
                                "description": (
                                    tool.description
                                    or ""
                                ),
                            }
                        )

                    self.oauth_completed = (
                        self.storage._tokens is not None
                    )

                    return tools

    # ================================================================
    # Generic Canva MCP tool call
    # ================================================================

    async def get_editable_design_snapshot(self, design_id: str) -> dict[str, Any]:
        """Return Canva design content used by the document/image AI edit flow.

        This helper intentionally relies on Canva MCP's design-content tools so
        imported PDF/PPT/PPTX designs and regular Canva designs use the same
        editing path.
        """
        design_id = str(design_id or "").strip()
        if not design_id:
            raise ValueError("A Canva design ID is required.")

        result = await self.call_tool("get-design-content", {"design_id": design_id})
        return self._extract_payload(result)

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> Any:
        """
        Call a Canva MCP tool.
        """

        if self.oauth_provider is None:
            self.create_oauth_provider()

        assert self.oauth_provider is not None

        if arguments is None:
            arguments = {}

        async with httpx.AsyncClient(
            auth=self.oauth_provider,
            timeout=httpx.Timeout(
                30.0,
                read=300.0,
            ),
        ) as http_client:

            transport = streamable_http_client(
                self.server_url,
                http_client=http_client,
            )

            async with Client(
                transport
            ) as client:

                result = await client.call_tool(
                    tool_name,
                    arguments,
                )

                self.oauth_completed = (
                    self.storage._tokens is not None
                )

                return result

    # ================================================================
    # Canva Connect OAuth + local-file upload
    # ================================================================

    @staticmethod
    def _connect_client_id() -> str:
        return os.getenv("CANVA_CONNECT_CLIENT_ID", "").strip()

    @staticmethod
    def _connect_client_secret() -> str:
        return os.getenv("CANVA_CONNECT_CLIENT_SECRET", "").strip()

    @staticmethod
    def _read_connect_tokens() -> dict[str, Any]:
        try:
            if CANVA_CONNECT_TOKEN_FILE.exists():
                value = json.loads(CANVA_CONNECT_TOKEN_FILE.read_text(encoding="utf-8"))
                return value if isinstance(value, dict) else {}
        except Exception:
            pass
        return {}

    @staticmethod
    def _write_connect_tokens(value: dict[str, Any]) -> None:
        CANVA_CONNECT_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CANVA_CONNECT_TOKEN_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, indent=2), encoding="utf-8")
        os.replace(tmp, CANVA_CONNECT_TOKEN_FILE)

    def _connect_configured(self) -> bool:
        return bool(self._connect_client_id() and self._connect_client_secret())

    def _connect_status(self) -> dict[str, Any]:
        tokens = self._read_connect_tokens()
        expires_at = float(tokens.get("expires_at") or 0)
        return {
            "configured": self._connect_configured(),
            "authenticated": bool(tokens.get("access_token")),
            "expires_at": expires_at or None,
            "has_refresh_token": bool(tokens.get("refresh_token")),
            "redirect_uri": CANVA_CONNECT_REDIRECT_URI,
            "scopes": CANVA_CONNECT_SCOPES,
            "authorization_url": self.connect_authorization_url,
        }

    def start_connect_oauth(self) -> dict[str, Any]:
        if not self._connect_configured():
            raise RuntimeError(
                "Canva Connect OAuth is not configured. Set "
                "CANVA_CONNECT_CLIENT_ID and CANVA_CONNECT_CLIENT_SECRET. "
                f"Register this redirect URL in Canva: {CANVA_CONNECT_REDIRECT_URI}"
            )

        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        state = secrets.token_urlsafe(32)
        self.connect_code_verifier = verifier
        self.connect_oauth_state = state

        params = {
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": CANVA_CONNECT_SCOPES,
            "response_type": "code",
            "client_id": self._connect_client_id(),
            "state": state,
            "redirect_uri": CANVA_CONNECT_REDIRECT_URI,
        }
        url = f"{CANVA_CONNECT_AUTHORIZE_URL}?{urlencode(params)}"
        self.connect_authorization_url = url
        return {
            "success": True,
            "authorization_url": url,
            "redirect_uri": CANVA_CONNECT_REDIRECT_URI,
            "scopes": CANVA_CONNECT_SCOPES,
        }

    async def complete_connect_oauth_callback(
        self,
        code: str | None,
        state: str | None,
        error: str | None = None,
        error_description: str | None = None,
    ) -> dict[str, Any]:
        if error:
            raise RuntimeError(error_description or error)
        if not code:
            raise RuntimeError("Canva Connect OAuth callback did not contain an authorization code.")
        if not state or state != self.connect_oauth_state:
            raise RuntimeError("Canva Connect OAuth state validation failed. Start authorization again.")
        if not self.connect_code_verifier:
            raise RuntimeError("Canva Connect OAuth verifier is missing. Start authorization again.")
        if not self._connect_configured():
            raise RuntimeError("Canva Connect OAuth client credentials are not configured.")

        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CANVA_CONNECT_REDIRECT_URI,
            "code_verifier": self.connect_code_verifier,
        }
        basic = base64.b64encode(f"{self._connect_client_id()}:{self._connect_client_secret()}".encode("utf-8")).decode("ascii")
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                CANVA_CONNECT_TOKEN_URL,
                data=data,
                headers={"Accept": "application/json", "Authorization": f"Basic {basic}"},
            )
        payload = response.json() if response.content else {}
        if response.status_code >= 400:
            raise RuntimeError(
                f"Canva Connect token exchange failed ({response.status_code}): "
                f"{payload.get('message') or payload.get('error_description') or payload.get('error') or payload}"
            )

        expires_in = int(payload.get("expires_in") or 14400)
        tokens = {
            "access_token": str(payload.get("access_token") or ""),
            "refresh_token": str(payload.get("refresh_token") or ""),
            "token_type": str(payload.get("token_type") or "bearer"),
            "scope": str(payload.get("scope") or CANVA_CONNECT_SCOPES),
            "expires_at": time.time() + max(60, expires_in - 30),
        }
        if not tokens["access_token"]:
            raise RuntimeError("Canva Connect did not return an access token.")
        self._write_connect_tokens(tokens)
        self.connect_oauth_state = None
        self.connect_code_verifier = None
        return {"success": True, "message": "Canva Connect authorization completed."}

    async def _get_connect_access_token(self) -> str:
        tokens = self._read_connect_tokens()
        access_token = str(tokens.get("access_token") or "").strip()
        expires_at = float(tokens.get("expires_at") or 0)
        if access_token and expires_at > time.time() + 30:
            return access_token

        refresh_token = str(tokens.get("refresh_token") or "").strip()
        if not refresh_token:
            raise RuntimeError(
                "Canva Connect is not authorized. Connect Canva first."
            )
        if not self._connect_configured():
            raise RuntimeError("Canva Connect client credentials are not configured.")

        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        basic = base64.b64encode(f"{self._connect_client_id()}:{self._connect_client_secret()}".encode("utf-8")).decode("ascii")
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                CANVA_CONNECT_TOKEN_URL,
                data=data,
                headers={"Accept": "application/json", "Authorization": f"Basic {basic}"},
            )
        payload = response.json() if response.content else {}
        if response.status_code >= 400:
            raise RuntimeError(
                f"Canva Connect token refresh failed ({response.status_code}): "
                f"{payload.get('message') or payload.get('error_description') or payload.get('error') or payload}"
            )
        new_access = str(payload.get("access_token") or "").strip()
        if not new_access:
            raise RuntimeError("Canva Connect token refresh returned no access token.")
        new_refresh = str(payload.get("refresh_token") or refresh_token)
        expires_in = int(payload.get("expires_in") or 14400)
        tokens.update({
            "access_token": new_access,
            "refresh_token": new_refresh,
            "token_type": str(payload.get("token_type") or "bearer"),
            "scope": str(payload.get("scope") or tokens.get("scope") or CANVA_CONNECT_SCOPES),
            "expires_at": time.time() + max(60, expires_in - 30),
        })
        self._write_connect_tokens(tokens)
        return new_access

    async def upload_local_asset_to_canva(self, local_path: str, asset_name: str) -> dict[str, Any]:
        path = Path(local_path)
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"Generated image was not found: {path}")
        if path.stat().st_size >= 50 * 1024 * 1024:
            raise RuntimeError("Canva image assets must be smaller than 50 MB.")

        token = await self._get_connect_access_token()
        safe_name = Path(asset_name).name[:50] or "generated-image.png"
        name_base64 = base64.b64encode(safe_name.encode("utf-8")).decode("ascii")
        content = path.read_bytes()
        mime = "image/png"
        suffix = path.suffix.lower()
        if suffix in {".jpg", ".jpeg"}:
            mime = "image/jpeg"
        elif suffix == ".webp":
            mime = "image/webp"
        elif suffix == ".gif":
            mime = "image/gif"

        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=300.0)) as client:
            response = await client.post(
                f"{CANVA_CONNECT_API_URL}/asset-uploads",
                content=content,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/octet-stream",
                    "Asset-Upload-Metadata": json.dumps({"name_base64": name_base64}),
                },
            )
        payload = response.json() if response.content else {}
        if response.status_code >= 400:
            raise RuntimeError(
                f"Canva asset upload failed ({response.status_code}): "
                f"{payload.get('message') or payload.get('code') or payload}"
            )
        job = payload.get("job") or {}
        job_id = str(job.get("id") or "").strip()
        status = str(job.get("status") or "").strip().lower()
        if not job_id:
            raise RuntimeError(f"Canva asset upload did not return a job ID: {payload}")

        delay = 0.4
        for _ in range(12):
            if status == "success":
                break
            if status == "failed":
                error = job.get("error") or {}
                raise RuntimeError(
                    f"Canva asset upload failed: {error.get('message') or error.get('code') or job}"
                )
            await asyncio.sleep(delay)
            delay = min(delay * 1.4, 2.5)
            async with httpx.AsyncClient(timeout=30.0) as client:
                poll = await client.get(
                    f"{CANVA_CONNECT_API_URL}/asset-uploads/{quote(job_id)}",
                    headers={"Authorization": f"Bearer {token}"},
                )
            poll_payload = poll.json() if poll.content else {}
            if poll.status_code >= 400:
                raise RuntimeError(
                    f"Canva asset upload status failed ({poll.status_code}): "
                    f"{poll_payload.get('message') or poll_payload.get('code') or poll_payload}"
                )
            job = poll_payload.get("job") or {}
            status = str(job.get("status") or "").strip().lower()

        if status != "success":
            raise RuntimeError("Timed out waiting for Canva to finish uploading the generated image.")
        asset = job.get("asset") or {}
        asset_id = str(asset.get("id") or "").strip()
        if not asset_id:
            raise RuntimeError(f"Canva upload completed without an asset ID: {payload}")
        return {"asset_id": asset_id, "asset": asset, "job": job}

    async def create_connect_design_from_asset(
        self,
        asset_id: str,
        asset_name: str,
        width: int,
        height: int,
    ) -> dict[str, Any]:
        token = await self._get_connect_access_token()
        width = max(40, min(8000, int(width or 1080)))
        height = max(40, min(8000, int(height or 1080)))
        while width * height > 25_000_000:
            width = max(40, int(width * 0.9))
            height = max(40, int(height * 0.9))

        payload = {
            "type": "type_and_asset",
            "design_type": {"type": "custom", "width": width, "height": height},
            "asset_id": asset_id,
            "title": Path(asset_name).stem[:255] or "Generated Image",
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{CANVA_CONNECT_API_URL}/designs",
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
        result = response.json() if response.content else {}
        if response.status_code >= 400:
            raise RuntimeError(
                f"Canva design creation failed ({response.status_code}): "
                f"{result.get('message') or result.get('code') or result}"
            )
        design = result.get("design") or {}
        design_id = str(design.get("id") or "").strip()
        urls = design.get("urls") or {}
        edit_url = str(urls.get("edit_url") or "").strip()
        view_url = str(urls.get("view_url") or "").strip()
        if not edit_url and design_id:
            edit_url = f"https://www.canva.com/design/{design_id}/edit"
        if not edit_url:
            raise RuntimeError(f"Canva created the design but returned no edit URL: {result}")
        return {
            "design_id": design_id,
            "edit_url": edit_url,
            "view_url": view_url,
            "design": design,
        }

    async def create_editable_design_from_local_image(
        self,
        local_path: str,
        design_type: str = "poster",
        asset_name: str = "generated-image.png",
        user_intent: str = "Create an editable Canva design from this generated image.",
    ) -> dict[str, Any]:
        """Upload a private local image directly to Canva, then create a design.

        No public URL, Cloudflare tunnel, ngrok, or temporary file host is used.
        Canva Connect receives the image bytes directly from this backend. The
        Canva MCP connection remains available for MCP tools and can optionally
        be used to inspect the newly created design.
        """
        width, height = self._image_dimensions(local_path)
        upload = await self.upload_local_asset_to_canva(local_path, asset_name)
        created = await self.create_connect_design_from_asset(
            asset_id=upload["asset_id"],
            asset_name=asset_name,
            width=width,
            height=height,
        )

        mcp_checked = False
        mcp_error = ""
        if self.is_authenticated():
            try:
                tools = await self.list_tools()
                tool_names = {str(item.get("name", "")) for item in tools}
                if "get-design" in tool_names and created["design_id"]:
                    await self.call_tool("get-design", {"design_id": created["design_id"]})
                    mcp_checked = True
            except Exception as exc:
                mcp_error = str(exc)

        return {
            "success": True,
            "design_id": created["design_id"],
            "edit_url": created["edit_url"],
            "view_url": created["view_url"],
            "asset_id": upload["asset_id"],
            "title": Path(asset_name).stem or "Generated Image",
            "width": width,
            "height": height,
            "message": (
                "The generated image was uploaded directly to Canva and placed in a Canva design. "
                "Open the Canva design to edit it."
            ),
            "source": "canva-connect-local-upload",
            "mcp_used": mcp_checked,
            "mcp_message": mcp_error or None,
            "public_tunnel_required": False,
            "editable_scope": "raster-image-element",
        }

    # ================================================================
    # Generated-image -> editable Canva design
    # ================================================================

    @staticmethod
    def _extract_payload(value: Any) -> Any:
        """Best-effort conversion of an MCP result into JSON-like data."""
        if value is None or isinstance(value, (str, int, float, bool)):
            if isinstance(value, str):
                text = value.strip()
                if text.startswith("{") or text.startswith("["):
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError:
                        return value
            return value
        if isinstance(value, dict):
            return {str(k): CanvaMCPService._extract_payload(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [CanvaMCPService._extract_payload(v) for v in value]
        for attribute in ("structuredContent", "structured_content", "data", "content"):
            if hasattr(value, attribute):
                try:
                    payload = getattr(value, attribute)
                except Exception:
                    continue
                if payload is not None:
                    return CanvaMCPService._extract_payload(payload)
        if hasattr(value, "text"):
            try:
                return CanvaMCPService._extract_payload(getattr(value, "text"))
            except Exception:
                pass
        try:
            return {
                str(k): CanvaMCPService._extract_payload(v)
                for k, v in vars(value).items()
                if not str(k).startswith("_")
            }
        except Exception:
            return str(value)

    @classmethod
    def _find_value(cls, value: Any, key_names: set[str]) -> Any:
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower() in key_names and item not in (None, ""):
                    return item
            for item in value.values():
                found = cls._find_value(item, key_names)
                if found not in (None, ""):
                    return found
        elif isinstance(value, list):
            for item in value:
                found = cls._find_value(item, key_names)
                if found not in (None, ""):
                    return found
        return None

    @staticmethod
    def _image_dimensions(path: str | None) -> tuple[int, int]:
        """Read common PNG/JPEG/WEBP dimensions without requiring Pillow."""
        fallback = (1080, 1080)
        if not path:
            return fallback
        try:
            with open(path, "rb") as handle:
                header = handle.read(32)
                if header.startswith(b"\x89PNG\r\n\x1a\n") and len(header) >= 24:
                    return struct.unpack(">II", header[16:24])
                if header.startswith(b"RIFF") and header[8:12] == b"WEBP" and header[12:16] == b"VP8X" and len(header) >= 30:
                    return (1 + int.from_bytes(header[24:27], "little"), 1 + int.from_bytes(header[27:30], "little"))
                if header.startswith(b"\xff\xd8"):
                    handle.seek(2)
                    while True:
                        marker_start = handle.read(1)
                        if not marker_start:
                            break
                        if marker_start != b"\xff":
                            continue
                        marker = handle.read(1)
                        while marker == b"\xff":
                            marker = handle.read(1)
                        if not marker:
                            break
                        if marker in (b"\xd8", b"\xd9"):
                            continue
                        length_bytes = handle.read(2)
                        if len(length_bytes) != 2:
                            break
                        length = struct.unpack(">H", length_bytes)[0]
                        if length < 2:
                            break
                        if marker[0] in {0xC0,0xC1,0xC2,0xC3,0xC5,0xC6,0xC7,0xC9,0xCA,0xCB,0xCD,0xCE,0xCF}:
                            data = handle.read(5)
                            if len(data) == 5:
                                height, width = struct.unpack(">HH", data[1:5])
                                return width, height
                            break
                        handle.seek(length - 2, os.SEEK_CUR)
        except (OSError, ValueError, struct.error):
            pass
        return fallback

    async def create_editable_design_from_image(
        self,
        image_url: str,
        design_type: str = "poster",
        asset_name: str = "generated-image.png",
        user_intent: str = "Create an editable Canva design from this generated image.",
        local_path: str | None = None,
    ) -> dict[str, Any]:
        """Create an editable Canva design from a generated local image.

        The Image Generator uses the local-file Canva Connect workflow.
        No public image URL, Cloudflare tunnel, ngrok, or temporary file host
        is required.

        ``image_url`` is retained for backwards compatibility with older
        callers, but it is intentionally not used when ``local_path`` is
        available. The new flow uploads the local image bytes directly to
        Canva through the Connect API and creates a design from that private
        asset.
        """
        if local_path:
            return await self.create_editable_design_from_local_image(
                local_path=local_path,
                design_type=design_type,
                asset_name=asset_name,
                user_intent=user_intent,
            )

        raise RuntimeError(
            "Canva local-image creation requires the backend local_path. "
            "The current application does not use public image URLs or tunnels. "
            "Pass the generated image's local filesystem path."
        )

    # ================================================================
    # Connection status
    # ================================================================

    def get_connect_connection_status(self) -> dict[str, Any]:
        return self._connect_status()


    async def get_connection_status(
        self,
    ) -> dict[str, Any]:
        """
        Return current Canva MCP connection status.
        """

        tokens = await self.storage.get_tokens()
        client_info = await self.storage.get_client_info()

        return {
            "server": self.server_url,
            "oauth_started": self.oauth_started,
            "oauth_completed": self.oauth_completed,
            "authenticated": tokens is not None,
            "client_registered": client_info is not None,
            "authorization_url": self.authorization_url,
        }

    # ================================================================
    # Reset OAuth
    # ================================================================

    async def reset_oauth(
        self,
    ) -> None:
        """
        Reset the in-memory OAuth session.

        This does not revoke Canva authorization.
        """

        self.storage._tokens = None
        self.storage._client_info = None

        self.authorization_url = None

        self.oauth_started = False
        self.oauth_completed = False

        self._oauth_callback_future = None

        self.oauth_provider = None

    # ================================================================
    # Development test
    # ================================================================

    async def test_connection(
        self,
    ) -> list[dict[str, Any]]:
        """
        Test Canva MCP and print discovered tools.
        """

        print()
        print("=" * 80)
        print("CANVA MCP CONNECTION TEST")
        print("=" * 80)
        print()

        print(
            await self.get_connection_status()
        )

        tools = await self.list_tools()

        print()
        print(
            f"Canva MCP returned {len(tools)} tools."
        )

        for tool in tools:
            print(
                f"- {tool['name']}"
            )

        return tools


# ====================================================================
# Singleton
# ====================================================================

canva_mcp_service = CanvaMCPService()


# ====================================================================
# Local development entry point
# ====================================================================

async def test_service() -> None:
    service = canva_mcp_service

    print()
    print("=" * 80)
    print("CANVA MCP SERVICE")
    print("=" * 80)
    print()

    print(
        await service.get_connection_status()
    )


if __name__ == "__main__":
    asyncio.run(
        test_service()
    )
    canva_mcp_service = CanvaMCPService()
