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

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

# Load backend/.env for local development. Environment variables already set
# by the process take precedence by using override=False.
if load_dotenv is not None:
    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)


class CanvaConnectService:
    """Local-only Canva Connect OAuth + asset/design/export integration."""

    API_BASE = "https://api.canva.com/rest/v1"
    AUTHORIZE_URL = "https://www.canva.com/api/oauth/authorize"
    @property
def redirect_uri(self) -> str:
    return (
        os.getenv("CANVA_CONNECT_REDIRECT_URI", "").strip()
        or "http://127.0.0.1:8000/api/canva/connect/oauth/callback"
    )

    # These are the Connect permissions required by this application:
    # upload the generated image, create the design, and export the final design.
    SCOPES = [
        "asset:write",
        "design:content:write",
        "design:content:read",
        "design:meta:read",
    ]

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self.token_file = self.base_dir / ".canva_connect_tokens.json"
        self._oauth_state: str | None = None
        self._code_verifier: str | None = None

    @property
    def client_id(self) -> str:
        return (
            os.getenv("CANVA_CONNECT_CLIENT_ID", "").strip()
            or os.getenv("CANVA_CLIENT_ID", "").strip()
        )

    @property
    def client_secret(self) -> str:
        return (
            os.getenv("CANVA_CONNECT_CLIENT_SECRET", "").strip()
            or os.getenv("CANVA_CLIENT_SECRET", "").strip()
        )

    def _require_credentials(self) -> None:
        if not self.client_id or not self.client_secret:
            raise RuntimeError(
                "Canva Connect credentials are missing. Set CANVA_CONNECT_CLIENT_ID "
                "and CANVA_CONNECT_CLIENT_SECRET (or CANVA_CLIENT_ID and "
                "CANVA_CLIENT_SECRET) in backend/.env or the backend environment."
            )

    def _read_tokens(self) -> dict:
        if not self.token_file.exists():
            return {}
        try:
            value = json.loads(self.token_file.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError, TypeError):
            return {}

    def _write_tokens(self, tokens: dict) -> None:
        # Local development only. The token file must never be committed.
        self.token_file.write_text(
            json.dumps(tokens, indent=2),
            encoding="utf-8",
        )
        try:
            os.chmod(self.token_file, 0o600)
        except OSError:
            pass

    def authorization_url(self) -> str:
        self._require_credentials()

        # RFC 7636 PKCE verifier: 43-128 URL-safe characters.
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).decode("ascii").rstrip("=")
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
                "redirect_uri": self.redirect_uri,
            }
        )
        return f"{self.AUTHORIZE_URL}?{query}"

    async def exchange_code(self, code: str, state: str | None) -> dict:
        self._require_credentials()
        if not self._oauth_state or not secrets.compare_digest(
            self._oauth_state,
            str(state or ""),
        ):
            raise RuntimeError("Canva OAuth state validation failed. Start authorization again.")
        if not self._code_verifier:
            raise RuntimeError("Canva OAuth PKCE verifier is missing. Start authorization again.")

        auth = httpx.BasicAuth(self.client_id, self.client_secret)
        data = {
            "grant_type": "authorization_code",
            "code_verifier": self._code_verifier,
            "code": code,
            "redirect_uri": self.redirect_uri,
        }

        async with httpx.AsyncClient(timeout=60) as client:
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
            raise RuntimeError(f"Canva Connect token exchange failed: {detail}")

        payload = response.json()
        expires_in = int(payload.get("expires_in") or 14400)
        payload["expires_at"] = int(time.time()) + max(60, expires_in)
        self._write_tokens(payload)
        self._oauth_state = None
        self._code_verifier = None
        return payload

    async def _refresh(self, tokens: dict) -> str:
        refresh_token = str(tokens.get("refresh_token") or "").strip()
        if not refresh_token:
            raise RuntimeError("Canva authorization has expired. Please reconnect Canva.")

        auth = httpx.BasicAuth(self.client_id, self.client_secret)
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }

        async with httpx.AsyncClient(timeout=60) as client:
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
            raise RuntimeError(f"Canva Connect token refresh failed: {detail}")

        payload = response.json()
        payload["expires_at"] = int(time.time()) + int(payload.get("expires_in") or 14400)
        self._write_tokens(payload)
        return str(payload["access_token"])

    async def access_token(self) -> str:
        self._require_credentials()
        tokens = self._read_tokens()
        token = str(tokens.get("access_token") or "").strip()
        expires_at = int(tokens.get("expires_at") or 0)

        if token and expires_at > int(time.time()) + 60:
            return token

        if tokens.get("refresh_token"):
            return await self._refresh(tokens)

        raise RuntimeError("Canva Connect is not authorized. Connect Canva before creating a design.")

    async def status(self) -> dict:
        tokens = self._read_tokens()
        expires_at = int(tokens.get("expires_at") or 0)
        return {
            "configured": bool(self.client_id and self.client_secret),
            "authenticated": bool(tokens.get("access_token") and (expires_at > int(time.time()) or tokens.get("refresh_token"))),
            "scopes": str(tokens.get("scope") or "").split(),
        }

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        token = await self.access_token()
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {token}"

        async with httpx.AsyncClient(timeout=120) as client:
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
            raise RuntimeError(f"Canva Connect API {response.status_code}: {detail}")
        return response.json()

    async def upload_asset(self, local_path: Path) -> str:
        path = Path(local_path)
        if not path.exists() or not path.is_file():
            raise RuntimeError("Generated image was not found on the server.")
        if path.stat().st_size >= 50 * 1024 * 1024:
            raise RuntimeError("Canva image uploads must be smaller than 50 MB.")

        name = path.name[:50]
        metadata = base64.b64encode(name.encode("utf-8")).decode("ascii")
        token = await self.access_token()

        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(
                f"{self.API_BASE}/asset-uploads",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/octet-stream",
                    "Asset-Upload-Metadata": json.dumps({"name_base64": metadata}),
                },
                content=path.read_bytes(),
            )

        if response.status_code >= 400:
            try:
                detail = response.json()
            except Exception:
                detail = response.text
            raise RuntimeError(f"Canva asset upload failed: {detail}")

        job = response.json().get("job") or {}
        job_id = str(job.get("id") or "")
        if not job_id:
            raise RuntimeError("Canva did not return an asset upload job ID.")

        for _ in range(60):
            result = await self._request("GET", f"/asset-uploads/{job_id}")
            current = result.get("job") or {}
            status = str(current.get("status") or "")
            if status == "success":
                asset_id = str((current.get("asset") or {}).get("id") or "")
                if asset_id:
                    return asset_id
                raise RuntimeError("Canva asset upload succeeded but returned no asset ID.")
            if status == "failed":
                error = current.get("error") or {}
                raise RuntimeError(str(error.get("message") or "Canva asset upload failed."))
            await self._sleep(1)

        raise RuntimeError("Timed out waiting for Canva to finish uploading the image.")

    async def create_design_from_asset(self, asset_id: str, local_path: Path, title: str) -> dict:
        from PIL import Image

        with Image.open(local_path) as image:
            width, height = image.size

        # Canva custom designs require 40-8000 px dimensions and max 25M pixels.
        width = max(40, min(int(width), 8000))
        height = max(40, min(int(height), 8000))
        if width * height > 25_000_000:
            scale = (25_000_000 / (width * height)) ** 0.5
            width = max(40, int(width * scale))
            height = max(40, int(height * scale))

        payload = {
            "type": "type_and_asset",
            "design_type": {
                "type": "custom",
                "width": width,
                "height": height,
            },
            "asset_id": asset_id,
            "title": title[:255] or "Generated Image",
        }
        return await self._request("POST", "/designs", json=payload)

    async def import_design_from_local_file(self, local_path: Path) -> dict:
        """Import a local structured design file into Canva.

        This uses Canva's Design Import API with a direct binary upload.
        It is intended for structured files such as PPTX/PPT/PDF and does
        not use Canva's image-to-design/Magic Layers endpoint.
        """
        path = Path(local_path)
        if not path.exists() or not path.is_file():
            raise RuntimeError(f"Editable design file was not found: {path}")

        suffix = path.suffix.lower()
        mime_types = {
            ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ".ppt": "application/vnd.ms-powerpoint",
            ".pdf": "application/pdf",
        }
        mime_type = mime_types.get(suffix)
        if not mime_type:
            raise RuntimeError(
                "Canva editable design import supports PPTX, PPT, and PDF files."
            )

        # Canva's Design Import API requires the title to be Base64 encoded
        # in the Import-Metadata header.
        title = path.stem[:50] or "Generated Editable Design"
        title_base64 = base64.b64encode(title.encode("utf-8")).decode("ascii")

        token = await self.access_token()

        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(
                f"{self.API_BASE}/imports",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/octet-stream",
                    "Import-Metadata": json.dumps(
                        {
                            "title_base64": title_base64,
                            "mime_type": mime_type,
                        }
                    ),
                },
                content=path.read_bytes(),
            )

        if response.status_code >= 400:
            try:
                detail = response.json()
            except Exception:
                detail = response.text
            raise RuntimeError(f"Canva design import failed: {detail}")

        payload = response.json()
        job = payload.get("job") or {}
        import_job_id = str(job.get("id") or "")
        if not import_job_id:
            raise RuntimeError(
                f"Canva design import did not return an import job ID: {payload}"
            )

        # Design imports are asynchronous. Poll until Canva returns success
        # or failed. The documented result is job.result.designs[].
        for _ in range(120):
            result = await self._request(
                "GET",
                f"/imports/{import_job_id}",
            )
            current_job = result.get("job") or {}
            status = str(current_job.get("status") or "").lower()

            if status == "success":
                designs = (
                    (current_job.get("result") or {}).get("designs") or []
                )
                if not designs:
                    raise RuntimeError(
                        "Canva import succeeded but returned no designs."
                    )

                design = designs[0] or {}
                design_id = str(design.get("id") or "")
                urls = design.get("urls") or {}
                edit_url = str(urls.get("edit_url") or "")
                view_url = str(urls.get("view_url") or "")

                if not design_id:
                    raise RuntimeError(
                        f"Canva import succeeded but returned no design ID: {result}"
                    )

                return {
                    "success": True,
                    "import_job_id": import_job_id,
                    "design_id": design_id,
                    "edit_url": edit_url,
                    "view_url": view_url,
                    "title": design.get("title") or title,
                    "page_count": design.get("page_count"),
                    "editable_source": "structured-design-import",
                    "magic_layers_required": False,
                    "job": current_job,
                }

            if status == "failed":
                error = current_job.get("error") or {}
                raise RuntimeError(
                    str(error.get("message") or "Canva design import failed.")
                )

            await self._sleep(1)

        raise RuntimeError(
            f"Timed out waiting for Canva to import '{path.name}'."
        )

    async def create_editable_design_from_local_image(
    self,
    local_path: Path,
) -> dict:
    """
    Upload the generated image to Canva and convert it into a
    Canva design with separately editable layers.
    """

    asset_id = await self.upload_asset(local_path)

    # Start Canva's Image-to-Design import job.
    result = await self._request(
        "POST",
        "/image-to-design-imports",
        json={
            "image": {
                "asset_id": asset_id,
            },
            "title": Path(local_path).stem[:255],
        },
    )

    job = result.get("job") or {}
    job_id = str(job.get("id") or "")

    if not job_id:
        raise RuntimeError(
            "Canva did not return an image-to-design import job ID."
        )

    # Poll until Canva finishes creating the editable design.
    for _ in range(90):
        result = await self._request(
            "GET",
            f"/image-to-design-imports/{job_id}",
        )

        job = result.get("job") or {}
        status = str(job.get("status") or "").lower()

        if status == "success":
            design = (
                (job.get("result") or {})
                .get("design")
                or {}
            )

            urls = design.get("urls") or {}

            edit_url = str(
                urls.get("edit_url") or ""
            )

            design_id = str(
                design.get("id") or ""
            )

            if not edit_url or not design_id:
                raise RuntimeError(
                    "Canva completed the import but did not return an edit URL."
                )

            return {
                "success": True,
                "design_id": design_id,
                "edit_url": edit_url,
                "view_url": str(
                    urls.get("view_url") or ""
                ),
                "asset_id": asset_id,
                "title": design.get("title")
                or Path(local_path).stem,
            }

        if status == "failed":
            error = job.get("error") or {}

            raise RuntimeError(
                str(
                    error.get("message")
                    or "Canva image-to-design import failed."
                )
            )

        await self._sleep(2)

    raise RuntimeError(
        "Timed out waiting for Canva to create the editable design."
    )
        asset_id = await self.upload_asset(local_path)
        result = await self.create_design_from_asset(
            asset_id,
            local_path,
            Path(local_path).stem,
        )
        design = result.get("design") or {}
        urls = design.get("urls") or {}
        return {
            "success": True,
            "design_id": str(design.get("id") or ""),
            "edit_url": str(urls.get("edit_url") or ""),
            "view_url": str(urls.get("view_url") or ""),
            "asset_id": asset_id,
            "title": design.get("title") or Path(local_path).stem,
        }

    async def export_design_png(self, design_id: str) -> bytes:
        payload = {
            "design_id": design_id,
            "format": {
                "type": "png",
            },
        }
        result = await self._request("POST", "/exports", json=payload)
        job = result.get("job") or {}
        export_id = str(job.get("id") or "")
        if not export_id:
            raise RuntimeError("Canva did not return an export job ID.")

        for _ in range(90):
            result = await self._request("GET", f"/exports/{export_id}")
            job = result.get("job") or {}
            status = str(job.get("status") or "")
            if status == "success":
                urls = job.get("urls") or []
                if not urls:
                    raise RuntimeError("Canva export succeeded but returned no download URL.")
                async with httpx.AsyncClient(timeout=180) as client:
                    response = await client.get(str(urls[0]))
                    response.raise_for_status()
                    return response.content
            if status == "failed":
                error = job.get("error") or {}
                raise RuntimeError(str(error.get("message") or "Canva export failed."))
            await self._sleep(2)

        raise RuntimeError("Timed out waiting for Canva to finish exporting the design.")

    @staticmethod
    async def _sleep(seconds: float) -> None:
        import asyncio
        await asyncio.sleep(seconds)


canva_connect_service = CanvaConnectService(Path(__file__).resolve().parent.parent)
