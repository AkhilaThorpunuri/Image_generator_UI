
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
    """Local-only Canva Connect OAuth + asset/design/export integration."""

    API_BASE = "https://api.canva.com/rest/v1"
    AUTHORIZE_URL = "https://www.canva.com/api/oauth/authorize"
    REDIRECT_URI = os.getenv(
        "CANVA_CONNECT_REDIRECT_URI",
        "https://agentic-content-generator-production.up.railway.app/api/canva/connect/oauth/callback",
    ).strip()

    # These are the Connect permissions required by this application:
    # read/write assets, create/read designs, and export the final design.
    SCOPES = [
        "asset:read",
        "asset:write",
        "design:content:write",
        "design:content:read",
        "design:meta:read",
    ]

    # Optional: ID of a Canva design that you created as the editable base
    # template for generated posters. When set, the application copies that
    # design and replaces its first editable image element with the newly
    # generated image through Canva MCP.
    #
    # IMPORTANT:
    # - This is a Canva DESIGN ID, not a public URL.
    # - The copied design must contain at least one editable image element.
    # - Text, shapes, and other elements already present in the template remain
    #   real Canva elements and can be edited manually or through MCP.
    EDITABLE_TEMPLATE_DESIGN_ID_ENV = "CANVA_EDITABLE_TEMPLATE_DESIGN_ID"

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self.token_file = self.base_dir / ".canva_connect_tokens.json"
        self._oauth_state: str | None = None
        self._code_verifier: str | None = None

    @property
    def client_id(self) -> str:
        return os.getenv("CANVA_CONNECT_CLIENT_ID", "").strip()

    @property
    def client_secret(self) -> str:
        return os.getenv("CANVA_CONNECT_CLIENT_SECRET", "").strip()

    @property
    def editable_template_design_id(self) -> str:
        return os.getenv(self.EDITABLE_TEMPLATE_DESIGN_ID_ENV, "").strip()

    def _require_credentials(self) -> None:
        if not self.client_id or not self.client_secret:
            raise RuntimeError(
                "CANVA_CONNECT_CLIENT_ID and CANVA_CONNECT_CLIENT_SECRET "
                "must be set in the backend environment."
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
                "redirect_uri": self.REDIRECT_URI,
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
            "redirect_uri": self.REDIRECT_URI,
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

    async def import_generated_editable_template(self, local_path: Path) -> dict:
        """Import an AI-generated editable PPTX template into Canva.

        This uses Canva's normal Design Import API. It does not upload a PNG/JPG
        for image-to-design conversion and therefore does not invoke Magic Layers.
        """
        path = Path(local_path)
        if path.suffix.lower() != ".pptx":
            raise RuntimeError("The generated editable Canva template must be a PPTX file.")
        result = await self.import_design_from_local_file(path)
        result["editable_source"] = "ai-generated-pptx-template"
        result["magic_layers_required"] = False
        return result

    async def import_design_from_local_file(self, local_path: Path) -> dict:
        """Import a local PDF/PPT/PPTX directly into Canva as a design.

        Canva's Design Import API accepts PDF and Microsoft PowerPoint files as
        binary uploads, so no public URL, Cloudflare tunnel, or ngrok is needed.
        """
        path = Path(local_path)
        if not path.exists() or not path.is_file():
            raise RuntimeError("The reference document was not found on the server.")

        suffix = path.suffix.lower()
        mime_types = {
            ".pdf": "application/pdf",
            ".ppt": "application/vnd.ms-powerpoint",
            ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        }
        mime_type = mime_types.get(suffix)
        if not mime_type:
            raise RuntimeError("Only PDF, PPT, and PPTX files can be imported into Canva.")

        title = path.stem[:50] or "Imported reference"
        title_base64 = base64.b64encode(title.encode("utf-8")).decode("ascii")
        token = await self.access_token()

        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(
                f"{self.API_BASE}/imports",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/octet-stream",
                    "Import-Metadata": json.dumps({
                        "title_base64": title_base64,
                        "mime_type": mime_type,
                    }),
                },
                content=path.read_bytes(),
            )

        if response.status_code >= 400:
            try:
                detail = response.json()
            except Exception:
                detail = response.text
            raise RuntimeError(f"Canva document import failed: {detail}")

        payload = response.json()
        job = payload.get("job") or {}
        job_id = str(job.get("id") or payload.get("id") or "").strip()
        if not job_id:
            raise RuntimeError(f"Canva did not return a design import job ID: {payload}")

        for _ in range(120):
            result = await self._request("GET", f"/imports/{job_id}")
            current = result.get("job") or result
            status = str(current.get("status") or "").lower()
            if status == "success":
                design = current.get("design") or result.get("design") or {}
                design_id = str(design.get("id") or "").strip()
                urls = design.get("urls") or {}
                edit_url = str(urls.get("edit_url") or "").strip()
                view_url = str(urls.get("view_url") or "").strip()
                if design_id and not edit_url:
                    edit_url = f"https://www.canva.com/design/{design_id}/edit"
                if not design_id:
                    raise RuntimeError(f"Canva import succeeded but returned no design ID: {result}")
                return {
                    "success": True,
                    "job_id": job_id,
                    "design_id": design_id,
                    "edit_url": edit_url,
                    "view_url": view_url,
                    "design": design,
                    "source_file": path.name,
                    "mime_type": mime_type,
                }
            if status == "failed":
                error = current.get("error") or {}
                raise RuntimeError(str(error.get("message") or current.get("message") or "Canva document import failed."))
            await self._sleep(2)

        raise RuntimeError("Timed out waiting for Canva to import the reference document.")

    async def create_design_copy(
        self,
        source_design_id: str,
        page_numbers: list[int] | None = None,
    ) -> dict:
        """Create a new Canva design by copying an existing editable design.

        Canva currently exposes this creation mode as a preview feature.
        The source design must be accessible by the authenticated Canva user.
        """
        source_design_id = str(source_design_id or "").strip()
        if not source_design_id:
            raise RuntimeError("A Canva editable template design ID is required.")

        payload = {
            "type": "design",
            "design_id": source_design_id,
        }
        if page_numbers:
            payload["page_numbers"] = [int(page) for page in page_numbers if int(page) > 0]

        result = await self._request("POST", "/designs", json=payload)
        design = result.get("design") or {}
        design_id = str(design.get("id") or "").strip()
        urls = design.get("urls") or {}
        edit_url = str(urls.get("edit_url") or "").strip()
        view_url = str(urls.get("view_url") or "").strip()

        if design_id and not edit_url:
            edit_url = f"https://www.canva.com/design/{design_id}/edit"
        if not design_id:
            raise RuntimeError(f"Canva copied the template but returned no design ID: {result}")

        return {
            "design_id": design_id,
            "edit_url": edit_url,
            "view_url": view_url,
            "design": design,
        }

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

    async def _attach_asset_to_template_with_mcp(
        self,
        design_id: str,
        asset_id: str,
    ) -> dict:
        """Replace the first editable image element in the copied template.

        Canva MCP is used only after the private asset has been uploaded through
        Canva Connect. No public URL is involved.
        """
        try:
            # Local import avoids a module-level circular import between the
            # Connect and MCP services.
            from app.services.canva_mcp_service import canva_mcp_service
        except Exception as exc:
            raise RuntimeError(
                "Canva MCP service could not be loaded. "
                "Make sure canva_mcp_service.py exports canva_mcp_service."
            ) from exc

        start_result = await canva_mcp_service.call_tool(
            "start-editing-transaction",
            {"design_id": design_id},
        )
        payload = canva_mcp_service._extract_payload(start_result)

        transaction_id = str(
            canva_mcp_service._find_value(
                payload,
                {"transaction_id", "transactionId"},
            )
            or ""
        ).strip()
        if not transaction_id:
            raise RuntimeError(
                "Canva MCP did not return an editing transaction ID for the copied template."
            )

        # Find an editable image fill. start-editing-transaction is the source
        # of truth for editable element IDs.
        fills = []

        def collect_fills(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if str(key).lower() == "fills" and isinstance(item, list):
                        fills.extend(item)
                    else:
                        collect_fills(item)
            elif isinstance(value, list):
                for item in value:
                    collect_fills(item)

        collect_fills(payload)

        target = None
        for fill in fills:
            if not isinstance(fill, dict):
                continue
            fill_type = str(fill.get("type") or "").lower()
            element_id = str(fill.get("element_id") or "").strip()
            editable = fill.get("editable", True)
            if fill_type == "image" and element_id and editable is not False:
                target = fill
                break

        if target is None:
            try:
                await canva_mcp_service.call_tool(
                    "cancel-editing-transaction",
                    {"transaction_id": transaction_id},
                )
            except Exception:
                pass
            raise RuntimeError(
                "The Canva editable template does not contain an editable image "
                "element. Add an image placeholder to the template and try again."
            )

        operation = {
            "type": "update_fill",
            "element_id": str(target["element_id"]),
            "asset_id": asset_id,
        }

        page_index = target.get("page_index")
        if page_index is not None:
            operation["page_index"] = page_index

        try:
            perform_result = await canva_mcp_service.call_tool(
                "perform-editing-operations",
                {
                    "transaction_id": transaction_id,
                    "operations": [operation],
                },
            )
            perform_payload = canva_mcp_service._extract_payload(perform_result)

            # Do not leave the template in draft mode. The image replacement is
            # part of the creation pipeline, so it is committed immediately.
            commit_result = await canva_mcp_service.call_tool(
                "commit-editing-transaction",
                {"transaction_id": transaction_id},
            )

            return {
                "transaction_id": transaction_id,
                "target_element_id": str(target["element_id"]),
                "perform_result": perform_payload,
                "commit_result": canva_mcp_service._extract_payload(commit_result),
            }
        except Exception:
            try:
                await canva_mcp_service.call_tool(
                    "cancel-editing-transaction",
                    {"transaction_id": transaction_id},
                )
            except Exception:
                pass
            raise

    async def create_editable_design_from_local_image(self, local_path: Path) -> dict:
        """Create a Canva design from a local generated image.

        Preferred mode:
          1. Upload local image bytes directly to Canva Connect.
          2. Copy CANVA_EDITABLE_TEMPLATE_DESIGN_ID.
          3. Use Canva MCP to replace the template's first editable image
             element with the uploaded generated image.
          4. Commit the change.
          5. Return the Canva edit URL.

        This produces a genuinely editable Canva design for the template's
        text/shapes/images. The generated PNG itself remains an image element;
        Canva's Connect API does not expose a general REST endpoint that
        decomposes arbitrary PNG pixels into native text/shape elements.

        Fallback mode (when no template ID is configured):
          - Upload the local image directly.
          - Create a custom Canva design containing that image as one raster
            element. It remains editable as an image, but text inside the PNG
            is not separate Canva text.
        """
        path = Path(local_path)
        if not path.exists() or not path.is_file():
            raise RuntimeError("Generated image was not found on the server.")

        asset_id = await self.upload_asset(path)

        template_id = self.editable_template_design_id
        if template_id:
            copied = await self.create_design_copy(
                template_id,
                page_numbers=[1],
            )
            design_id = str(copied.get("design_id") or "").strip()
            if not design_id:
                raise RuntimeError("Canva copied the editable template but returned no design ID.")

            try:
                mcp_sync = await self._attach_asset_to_template_with_mcp(
                    design_id=design_id,
                    asset_id=asset_id,
                )
            except Exception as exc:
                raise RuntimeError(
                    "The editable Canva template was copied, but the generated image "
                    f"could not be inserted into its image placeholder: {exc}"
                ) from exc

            return {
                "success": True,
                "design_id": design_id,
                "edit_url": str(copied.get("edit_url") or ""),
                "view_url": str(copied.get("view_url") or ""),
                "asset_id": asset_id,
                "template_design_id": template_id,
                "title": copied.get("design", {}).get("title") or path.stem,
                "editable_scope": "template-elements-plus-generated-image-element",
                "canva_ai_ready": True,
                "public_tunnel_required": False,
                "message": (
                    "An editable Canva design was created from your template. "
                    "The generated image was inserted into the template's editable "
                    "image element. Existing Canva text, shapes, and other template "
                    "elements remain independently editable, and Canva MCP AI edits "
                    "can be applied to those elements."
                ),
                "mcp_sync": mcp_sync,
            }

        # Backward-compatible raster fallback.
        result = await self.create_design_from_asset(
            asset_id,
            path,
            path.stem,
        )
        design = result.get("design") or {}
        urls = design.get("urls") or {}
        return {
            "success": True,
            "design_id": str(design.get("id") or ""),
            "edit_url": str(urls.get("edit_url") or ""),
            "view_url": str(urls.get("view_url") or ""),
            "asset_id": asset_id,
            "title": design.get("title") or path.stem,
            "editable_scope": "raster-image-element",
            "canva_ai_ready": True,
            "public_tunnel_required": False,
            "message": (
                "The generated image was uploaded directly to Canva. "
                "For fully editable text/shape elements, configure "
                "CANVA_EDITABLE_TEMPLATE_DESIGN_ID with a Canva template "
                "that contains editable placeholders."
            ),
        }

    def editable_design_mode(self) -> dict:
        """Return the currently configured Canva creation mode."""
        template_id = self.editable_template_design_id
        return {
            "mode": "editable-template" if template_id else "raster-image",
            "template_configured": bool(template_id),
            "template_design_id": template_id or None,
            "public_tunnel_required": False,
        }

    async def export_design(self, design_id: str, file_format: str = "png") -> bytes:
        """Export a Canva design as PNG, PDF, or PPTX."""
        normalized = str(file_format or "png").strip().lower()
        if normalized not in {"png", "pdf", "pptx"}:
            raise RuntimeError("Supported Canva export formats are PNG, PDF, and PPTX.")

        result = await self._request(
            "POST",
            "/exports",
            json={"design_id": design_id, "format": {"type": normalized}},
        )
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

    async def export_design_png(self, design_id: str) -> bytes:
        return await self.export_design(design_id, "png")

    @staticmethod
    async def _sleep(seconds: float) -> None:
        import asyncio
        await asyncio.sleep(seconds)


canva_connect_service = CanvaConnectService(Path(__file__).resolve().parent.parent)
