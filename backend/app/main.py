from pathlib import Path
import io
import json
import base64
import os
import re
import pickle
from urllib.parse import quote
from urllib.request import Request, urlopen
from app.services.canva_mcp_service import canva_mcp_service
from app.services.canva_connect_service import canva_connect_service

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    UploadFile,
    Body,
)

from fastapi.middleware.cors import (
    CORSMiddleware,
)
from pydantic import BaseModel

from fastapi.responses import (
    FileResponse,
    StreamingResponse,
    HTMLResponse,
)

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

from app.services.image_tagger import (
    generate_and_cache_tag,
    load_tag_metadata,
)

from app.services.template_builder import (
    generate_template,
)

from app.services.template_generator import (
    generate_template_from_url,
)

from app.services.prompt_generator import (
    generate_image_prompt,
)

from app.services.document_reference_service import (
    get_document_kind,
    is_supported_document,
    document_mime_type,
)
from app.services.editable_design_service import (
    build_editable_pptx,
)


# -------------------------------------------------------------------
# Application
# -------------------------------------------------------------------

app = FastAPI(
    title="Image Generator API",
    version="1.0.0",
)


# -------------------------------------------------------------------
# Directories
# -------------------------------------------------------------------

BASE_DIR = (
    Path(__file__)
    .resolve()
    .parent
    .parent
)

INPUT_DIR = (
    BASE_DIR /
    "input"
)

MANUAL_UPLOADS_DIR = (
    BASE_DIR /
    "manual_uploads"
)

UPLOADS_DIR = (
    BASE_DIR /
    "uploads"
)

TEMPLATES_DIR = (
    BASE_DIR /
    "templates"
)

EDITABLE_DESIGNS_DIR = (
    BASE_DIR /
    "output" /
    "editable_designs"
)

METADATA_FILE = (
    BASE_DIR /
    "input_metadata.json"
)


MANUAL_UPLOADS_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

UPLOADS_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

TEMPLATES_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

EDITABLE_DESIGNS_DIR.mkdir(
    parents=True,
    exist_ok=True,
)


# -------------------------------------------------------------------
# API key setup / in-memory session
# -------------------------------------------------------------------

DRIVE_OAUTH_KEY_ID = "__GOOGLE_DRIVE_OAUTH__"
DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
]
TOKEN_PICKLE_FILE = BASE_DIR / "token.pickle"
CREDENTIALS_FILE = BASE_DIR / "credentials.json"
TOKEN_FILE = BASE_DIR / "token.json"
DRIVE_CONFIG_FILE = BASE_DIR / "drive_config.json"

API_KEY_STATE = {
    "keys": {},
    "selected_ids": [],
    "pipeline_key_id": "",
    "config": {},
    "drive_folder_id": "",
    "drive_folder_name": "",
    "drive_folders": [],
    "drive_output_folders": [],
    "drive_output_folder_id": "",
    "drive_output_folder_name": "outputs",
    "gemini_model": "gemini-3.5-flash-lite",
}

CANVA_AI_TRANSACTIONS: dict[str, dict] = {}


def extract_api_key_icons(file_bytes: bytes, filename: str) -> dict[str, str]:
    """Return leading icon/symbol text for each API key in a text config file."""
    if Path(filename).suffix.lower() == ".json":
        return {}

    text = file_bytes.decode("utf-8-sig", errors="replace")
    icons: dict[str, str] = {}
    occurrences: dict[str, int] = {}

    for raw_line in text.splitlines():
        line = raw_line.strip().rstrip(",")
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        if "=" in line:
            left = line.split("=", 1)[0].strip()
        elif ":" in line:
            left = line.split(":", 1)[0].strip()
        else:
            continue

        left = left.strip("\"'").strip()
        match = re.search(r"([A-Za-z][A-Za-z0-9_.\-\s]*)\s*$", left)
        if not match:
            continue

        raw_name = match.group(1).strip()
        normalized = normalize_key_name(raw_name)
        base = re.sub(r"_\d+$", "", normalized)
        occurrence = occurrences.get(base, 0) + 1
        occurrences[base] = occurrence
        key_id = base if occurrence == 1 else f"{base}_{occurrence}"

        prefix = left[:match.start()].strip()
        prefix = prefix.strip("\"' -|:")
        if prefix:
            icons[key_id] = prefix

    return icons


def extract_drive_folder_configs(values: dict[str, str]) -> list[dict[str, str]]:
    """Extract one or more Google Drive reference/output folders from the uploaded config."""
    configs: list[dict[str, str]] = []

    def add(folder_id: str = "", folder_name: str = "") -> None:
        normalized = normalize_drive_folder_id(str(folder_id or ""))
        name = str(folder_name or "").strip()
        if not normalized and not name:
            return
        if any(c.get("id") == normalized and c.get("name") == name for c in configs):
            return
        configs.append({"id": normalized, "name": name})

    # Preferred compact form: GOOGLE_DRIVE_FOLDER_IDS=id1,id2,...
    for key in ("GOOGLE_DRIVE_FOLDER_IDS", "GDRIVE_FOLDER_IDS", "DRIVE_FOLDER_IDS"):
        raw = find_config_value(values, (key,))
        if raw:
            for value in re.split(r"[,;\n]+", str(raw)):
                add(folder_id=value.strip())

    # Numbered forms: GOOGLE_DRIVE_FOLDER_ID, _2, _3 ...
    id_items: list[tuple[int, str, str]] = []
    name_items: dict[int, str] = {}
    for key, value in values.items():
        normalized_key = normalize_key_name(key)
        id_match = re.match(r"^(?:GOOGLE|GDRIVE|DRIVE)_DRIVE?_?FOLDER_ID(?:_(\d+))?$", normalized_key)
        if not id_match:
            id_match = re.match(r"^(?:GOOGLE_DRIVE|GDRIVE|DRIVE)_FOLDER_ID(?:_(\d+))?$", normalized_key)
        if id_match:
            index = int(id_match.group(1) or "1")
            id_items.append((index, str(value or ""), ""))
            continue
        name_match = re.match(r"^(?:GOOGLE_DRIVE|GDRIVE|DRIVE)_FOLDER_NAME(?:_(\d+))?$", normalized_key)
        if name_match:
            name_items[int(name_match.group(1) or "1")] = str(value or "").strip()

    for index, folder_id, _ in sorted(id_items, key=lambda item: item[0]):
        add(folder_id=folder_id, folder_name=name_items.get(index, ""))

    # Backward-compatible single-folder aliases.
    if not configs:
        add(
            folder_id=find_config_value(values, (
                "GOOGLE_DRIVE_FOLDER_ID", "GDRIVE_FOLDER_ID", "DRIVE_FOLDER_ID",
                "GOOGLE_DRIVE_FOLDER_URL", "GDRIVE_FOLDER_URL", "DRIVE_FOLDER_URL",
            )),
            folder_name=find_config_value(values, (
                "GOOGLE_DRIVE_FOLDER_NAME", "GDRIVE_FOLDER_NAME", "GOOGLE_DRIVE_NAME",
                "GDRIVE_NAME", "DRIVE_FOLDER_NAME", "DRIVE_NAME",
            )),
        )

    return configs


def extract_drive_output_folder_configs(values: dict[str, str]) -> list[dict[str, str]]:
    """Extract dedicated Google Drive output folders from the uploaded config.

    Supported examples:
      GDRIVE_OUTPUT_FOLDER_ID_1=...
      GDRIVE_OUTPUT_FOLDER_ID_2=...
      GOOGLE_DRIVE_OUTPUT_FOLDER_ID_1=...
      GDRIVE_OUTPUT_FOLDER_IDS=id1,id2
    Optional matching names are supported with *_NAME_1, *_NAME_2, etc.
    """
    configs: list[dict[str, str]] = []

    def add(folder_id: str = "", folder_name: str = "") -> None:
        normalized = normalize_drive_folder_id(str(folder_id or ""))
        name = str(folder_name or "").strip()
        if not normalized and not name:
            return
        if any(c.get("id") == normalized for c in configs):
            return
        configs.append({"id": normalized, "name": name})

    # Compact form: GDRIVE_OUTPUT_FOLDER_IDS=id1,id2,...
    for key in (
        "GDRIVE_OUTPUT_FOLDER_IDS",
        "GOOGLE_DRIVE_OUTPUT_FOLDER_IDS",
        "DRIVE_OUTPUT_FOLDER_IDS",
    ):
        raw = find_config_value(values, (key,))
        if raw:
            for value in re.split(r"[,;\n]+", str(raw)):
                add(folder_id=value.strip())

    id_items: list[tuple[int, str]] = []
    name_items: dict[int, str] = {}
    for key, value in values.items():
        normalized_key = normalize_key_name(key)

        id_match = re.match(
            r"^(?:GOOGLE_DRIVE|GDRIVE|DRIVE)_OUTPUT_FOLDER_ID(?:_(\d+))?$",
            normalized_key,
        )
        if id_match:
            index = int(id_match.group(1) or "1")
            id_items.append((index, str(value or "")))
            continue

        name_match = re.match(
            r"^(?:GOOGLE_DRIVE|GDRIVE|DRIVE)_OUTPUT_FOLDER_NAME(?:_(\d+))?$",
            normalized_key,
        )
        if name_match:
            name_items[int(name_match.group(1) or "1")] = str(value or "").strip()

    for index, folder_id in sorted(id_items, key=lambda item: item[0]):
        add(folder_id=folder_id, folder_name=name_items.get(index, ""))

    return configs


def persist_drive_configuration() -> None:
    """
    Persist only non-secret Google Drive configuration.

    API keys and OAuth tokens are never written here. This file only keeps
    the Drive reference/output folder configuration so a FastAPI restart
    does not make the configured Drive references disappear.
    """
    folders = API_KEY_STATE.get("drive_folders", [])
    if not isinstance(folders, list):
        folders = []
    payload = {
        "drive_folder_id": normalize_drive_folder_id(
            API_KEY_STATE.get("drive_folder_id", "")
        ),
        "drive_folder_name": str(
            API_KEY_STATE.get("drive_folder_name", "")
        ).strip(),
        "drive_folders": folders,
        "drive_output_folders": API_KEY_STATE.get("drive_output_folders", []) if isinstance(API_KEY_STATE.get("drive_output_folders", []), list) else [],
        "drive_output_folder_id": str(
            API_KEY_STATE.get("drive_output_folder_id", "")
        ).strip(),
        "drive_output_folder_name": str(
            API_KEY_STATE.get("drive_output_folder_name", "outputs")
        ).strip() or "outputs",
    }

    try:
        DRIVE_CONFIG_FILE.write_text(
            json.dumps(payload, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        print(
            "Unable to persist Google Drive configuration:",
            repr(exc),
        )


def load_persisted_drive_configuration() -> None:
    """
    Restore the non-secret Drive folder configuration at backend startup.

    This is intentionally independent of the API-key selection state.
    Google Drive authentication continues to use credentials.json/token.json.
    """
    if not DRIVE_CONFIG_FILE.exists():
        return

    try:
        payload = json.loads(
            DRIVE_CONFIG_FILE.read_text(encoding="utf-8")
        )

        if not isinstance(payload, dict):
            return

        folder_id = normalize_drive_folder_id(
            str(payload.get("drive_folder_id", "") or "")
        )
        folder_name = str(
            payload.get("drive_folder_name", "") or ""
        ).strip()

        persisted_folders = payload.get("drive_folders", [])
        if isinstance(persisted_folders, list):
            API_KEY_STATE["drive_folders"] = [
                {
                    "id": normalize_drive_folder_id(str(item.get("id", "") or "")),
                    "name": str(item.get("name", "") or "").strip(),
                }
                for item in persisted_folders
                if isinstance(item, dict) and (item.get("id") or item.get("name"))
            ]

        persisted_output_folders = payload.get("drive_output_folders", [])
        if isinstance(persisted_output_folders, list):
            API_KEY_STATE["drive_output_folders"] = [
                {
                    "id": normalize_drive_folder_id(str(item.get("id", "") or "")),
                    "name": str(item.get("name", "") or "").strip(),
                }
                for item in persisted_output_folders
                if isinstance(item, dict) and (item.get("id") or item.get("name"))
            ]

        if folder_id or folder_name:
            API_KEY_STATE["drive_folder_id"] = folder_id
            API_KEY_STATE["drive_folder_name"] = folder_name
            if not API_KEY_STATE.get("drive_folders"):
                API_KEY_STATE["drive_folders"] = [{"id": folder_id, "name": folder_name}]

        if "drive_output_folder_id" in payload:
            API_KEY_STATE["drive_output_folder_id"] = str(
                payload.get("drive_output_folder_id", "") or ""
            ).strip()

        API_KEY_STATE["drive_output_folder_name"] = str(
            payload.get("drive_output_folder_name", "outputs") or "outputs"
        ).strip() or "outputs"

        print(
            "Restored Google Drive configuration:",
            folder_id or folder_name,
        )

    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        print(
            "Unable to restore Google Drive configuration:",
            repr(exc),
        )


class ApiKeySelection(BaseModel):
    selected_ids: list[str]


def normalize_key_name(name: str) -> str:
    return re.sub(
        r"[^A-Z0-9]+",
        "_",
        name.strip().upper(),
    ).strip("_")


def display_api_name(key_name: str) -> str:
    normalized = normalize_key_name(key_name)

    # Numeric suffixes identify separate credentials for the same provider.
    # They are added by the parser for repeated keys and are displayed by the
    # frontend as 1, 2, 3. Keep the backend provider name clean.
    normalized = re.sub(r"_\d+$", "", normalized)

    if "DRIVE" in normalized and "API" in normalized:
        return "Google Drive API"

    if "GEMINI" in normalized:
        return "Gemini API"

    if (
        "GOOGLE_AI" in normalized
        or "GOOGLEAI" in normalized
        or "GENERATIVE_AI" in normalized
    ):
        return "Google AI API"

    if "GOOGLE" in normalized and "API" in normalized:
        return "Google API"

    words = normalized.replace("_API_KEY", "").replace("_KEY", "").split("_")
    words = [word.title() for word in words if word]

    return " ".join(words) + " API" if words else "API"


def unique_key_name(values: dict[str, str], raw_name: str) -> str:
    base = normalize_key_name(raw_name)
    if base not in values:
        return base
    index = 2
    while f"{base}_{index}" in values:
        index += 1
    return f"{base}_{index}"


def logical_api_service(key_name: str) -> str:
    normalized = normalize_key_name(key_name)
    base = re.sub(r"_\d+$", "", normalized)
    if base in {"GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_AI_API_KEY", "GENERATIVE_AI_API_KEY"} or "GEMINI" in base:
        return "gemini"
    if "OPENROUTER" in base or "OPEN_ROUTER" in base:
        return "openrouter"
    if "OPENAI" in base:
        return "openai"
    if "CLAUDE" in base or "ANTHROPIC" in base:
        return "claude"
    return "other"


def service_can_generate_image(item: dict) -> bool:
    """Return the actual image-generation capability of one credential."""
    return _pipeline_key_is_usable(item, "image")


def parse_api_key_text(text: str) -> dict[str, str]:
    values: dict[str, str] = {}

    for raw_line in text.splitlines():
        line = raw_line.strip()

        if (
            not line
            or line.startswith("#")
            or line.startswith("//")
        ):
            continue

        line = line.rstrip(",")

        if line.startswith("{") or line.startswith("}"):
            continue

        match = re.match(
            r'^\s*[^A-Za-z0-9_.\-\s]*["\']?([A-Za-z0-9_.\-\s]+?)["\']?\s*(?:=|:)\s*(.*?)\s*$',
            line,
        )

        if not match:
            continue

        raw_name = match.group(1).strip()
        raw_value = match.group(2).strip()

        raw_value = raw_value.rstrip(",").strip()
        raw_value = raw_value.strip('"').strip("'").strip()

        if not raw_name or not raw_value:
            continue

        key_name = normalize_key_name(
            raw_name
        )

        # Keep repeated credentials instead of overwriting them.
        # For example, two identical OPENAI_API_KEY lines become
        # OPENAI_API_KEY and OPENAI_API_KEY_2. Three become _3, etc.
        # This lets the UI show them as OpenAI API 1, 2, 3.
        base_key_name = key_name
        occurrence = 1
        while key_name in values:
            occurrence += 1
            key_name = f"{base_key_name}_{occurrence}"

        values[key_name] = raw_value

    return values


def parse_api_key_file(
    file_bytes: bytes,
    filename: str,
) -> dict[str, str]:
    text = file_bytes.decode(
        "utf-8-sig",
        errors="replace",
    )

    if Path(filename).suffix.lower() == ".json":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "The JSON API key file is not valid JSON."
            ) from exc

        values: dict[str, str] = {}

        def collect(
            item,
            prefix: str = "",
        ):
            if isinstance(item, dict):
                for key, value in item.items():
                    next_prefix = (
                        f"{prefix}_{key}"
                        if prefix
                        else str(key)
                    )
                    collect(
                        value,
                        next_prefix,
                    )
            elif isinstance(item, (str, int, float)):
                if item != "":
                    key_name = unique_key_name(values, prefix)
                    values[key_name] = str(item)

        collect(payload)
        return values

    return parse_api_key_text(text)


def find_config_value(
    values: dict[str, str],
    patterns: tuple[str, ...],
) -> str:
    for key, value in values.items():
        normalized = normalize_key_name(key)

        if any(
            pattern in normalized
            for pattern in patterns
        ):
            return value

    return ""


def selected_key_value(
    category: str,
) -> str:
    selected = API_KEY_STATE.get("selected_ids", [])
    keys = API_KEY_STATE["keys"]

    if category != "gemini":
        return ""

    preferred_names = (
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_AI_API_KEY",
        "GENERATIVE_AI_API_KEY",
    )

    for key_name in preferred_names:
        if key_name not in selected:
            continue
        item = keys.get(key_name)
        if item and item.get("service") == "gemini":
            value = str(item.get("value", "")).strip()
            if value:
                return value

    for key_id in selected:
        item = keys.get(key_id)
        if item and item.get("service") == "gemini":
            value = str(item.get("value", "")).strip()
            if value:
                return value

    return ""


def _selected_pipeline_candidates() -> list[tuple[str, dict]]:
    """Return every selected non-OAuth credential with a usable value."""
    candidates = []
    for key_id in API_KEY_STATE.get("selected_ids", []):
        item = API_KEY_STATE.get("keys", {}).get(key_id)
        if not item or item.get("auth_type") == "oauth":
            continue
        if not str(item.get("value", "")).strip():
            continue
        candidates.append((key_id, item))
    return candidates


def _pipeline_key_is_usable(item: dict, stage: str = "image") -> bool:
    """Return whether a selected credential can perform the requested stage.

    The old implementation required every provider to support template + prompt +
    image generation. That made text/vision-only providers unusable even though
    they can correctly analyze references and create templates/prompts.
    """
    if not item or item.get("auth_type") == "oauth":
        return False
    if not str(item.get("value", "")).strip():
        return False
    service = str(item.get("service", "other")).strip().lower()
    if service == "google-drive":
        return False

    try:
        if stage in {"text", "template", "prompt", "tag"}:
            return bool(_provider_text_model(item))

        if stage == "image":
            # Built-in image providers have image adapters. Any other provider
            # is considered image-capable only when its API configuration
            # explicitly supplies an image model.
            if service in {"gemini", "openai", "openrouter"}:
                return bool(_provider_image_model(item))
            return bool(_provider_base_url(item) and _provider_image_model(item))

        return bool(_provider_text_model(item))
    except Exception:
        return False


def _stage_capability_message(stage: str, selected_ids: list[str]) -> str:
    """Build a provider-neutral capability error for the current selection."""
    names = ", ".join(
        str(API_KEY_STATE.get("keys", {}).get(k, {}).get("display_name", k))
        for k in selected_ids
        if k in API_KEY_STATE.get("keys", {})
    ) or "none"

    if stage == "image":
        return (
            "None of the selected API keys can generate the final image. "
            "Template and prompt generation can use text/vision-capable APIs, "
            "but final image generation requires an image-capable API. Select an "
            "image-capable API or configure <PREFIX>_BASE_URL and "
            "<PREFIX>_IMAGE_MODEL for a custom image provider. "
            f"Selected: {names}."
        )

    return (
        f"None of the selected API keys can perform {stage} generation. "
        "Select an API key with text/vision capability or configure its "
        "provider-specific model settings. "
        f"Selected: {names}."
    )


def _require_pipeline_key(stage: str = "image") -> tuple[str, dict]:
    """Return the first selected credential capable of the requested stage."""
    selected_ids = API_KEY_STATE.get("selected_ids", [])
    if not selected_ids:
        raise HTTPException(
            status_code=400,
            detail="No API key is selected. Select at least one API key before using the AI pipeline.",
        )

    # Keep the previously chosen key when it still supports this stage.
    pipeline_id = str(API_KEY_STATE.get("pipeline_key_id", "")).strip()
    if pipeline_id:
        item = API_KEY_STATE.get("keys", {}).get(pipeline_id)
        if pipeline_id in selected_ids and _pipeline_key_is_usable(item, stage):
            return pipeline_id, item

    for key_id in selected_ids:
        item = API_KEY_STATE.get("keys", {}).get(key_id)
        if _pipeline_key_is_usable(item, stage):
            # The pipeline key is only a convenience cache. It may change between
            # stages: Claude can be used for text/vision while Gemini/OpenAI is used
            # for image generation.
            if stage == "image":
                API_KEY_STATE["pipeline_key_id"] = key_id
            return key_id, item

    raise HTTPException(
        status_code=400,
        detail=_stage_capability_message(stage, selected_ids),
    )

def _key_prefix(key_name: str) -> str:
    base = re.sub(r"_\d+$", "", normalize_key_name(key_name))
    for suffix in ("_API_KEY", "_KEY", "_TOKEN"):
        if base.endswith(suffix):
            return base[:-len(suffix)].strip("_")
    return base


def _key_config(item: dict) -> dict:
    values = API_KEY_STATE.get("config", {}) or {}
    prefix = _key_prefix(str(item.get("key_name", "")))
    def pick(*names: str) -> str:
        for name in names:
            value = values.get(normalize_key_name(name))
            if value not in (None, ""):
                return str(value).strip()
        return ""
    # Provider-specific model settings always take precedence.
    # Do NOT let a generic MODEL/TEXT_MODEL value intended for another
    # provider override the selected provider (for example OpenRouter).
    provider_text_model = pick(
        f"{prefix}_TEXT_MODEL",
        f"{prefix}_MODEL",
    )
    provider_image_model = pick(
        f"{prefix}_IMAGE_MODEL",
        f"{prefix}_MODEL_IMAGE",
    )
    generic_text_model = pick("AI_TEXT_MODEL", "TEXT_MODEL")
    generic_image_model = pick("AI_IMAGE_MODEL", "IMAGE_MODEL")

    return {
        "base_url": pick(
            f"{prefix}_BASE_URL",
            f"{prefix}_API_BASE_URL",
            f"{prefix}_ENDPOINT",
            "AI_BASE_URL",
            "API_BASE_URL",
            "BASE_URL",
        ).rstrip("/"),
        "text_model": provider_text_model or generic_text_model,
        "image_model": provider_image_model or generic_image_model,
    }


def _provider_base_url(item: dict) -> str:
    service = str(item.get("service", "other"))
    if service == "openai": return "https://api.openai.com/v1"
    if service == "openrouter": return OPENROUTER_BASE_URL
    if service == "claude": return "https://api.anthropic.com/v1"
    return _key_config(item).get("base_url", "")


def _provider_text_model(item: dict) -> str:
    service = str(item.get("service", "other"))
    config = _key_config(item)
    if service == "openrouter":
        # OpenRouter must never inherit a generic MODEL value belonging to
        # another provider. Use OPENROUTER_TEXT_MODEL / OPENROUTER_MODEL
        # when explicitly configured, otherwise use the built-in valid model.
        openrouter_text = _key_config(item).get("text_model")
        if openrouter_text:
            return openrouter_text
        return OPENROUTER_TEXT_MODEL
    if config.get("text_model"): return config["text_model"]
    if service == "gemini": return str(API_KEY_STATE.get("gemini_model") or "gemini-3.5-flash-lite").strip()
    if service == "openai": return "gpt-5-mini"
    if service == "claude": return "claude-sonnet-5"
    return ""


def _provider_image_model(item: dict) -> str:
    service = str(item.get("service", "other"))
    config = _key_config(item)
    if service == "openrouter":
        openrouter_image = _key_config(item).get("image_model")
        if openrouter_image:
            return openrouter_image
        return OPENROUTER_IMAGE_MODEL
    if config.get("image_model"): return config["image_model"]
    if service == "gemini": return "gemini-3.1-flash-image"
    if service == "openai": return "gpt-image-2"
    # Claude is a vision/text provider here; its API is not an image generator.
    if service == "claude": return ""
    return ""


def _generic_compatible_error(item: dict) -> RuntimeError:
    prefix = _key_prefix(str(item.get("key_name", "API_KEY")))
    return RuntimeError(f"{item.get('display_name', 'Selected API')} is accepted, but its API protocol could not be determined. For a provider without a built-in adapter, add {prefix}_BASE_URL and {prefix}_MODEL to the uploaded API configuration. Add {prefix}_IMAGE_MODEL if its image model differs from its text model.")


def _with_pipeline_key(item: dict, operation):
    service = item.get("service")
    api_key = str(item.get("value", "")).strip()
    if not api_key: raise RuntimeError("The selected pipeline API key is empty.")
    previous = {name: os.environ.get(name) for name in ("GEMINI_API_KEY","GOOGLE_API_KEY","OPENAI_API_KEY","OPENROUTER_API_KEY","GEMINI_MODEL")}
    try:
        for name in previous: os.environ.pop(name, None)
        if service == "gemini":
            os.environ["GEMINI_API_KEY"] = api_key
            os.environ["GEMINI_MODEL"] = _provider_text_model(item)
        elif service == "openrouter": os.environ["OPENROUTER_API_KEY"] = api_key
        elif service == "openai": os.environ["OPENAI_API_KEY"] = api_key
        return operation()
    finally:
        for name, value in previous.items():
            if value is None: os.environ.pop(name, None)
            else: os.environ[name] = value


# -------------------------------------------------------------------
# Provider adapters
# -------------------------------------------------------------------

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_TEXT_MODEL = os.getenv("OPENROUTER_TEXT_MODEL", "google/gemini-3.1-flash-lite").strip()
OPENROUTER_IMAGE_MODEL = os.getenv("OPENROUTER_IMAGE_MODEL", "google/gemini-3.1-flash-image").strip()


def _openrouter_request(api_key: str, endpoint: str, payload: dict, timeout: int = 180) -> dict:
    body=json.dumps(payload).encode("utf-8")
    request=Request(f"{OPENROUTER_BASE_URL}/{endpoint.lstrip('/')}",data=body,method="POST",headers={"Authorization":f"Bearer {api_key}","Content-Type":"application/json","HTTP-Referer":"http://localhost:5173","X-Title":"Objectives Image Generator"})
    try:
        with urlopen(request,timeout=timeout) as response: return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        detail=str(exc)
        if hasattr(exc,"read"):
            try: detail=exc.read().decode("utf-8",errors="replace")
            except Exception: pass
        raise RuntimeError(f"OpenRouter request failed: {detail}") from exc


def _image_data_url(path: Path) -> str:
    mime=get_mime_type(path)
    if mime == "image/gif":
        from PIL import Image
        with Image.open(path) as image:
            image.seek(0); frame=image.convert("RGB"); buffer=io.BytesIO(); frame.save(buffer,format="PNG"); data=buffer.getvalue()
        mime="image/png"
    else: data=path.read_bytes()
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _chat_response_text(result: dict, provider_name: str) -> str:
    choices=result.get("choices") or []
    if not choices: raise RuntimeError(f"{provider_name} returned no text completion.")
    content=(choices[0].get("message") or {}).get("content", "")
    if isinstance(content,list): content="".join(str(x.get("text", "")) for x in content if isinstance(x,dict))
    content=str(content or "").strip()
    if not content: raise RuntimeError(f"{provider_name} returned an empty text response.")
    return content


def _openrouter_chat_with_image(api_key: str, path: Path, instruction: str, model: str | None = None) -> str:
    selected_model = str(model or OPENROUTER_TEXT_MODEL).strip()
    if not selected_model:
        raise RuntimeError("No OpenRouter text/vision model is configured for the selected API key.")
    payload={"model":selected_model,"messages":[{"role":"user","content":[{"type":"text","text":instruction},{"type":"image_url","image_url":{"url":_image_data_url(path)}}]}],"temperature":0.2}
    return _chat_response_text(_openrouter_request(api_key,"chat/completions",payload),"OpenRouter")


def _extract_json_object(text: str) -> dict:
    cleaned=text.strip()
    if cleaned.startswith("```"):
        cleaned=re.sub(r"^```(?:json)?\s*","",cleaned,flags=re.I); cleaned=re.sub(r"\s*```$","",cleaned)
    try:
        value=json.loads(cleaned)
        if isinstance(value,dict): return value
    except json.JSONDecodeError: pass
    match=re.search(r"\{.*\}",cleaned,flags=re.S)
    if not match: raise RuntimeError("The selected API did not return valid JSON for template generation.")
    try: value=json.loads(match.group(0))
    except json.JSONDecodeError as exc: raise RuntimeError("The selected API returned malformed template JSON.") from exc
    if not isinstance(value,dict): raise RuntimeError("Template response was not a JSON object.")
    return value


def _template_from_text(item: dict, raw: str, reference_path: Path) -> dict:
    from PIL import Image
    with Image.open(reference_path) as image: width,height=image.size
    parsed=_extract_json_object(raw); elements=parsed.get("text_elements")
    if not isinstance(elements,list): raise RuntimeError("The selected API did not return text_elements for template generation.")
    normalized=[]
    for i,value in enumerate(elements[:100]):
        if not isinstance(value,dict): continue
        normalized.append({"id":str(value.get("id") or f"text_{i+1}"),"text":str(value.get("text") or ""),"x":float(value.get("x") or 0),"y":float(value.get("y") or 0),"width":float(value.get("width") or width),"height":float(value.get("height") or 50),"font_size":float(value.get("font_size") or 32),"font_weight":str(value.get("font_weight") or "normal"),"alignment":str(value.get("alignment") or "left"),"color":str(value.get("color") or "#FFFFFF"),"role":str(value.get("role") or "content")})
    template_id=__import__("uuid").uuid4().hex
    template={"template_id":template_id,"version":"4.0","name":"Generated Reference Template","canvas":{"width":width,"height":height,"orientation":"landscape" if width>=height else "portrait","aspect_ratio":round(width/height,4) if height else 1},"layout":{"type":"reference-based","alignment":"reference","preserve_reference_structure":True,"preserve_reference_composition":True,"editable_text_only":True},"regions":[],"text_elements":normalized,"text_groups":[],"style":{"keywords":["reference-preserved","professional","editable-text"],"preserve_reference_colors":True},"content":{"keywords":["editable-text","content-replacement"],"source_prompt":""},"reference_analysis":{"canvas":{"width":width,"height":height}}}
    filename=f"{template_id}.json"; (TEMPLATES_DIR/filename).write_text(json.dumps(template,indent=2,ensure_ascii=False),encoding="utf-8")
    return {"template_id":template_id,"template_name":template["name"],"reference":template["reference_analysis"],"prompt":{"original_prompt":"","layout":{"type":"reference-based","alignment":"reference"},"style_keywords":template["style"]["keywords"],"content_keywords":template["content"]["keywords"]},"template":template,"template_file":str(Path("templates")/filename)}


def _openrouter_template(api_key: str, reference_path: Path, model: str | None = None) -> dict:
    from PIL import Image
    with Image.open(reference_path) as image: width,height=image.size
    instruction=f"""Analyze this reference poster exactly as a design-template extraction task. Do not redesign it. Identify editable text regions and approximate bounding boxes in pixel coordinates using the reference canvas {width}x{height}. Preserve composition, visual hierarchy, colors, decorative elements, images, logos and spacing. Return ONLY valid JSON: {{"text_elements":[{{"id":"text_1","text":"exact visible text","x":0,"y":0,"width":100,"height":50,"font_size":32,"font_weight":"normal","alignment":"left","color":"#FFFFFF","role":"title"}}]}}"""
    return _template_from_text(None,_openrouter_chat_with_image(api_key,reference_path,instruction,model or OPENROUTER_TEXT_MODEL),reference_path)


def _openrouter_prompt(api_key: str, reference_path: Path, model: str | None = None) -> str:
    instruction="""Analyze the supplied reference design and create a concise production-ready content prompt for replacing its text while preserving the reference layout. Describe subject/content, important text regions, hierarchy and visual intent. Do not redesign it or invent factual details. Return only the prompt text."""
    return _openrouter_chat_with_image(api_key,reference_path,instruction,model)



def _claude_request(api_key: str, payload: dict, timeout: int = 180) -> dict:
    """Call Anthropic's Messages API without exposing the credential to the client."""
    request = Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        detail = str(exc)
        if hasattr(exc, "read"):
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
        raise RuntimeError(f"Claude request failed: {detail}") from exc


def _claude_chat_with_image(item: dict, path: Path, instruction: str) -> str:
    """Send a reference image + instruction to Claude's vision-capable Messages API."""
    image_path = Path(path)
    mime = get_mime_type(image_path)
    if mime == "image/gif":
        from PIL import Image
        with Image.open(image_path) as image:
            image.seek(0)
            frame = image.convert("RGB")
            buffer = io.BytesIO()
            frame.save(buffer, format="PNG")
            image_bytes = buffer.getvalue()
        mime = "image/png"
    else:
        image_bytes = image_path.read_bytes()

    payload = {
        "model": _provider_text_model(item),
        "max_tokens": 4096,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64",
                        "media_type": mime,
                        "data": base64.b64encode(image_bytes).decode("ascii"),
                    }},
                    {"type": "text", "text": instruction},
                ],
            }
        ],
    }
    result = _claude_request(str(item.get("value", "")).strip(), payload)
    content = result.get("content") or []
    text = "".join(
        str(part.get("text", ""))
        for part in content
        if isinstance(part, dict) and part.get("type") == "text"
    ).strip()
    if not text:
        raise RuntimeError("Claude returned an empty response.")
    return text

def _generic_request(item: dict, endpoint: str, payload: dict, timeout: int = 180) -> dict:
    base_url=_provider_base_url(item); model=_provider_text_model(item)
    if not base_url or not model: raise _generic_compatible_error(item)
    request=Request(f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}",data=json.dumps(payload).encode("utf-8"),method="POST",headers={"Authorization":f"Bearer {str(item.get('value','')).strip()}","Content-Type":"application/json"})
    try:
        with urlopen(request,timeout=timeout) as response: return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        detail=str(exc)
        if hasattr(exc,"read"):
            try: detail=exc.read().decode("utf-8",errors="replace")
            except Exception: pass
        raise RuntimeError(f"{item.get('display_name','API')} request failed: {detail}") from exc


def _generic_chat_with_image(item: dict, path: Path, instruction: str) -> str:
    payload={"model":_provider_text_model(item),"messages":[{"role":"user","content":[{"type":"text","text":instruction},{"type":"image_url","image_url":{"url":_image_data_url(path)}}]}],"temperature":0.2}
    return _chat_response_text(_generic_request(item,"chat/completions",payload),item.get("display_name","API"))


def _generic_template(item: dict, reference_path: Path) -> dict:
    from PIL import Image
    with Image.open(reference_path) as image: width,height=image.size
    instruction=f"""Analyze this reference poster as a design-template extraction task. Do not redesign it. Identify editable text regions and approximate bounding boxes in pixel coordinates for canvas {width}x{height}. Preserve composition, colors, decorative elements, logos and hierarchy. Return ONLY JSON: {{"text_elements":[{{"id":"text_1","text":"exact visible text","x":0,"y":0,"width":100,"height":50,"font_size":32,"font_weight":"normal","alignment":"left","color":"#FFFFFF","role":"title"}}]}}"""
    return _template_from_text(item,_generic_chat_with_image(item,reference_path,instruction),reference_path)


def _generic_prompt(item: dict, reference_path: Path) -> str:
    return _generic_chat_with_image(item,reference_path,"""Analyze the supplied reference design and create a concise production-ready content prompt for replacing its text while preserving the reference layout. Describe subject/content, important text regions, hierarchy and visual intent. Do not redesign it or invent factual details. Return only the prompt text.""")


def _pipeline_metadata(pipeline_item: dict, operation: str) -> dict:
    """Return the exact selected provider identity and model for an AI operation."""
    service = str(pipeline_item.get("service") or "").strip().lower()
    provider = str(pipeline_item.get("display_name") or display_api_name(service) or service or "Selected API").strip()
    model = _provider_image_model(pipeline_item) if operation == "image" else _provider_text_model(pipeline_item)
    if not model:
        model = "provider-default"
    return {
        "provider": provider,
        "model": model,
        "api_id": str(pipeline_item.get("id") or ""),
        "service": service,
    }


def _pipeline_prompt(pipeline_item: dict, **kwargs):
    path=kwargs.get("image_path")
    if path is None: raise RuntimeError("AI prompt generation requires a local reference image.")
    service=pipeline_item.get("service")
    if service=="openrouter": return _openrouter_prompt(str(pipeline_item["value"]).strip(),Path(path),_provider_text_model(pipeline_item))
    if service=="gemini": return _with_pipeline_key(pipeline_item,lambda:generate_image_prompt(**kwargs))
    if service=="claude": return _claude_chat_with_image(pipeline_item,Path(path),"""Analyze the supplied reference design and create a concise production-ready content prompt for replacing its text while preserving the reference layout. Describe subject/content, important text regions, hierarchy and visual intent. Do not redesign it or invent factual details. Return only the prompt text.""")
    return _generic_prompt(pipeline_item,Path(path))


def _pipeline_template(pipeline_item: dict, **kwargs):
    path=kwargs.get("reference_path")
    if path is None: raise RuntimeError("Template generation requires a local reference image.")
    service=pipeline_item.get("service")
    if service=="openrouter": return _openrouter_template(str(pipeline_item["value"]).strip(),Path(path),_provider_text_model(pipeline_item))
    if service=="gemini": return _with_pipeline_key(pipeline_item,lambda:generate_template(**kwargs))
    if service=="claude":
        from PIL import Image
        with Image.open(Path(path)) as image: width,height=image.size
        instruction=f"""Analyze this reference poster as a design-template extraction task. Do not redesign it. Identify editable text regions and approximate bounding boxes in pixel coordinates for canvas {width}x{height}. Preserve composition, colors, decorative elements, logos and hierarchy. Return ONLY JSON: {{\"text_elements\":[{{\"id\":\"text_1\",\"text\":\"exact visible text\",\"x\":0,\"y\":0,\"width\":100,\"height\":50,\"font_size\":32,\"font_weight\":\"normal\",\"alignment\":\"left\",\"color\":\"#FFFFFF\",\"role\":\"title\"}}]}}"""
        return _template_from_text(pipeline_item,_claude_chat_with_image(pipeline_item,Path(path),instruction),Path(path))
    return _generic_template(pipeline_item,Path(path))


def _selected_stage_candidates(stage: str) -> list[tuple[str, dict]]:
    """Return selected credentials capable of one specific pipeline stage."""
    candidates: list[tuple[str, dict]] = []
    for key_id, item in _selected_pipeline_candidates():
        if _pipeline_key_is_usable(item, stage):
            candidates.append((key_id, item))
    return candidates


def _exception_is_quota_error(exc: Exception) -> bool:
    text = str(exc).upper()
    return any(token in text for token in (
        "429", "RESOURCE_EXHAUSTED", "QUOTA", "RATE LIMIT", "RATE_LIMIT",
    ))


def _normalize_ai_tag(raw: str) -> str:
    """Return one short semantic tag from a model response."""
    text = str(raw or "").strip()
    if not text:
        raise RuntimeError("The selected API returned an empty image tag.")
    # Accept JSON responses from providers that follow the requested schema.
    try:
        parsed = _extract_json_object(text)
        if isinstance(parsed, dict) and parsed.get("tag"):
            text = str(parsed["tag"]).strip()
    except Exception:
        pass
    text = re.sub(r"^['\"`]+|['\"`]+$", "", text).strip()
    text = re.sub(r"^(?:tag|semantic tag)\s*[:=-]\s*", "", text, flags=re.I).strip()
    text = re.sub(r"\s+", " ", text)
    words = text.split()
    if len(words) > 5:
        text = " ".join(words[:5])
    if len(words) < 1:
        raise RuntimeError("The selected API did not return a usable image tag.")
    return text


def _pipeline_tag(pipeline_item: dict, path: Path) -> str:
    """Generate an image tag with the SAME selected pipeline credential."""
    service = str(pipeline_item.get("service", "other"))
    instruction = (
        "Look at the supplied image and produce exactly ONE semantic tag "
        "describing the main subject/design. Use 3 to 5 words, no hashtags, "
        "no punctuation, and no explanation. Return only the tag text."
    )
    if service == "openrouter":
        raw = _openrouter_chat_with_image(
            str(pipeline_item["value"]).strip(),
            Path(path),
            instruction,
        )
        return _normalize_ai_tag(raw)
    if service == "gemini":
        raw = _with_pipeline_key(
            pipeline_item,
            lambda: generate_and_cache_tag(Path(path), METADATA_FILE),
        )
        # Gemini's existing tagger returns the project's canonical cached tag.
        return _normalize_ai_tag(raw)
    if service == "claude":
        raw = _claude_chat_with_image(pipeline_item, Path(path), instruction)
        return _normalize_ai_tag(raw)
    raw = _generic_chat_with_image(
        pipeline_item,
        Path(path),
        instruction,
    )
    return _normalize_ai_tag(raw)


def configure_selected_environment():
    # Credentials are held only in the running backend process.
    os.environ.pop("GEMINI_API_KEY", None)
    os.environ.pop("GOOGLE_API_KEY", None)
    os.environ.pop("GEMINI_MODEL", None)

    gemini_key = selected_key_value("gemini")

    # Do not mirror the Gemini key into GOOGLE_API_KEY. The google-genai
    # client otherwise reports that both credentials are configured and may
    # choose GOOGLE_API_KEY unexpectedly.
    if gemini_key:
        os.environ["GEMINI_API_KEY"] = gemini_key

    model = str(
        API_KEY_STATE.get("gemini_model")
        or "gemini-3.5-flash-lite"
    ).strip()
    os.environ["GEMINI_MODEL"] = model


def normalize_drive_folder_id(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""

    match = re.search(
        r"/folders/([A-Za-z0-9_-]+)",
        value,
    )
    if match:
        return match.group(1)

    return value


def drive_oauth_available() -> bool:
    return CREDENTIALS_FILE.exists()


def drive_oauth_selected() -> bool:
    """
    Google Drive is available when its OAuth files and folder configuration
    are present. It does not depend on the frontend selecting a synthetic
    "Google Drive API" checkbox.
    """
    return (
        CREDENTIALS_FILE.exists()
        and TOKEN_FILE.exists()
        and bool(
            normalize_drive_folder_id(
                API_KEY_STATE.get("drive_folder_id", "")
            )
            or API_KEY_STATE.get("drive_folder_name", "")
        )
    )


def get_drive_service():
    """Return an authenticated Google Drive v3 service.

    The project historically used both token.pickle and token.json. Prefer the
    working pickle credential first because it preserves the credential object
    and scopes that were successfully used by the existing application. Fall
    back to token.json for installations that only have the JSON token.
    """
    if not CREDENTIALS_FILE.exists():
        raise RuntimeError(
            "Google Drive OAuth credentials.json was not found in the backend folder."
        )

    # 1. Prefer the existing pickle credential. This is the credential format
    # already proven to work with this application.
    if TOKEN_PICKLE_FILE.exists():
        try:
            with TOKEN_PICKLE_FILE.open("rb") as token_handle:
                pickle_credentials = pickle.load(token_handle)

            if pickle_credentials and getattr(pickle_credentials, "valid", False):
                return build("drive", "v3", credentials=pickle_credentials)

            if (
                pickle_credentials
                and getattr(pickle_credentials, "expired", False)
                and getattr(pickle_credentials, "refresh_token", None)
            ):
                try:
                    pickle_credentials.refresh(GoogleAuthRequest())
                    with TOKEN_PICKLE_FILE.open("wb") as token_handle:
                        pickle.dump(pickle_credentials, token_handle)
                    # Keep token.json synchronized when possible, but do not
                    # make a working pickle credential depend on token.json.
                    try:
                        TOKEN_FILE.write_text(
                            pickle_credentials.to_json(),
                            encoding="utf-8",
                        )
                    except OSError:
                        pass
                    return build("drive", "v3", credentials=pickle_credentials)
                except Exception as pickle_refresh_error:
                    print(
                        "Google Drive token.pickle refresh failed; trying token.json:",
                        repr(pickle_refresh_error),
                    )
        except Exception as pickle_error:
            print(
                "Google Drive token.pickle could not be loaded; trying token.json:",
                repr(pickle_error),
            )

    # 2. Fall back to the JSON credential used by newer OAuth flows.
    credentials = None
    if TOKEN_FILE.exists():
        try:
            credentials = Credentials.from_authorized_user_file(
                str(TOKEN_FILE),
                DRIVE_SCOPES,
            )
        except Exception as json_load_error:
            print(
                "Google Drive token.json could not be loaded:",
                repr(json_load_error),
            )
            credentials = None

    if credentials and credentials.valid:
        return build("drive", "v3", credentials=credentials)

    if credentials and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(GoogleAuthRequest())
            TOKEN_FILE.write_text(
                credentials.to_json(),
                encoding="utf-8",
            )
            # Keep the legacy credential store synchronized too.
            try:
                with TOKEN_PICKLE_FILE.open("wb") as token_handle:
                    pickle.dump(credentials, token_handle)
            except OSError:
                pass
            return build(
                "drive",
                "v3",
                credentials=credentials,
            )
        except Exception as exc:
            raise RuntimeError(
                "Google Drive authorization could not be refreshed from token.json. "
                "The existing token.pickle is also unavailable or could not be refreshed. "
                "Run 'python test_google_drive.py' once from the backend folder if both "
                "credential stores are no longer valid."
            ) from exc

    raise RuntimeError(
        "Google Drive is not authorized yet. Run 'python test_google_drive.py' "
        "once from the backend folder, then restart the backend."
    )

def require_drive_configuration():
    """
    Validate Google Drive configuration.

    The uploaded configuration file only needs the Drive folder ID/name.
    OAuth authentication comes from credentials.json and token.json in the
    backend folder.
    """
    folder_id = normalize_drive_folder_id(
        API_KEY_STATE.get("drive_folder_id", "")
    )
    folder_name = str(
        API_KEY_STATE.get("drive_folder_name", "")
    ).strip()

    # Recover persisted configuration on demand as an additional safeguard.
    if not folder_id and not folder_name:
        load_persisted_drive_configuration()
        folder_id = normalize_drive_folder_id(
            API_KEY_STATE.get("drive_folder_id", "")
        )
        folder_name = str(
            API_KEY_STATE.get("drive_folder_name", "")
        ).strip()

    if not folder_id and not folder_name:
        raise HTTPException(
            status_code=400,
            detail=(
                "No Google Drive folder was configured. Add "
                "GOOGLE_DRIVE_FOLDER_ID to the uploaded API file."
            ),
        )

    if not CREDENTIALS_FILE.exists():
        raise HTTPException(
            status_code=500,
            detail=(
                "Google Drive credentials.json was not found in the backend folder."
            ),
        )

    if not TOKEN_FILE.exists():
        raise HTTPException(
            status_code=500,
            detail=(
                "Google Drive token.json was not found in the backend folder."
            ),
        )

    return folder_id, folder_name


def _drive_folder_metadata(service, folder_id: str) -> dict:
    """Validate that the current OAuth account can access a live Drive folder."""
    folder_id = normalize_drive_folder_id(folder_id)
    if not folder_id:
        raise RuntimeError("Google Drive folder ID is empty.")

    metadata = (
        service.files()
        .get(
            fileId=folder_id,
            fields="id,name,mimeType,trashed,parents",
        )
        .execute()
    )

    if metadata.get("trashed"):
        raise RuntimeError(f'Google Drive folder "{folder_id}" is in the trash.')

    if metadata.get("mimeType") != "application/vnd.google-apps.folder":
        raise RuntimeError(f'Google Drive ID "{folder_id}" is not a folder.')

    return metadata


def _find_drive_folder_by_name(service, folder_name: str) -> dict:
    """Find an exact-name accessible folder for the current OAuth account."""
    folder_name = str(folder_name or "").strip()
    if not folder_name:
        raise RuntimeError("Google Drive folder name is empty.")

    escaped_name = folder_name.replace("'", "\\'")

    result = (
        service.files()
        .list(
            q=(
                f"name = '{escaped_name}' "
                "and mimeType = 'application/vnd.google-apps.folder' "
                "and trashed = false"
            ),
            pageSize=20,
            orderBy="name",
            fields="files(id,name,mimeType,trashed,parents)",
        )
        .execute()
    )

    folders = result.get("files", [])
    if not folders:
        raise RuntimeError(
            f'Google Drive folder "{folder_name}" was not found '
            "for the currently authorized Google account."
        )

    return folders[0]


def resolve_drive_folder_id(service, folder_id: str, folder_name: str) -> str:
    """
    Resolve a Drive folder for the current OAuth account.

    A valid accessible ID is preferred. If the configured ID is stale but
    a configured name exists, an exact-name lookup is attempted in the
    currently authorized account.
    """
    folder_id = normalize_drive_folder_id(folder_id)
    folder_name = str(folder_name or "").strip()

    if folder_id:
        try:
            return str(_drive_folder_metadata(service, folder_id)["id"])
        except Exception as id_error:
            if not folder_name:
                raise RuntimeError(
                    f'Configured Google Drive folder ID "{folder_id}" '
                    "is not accessible by the currently authorized account. "
                    f"Original error: {id_error}"
                ) from id_error

            print(
                f'Configured Drive folder ID "{folder_id}" is not accessible: '
                f"{id_error}"
            )
            print(f'Attempting exact-name recovery for "{folder_name}"...')

    metadata = _find_drive_folder_by_name(service, folder_name)
    resolved_id = str(metadata["id"])

    print(
        f'Google Drive folder "{folder_name}" resolved to "{resolved_id}" '
        "for the current account."
    )
    return resolved_id


def _configured_drive_folder_name(folder_id: str, folder_configs: list[dict]) -> str:
    """Return the configured folder name associated with a folder ID."""
    folder_id = normalize_drive_folder_id(folder_id)

    for item in folder_configs:
        if not isinstance(item, dict):
            continue
        item_id = normalize_drive_folder_id(str(item.get("id", "") or ""))
        if item_id == folder_id:
            return str(item.get("name", "") or "").strip()

    return ""


def resolve_configured_drive_folder(
    service,
    folder_id: str,
    folder_configs: list[dict],
    *,
    label: str = "Google Drive folder",
) -> tuple[str, str]:
    """Resolve a configured folder and return its current ID and name."""
    folder_id = normalize_drive_folder_id(folder_id)
    if not folder_id:
        raise RuntimeError(f"{label} ID is empty.")

    configured_name = _configured_drive_folder_name(
        folder_id,
        folder_configs,
    )

    resolved_id = resolve_drive_folder_id(
        service,
        folder_id,
        configured_name,
    )

    metadata = _drive_folder_metadata(service, resolved_id)
    resolved_name = str(
        metadata.get("name") or configured_name or label
    ).strip()

    return resolved_id, resolved_name


def ensure_drive_outputs_folder(
    service,
    parent_folder_id: str,
    parent_folder_name: str = "",
) -> str:
    """
    Return the `outputs` child folder, creating it when necessary.

    The parent is first validated against the current OAuth account, which
    prevents an inaccessible old-account folder ID from producing a confusing
    Drive 404 during the outputs query.
    """
    parent_folder_id = normalize_drive_folder_id(parent_folder_id)
    parent_folder_name = str(parent_folder_name or "").strip()

    if not parent_folder_id:
        raise RuntimeError("The Google Drive parent folder ID is empty.")

    try:
        parent_metadata = _drive_folder_metadata(
            service,
            parent_folder_id,
        )
        resolved_parent_id = str(parent_metadata["id"])
        resolved_parent_name = str(
            parent_metadata.get("name") or parent_folder_name
        ).strip()
    except Exception as parent_error:
        if not parent_folder_name:
            raise RuntimeError(
                f'Google Drive parent folder "{parent_folder_id}" is not '
                "accessible by the currently authorized account. "
                f"Original error: {parent_error}"
            ) from parent_error

        print(
            f'Parent folder ID "{parent_folder_id}" is not accessible: '
            f"{parent_error}"
        )
        print(
            f'Attempting exact-name recovery for "{parent_folder_name}"...'
        )

        recovered = _find_drive_folder_by_name(
            service,
            parent_folder_name,
        )
        resolved_parent_id = str(recovered["id"])
        resolved_parent_name = str(
            recovered.get("name") or parent_folder_name
        ).strip()

    escaped_parent_id = resolved_parent_id.replace("'", "\\'")

    result = (
        service.files()
        .list(
            q=(
                f"'{escaped_parent_id}' in parents "
                "and name = 'outputs' "
                "and mimeType = 'application/vnd.google-apps.folder' "
                "and trashed = false"
            ),
            pageSize=10,
            fields="files(id,name,mimeType,trashed,parents)",
        )
        .execute()
    )

    folders = result.get("files", [])
    if folders:
        outputs_id = str(folders[0]["id"])
        print(
            f'Found existing Drive/outputs folder {outputs_id} '
            f'under "{resolved_parent_name}".'
        )
        return outputs_id

    created = (
        service.files()
        .create(
            body={
                "name": "outputs",
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [resolved_parent_id],
            },
            fields="id,name,mimeType,parents",
        )
        .execute()
    )

    outputs_id = str(created["id"])
    print(
        f'Created Drive/outputs folder {outputs_id} '
        f'under "{resolved_parent_name}".'
    )
    return outputs_id


def get_drive_files(

    service,
    folder_id: str,
) -> list[dict]:
    files: list[dict] = []
    page_token = None

    while True:
        result = service.files().list(
            q=(
                f"'{folder_id}' in parents "
                "and trashed = false"
            ),
            pageSize=100,
            orderBy="name",
            fields=(
                "nextPageToken,"
                "files(id,name,mimeType,size,modifiedTime)"
            ),
            pageToken=page_token,
        ).execute()

        for item in result.get("files", []):
            mime_type = str(item.get("mimeType", ""))

            name = str(item.get("name", "Drive Reference"))
            size = int(item.get("size", 0) or 0)
            extension = Path(name).suffix.lower()

            is_image = mime_type.startswith("image/") and mime_type != "image/svg+xml"
            is_document = extension in DOCUMENT_EXTENSIONS or mime_type in {
                "application/pdf",
                "application/vnd.ms-powerpoint",
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            }
            if not is_image and not is_document:
                continue

            if is_document:
                file_type = get_document_kind(extension)
            else:
                file_type = (
                    "gif"
                    if extension == ".gif" or mime_type == "image/gif"
                    else "image"
                )

            drive_id = str(item["id"])
            file_data = {
                "id": f"drive:{drive_id}",
                "name": name,
                "type": file_type,
                "mimeType": mime_type,
                "size": size,
                "sizeFormatted": format_file_size(size),
                "url": f"/api/drive/file/{drive_id}",
                "source": "google-drive",
                "driveFileId": drive_id,
            }

            metadata = load_tag_metadata(METADATA_FILE)
            cached = metadata.get(f"drive:{drive_id}")
            if isinstance(cached, dict) and cached.get("tag"):
                file_data["tag"] = str(cached["tag"])

            files.append(file_data)

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    return files


def download_drive_file(
    service,
    file_id: str,
    destination: Path,
) -> Path:
    """
    Download the real Google Drive media bytes.

    The previous implementation used MediaIoBaseDownload. In this project
    that endpoint was returning a small JSON document instead of the image
    bytes, so this implementation performs an authenticated Drive REST GET
    explicitly with alt=media and verifies the response before saving it.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)

    # Reuse the credentials already loaded by the Drive service.
    credentials = getattr(service, "_http", None)
    credentials = getattr(credentials, "credentials", None)

    if credentials is None:
        raise RuntimeError(
            "Unable to access the authenticated Google Drive credentials."
        )

    # Make sure an expired access token is refreshed before the request.
    if not credentials.valid:
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(GoogleAuthRequest())
        else:
            raise RuntimeError(
                "Google Drive authorization is not valid. "
                "Run 'python test_google_drive.py' once to authorize again."
            )

    from google.auth.transport.requests import AuthorizedSession

    session = AuthorizedSession(credentials)

    url = (
        "https://www.googleapis.com/drive/v3/files/"
        f"{quote(str(file_id), safe='')}"
    )

    try:
        response = session.get(
            url,
            params={
                "alt": "media",
            },
            timeout=60,
        )
    except Exception as exc:
        raise RuntimeError(
            f"Google Drive media request failed: {exc}"
        ) from exc

    if response.status_code != 200:
        body = response.text[:500]
        raise RuntimeError(
            "Google Drive media request returned "
            f"HTTP {response.status_code}: {body}"
        )

    data = response.content

    if not data:
        raise RuntimeError("Google Drive returned an empty file.")

    content_type = str(
        response.headers.get("Content-Type", "")
    ).lower()

    # Drive must return image bytes for an image reference. If the response
    # is JSON, include a short diagnostic instead of saving it as an image.
    if "application/json" in content_type or data.lstrip().startswith(
        (b"{", b"[")
    ):
        preview = data[:300].decode(
            "utf-8",
            errors="replace",
        )
        raise RuntimeError(
            "Google Drive returned JSON instead of image bytes. "
            f"Response: {preview}"
        )

    temp_path = destination.with_name(
        f".{destination.name}.download"
    )

    try:
        temp_path.write_bytes(data)
        temp_path.replace(destination)
    finally:
        if temp_path.exists():
            temp_path.unlink()

    if not destination.exists() or destination.stat().st_size == 0:
        raise RuntimeError("Google Drive returned an empty file.")

    return destination

def is_valid_image_file(path: Path) -> bool:
    """
    Check that a cached Drive file is actually a readable image.
    This prevents old metadata JSON from being returned as image/png.
    """
    if not path.exists() or not path.is_file() or path.stat().st_size == 0:
        return False

    try:
        from PIL import Image

        with Image.open(path) as image:
            image.verify()

        return True
    except Exception:
        return False


def safe_drive_filename(
    file_id: str,
    filename: str,
) -> Path:
    clean_name = Path(filename).name
    if not clean_name:
        clean_name = "drive_reference"

    return UPLOADS_DIR / f"drive_{file_id}_{clean_name}"


# Restore the last non-secret Drive folder configuration when the
# FastAPI process starts. This prevents /api/inputs from losing the Drive
# references after a backend restart.
load_persisted_drive_configuration()


# -------------------------------------------------------------------
# CORS
# -------------------------------------------------------------------



# -------------------------------------------------------------------
# Supported files
# -------------------------------------------------------------------

IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
}

PDF_EXTENSIONS = {
    ".pdf",
}

VIDEO_EXTENSIONS = {
    ".mp4",
    ".webm",
    ".mov",
}

DOCUMENT_EXTENSIONS = {
    ".pdf",
    ".ppt",
    ".pptx",
}


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------

def get_file_type(
    path: Path,
) -> str:

    extension = (
        path.suffix.lower()
    )

    if extension in IMAGE_EXTENSIONS:

        if extension == ".gif":
            return "gif"

        return "image"

    if extension in DOCUMENT_EXTENSIONS:
        return get_document_kind(extension)

    if extension in VIDEO_EXTENSIONS:
        return "video"

    return "unknown"


def get_mime_type(
    path: Path,
) -> str:

    extension = (
        path.suffix.lower()
    )

    mime_types = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".pdf": "application/pdf",
        ".ppt": "application/vnd.ms-powerpoint",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
    }

    return mime_types.get(
        extension,
        "application/octet-stream",
    )


def format_file_size(
    size: int,
) -> str:

    if size < 1024:
        return f"{size} B"

    if size < 1024 * 1024:
        return (
            f"{size / 1024:.1f} KB"
        )

    if size < (
        1024 * 1024 * 1024
    ):
        return (
            f"{size / (1024 * 1024):.1f} MB"
        )

    return (
        f"{size / (1024 * 1024 * 1024):.1f} GB"
    )


def is_valid_http_url(
    value: str,
) -> bool:

    from urllib.parse import (
        urlparse,
    )

    try:

        parsed = urlparse(
            value
        )

        return (
            parsed.scheme
            in {"http", "https"}
            and bool(parsed.netloc)
        )

    except Exception:
        return False


# -------------------------------------------------------------------
# API key setup
# -------------------------------------------------------------------

@app.post(
    "/api/api-keys/upload"
)
async def upload_api_keys_file(
    file: UploadFile = File(...),
):
    if not file.filename:
        raise HTTPException(
            status_code=400,
            detail="No API key filename provided.",
        )

    filename = Path(
        file.filename
    ).name

    extension = Path(
        filename
    ).suffix.lower()

    if extension not in {
        ".env",
        ".txt",
        ".json",
    }:
        raise HTTPException(
            status_code=400,
            detail=(
                "API key files must be .env, .txt or .json."
            ),
        )

    file_bytes = await file.read()

    if not file_bytes:
        raise HTTPException(
            status_code=400,
            detail="The API key file is empty.",
        )

    if len(file_bytes) > 2 * 1024 * 1024:
        raise HTTPException(
            status_code=400,
            detail="The API key file is too large.",
        )

    try:
        values = parse_api_key_file(
            file_bytes,
            filename,
        )
        api_icon_map = extract_api_key_icons(file_bytes, filename)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    if not values:
        raise HTTPException(
            status_code=400,
            detail=(
                "No key=value or key:value entries were found "
                "in the uploaded API key file."
            ),
        )

    API_KEY_STATE["keys"] = {}
    API_KEY_STATE["selected_ids"] = []
    API_KEY_STATE["pipeline_key_id"] = ""
    API_KEY_STATE["config"] = dict(values)

    configuration_names = {
        "GEMINI_MODEL", "GOOGLE_AI_MODEL", "MODEL",
        "GOOGLE_DRIVE_FOLDER_ID", "GDRIVE_FOLDER_ID", "DRIVE_FOLDER_ID",
        "GOOGLE_DRIVE_FOLDER_URL", "GDRIVE_FOLDER_URL", "DRIVE_FOLDER_URL",
        "GOOGLE_DRIVE_FOLDER_NAME", "GDRIVE_FOLDER_NAME", "GOOGLE_DRIVE_NAME",
        "GDRIVE_NAME", "DRIVE_FOLDER_NAME", "DRIVE_NAME",
        "GOOGLE_DRIVE_FOLDER_IDS", "GDRIVE_FOLDER_IDS", "DRIVE_FOLDER_IDS",
        "GOOGLE_DRIVE_OUTPUT_FOLDER_ID", "GDRIVE_OUTPUT_FOLDER_ID", "DRIVE_OUTPUT_FOLDER_ID",
        "GOOGLE_DRIVE_OUTPUT_FOLDER_IDS", "GDRIVE_OUTPUT_FOLDER_IDS", "DRIVE_OUTPUT_FOLDER_IDS",
        "GOOGLE_DRIVE_OUTPUT_FOLDER_NAME", "GDRIVE_OUTPUT_FOLDER_NAME", "DRIVE_OUTPUT_FOLDER_NAME",
    }
    provider_counts: dict[str, int] = {}
    for key_name, value in values.items():
        normalized = normalize_key_name(key_name)
        base_name = re.sub(r"_\d+$", "", normalized)
        if not value or base_name in configuration_names:
            continue
        # Credential names are not a capability gate. Any credential can be
        # selected; its actual protocol/capabilities are resolved at runtime.
        service = logical_api_service(normalized)
        provider_counts[service] = provider_counts.get(service, 0) + 1
        occurrence = provider_counts[service]
        display_name = (
            "Gemini API" if service == "gemini" else
            "OpenRouter API" if service == "openrouter" else
            "OpenAI API" if service == "openai" else
            display_api_name(normalized)
        )
        if occurrence > 1:
            display_name = f"{display_name} {occurrence}"
        API_KEY_STATE["keys"][normalized] = {
            "key_name": normalized, "value": str(value).strip(),
            "display_name": display_name,
            "display_icon": api_icon_map.get(normalized, ""),
            "auth_type": "api-key",
            "service": service,
        }
        # Capability is evaluated from the actual credential/configuration,
        # not from the provider name alone. This makes newly added providers
        # work when their required endpoint/model settings are supplied.
        API_KEY_STATE["keys"][normalized]["image_generation"] = service_can_generate_image(
            API_KEY_STATE["keys"][normalized]
        )

    drive_folders = extract_drive_folder_configs(values)
    drive_output_folders = extract_drive_output_folder_configs(values)
    API_KEY_STATE["drive_folders"] = drive_folders
    API_KEY_STATE["drive_output_folders"] = drive_output_folders
    API_KEY_STATE["drive_folder_id"] = drive_folders[0]["id"] if drive_folders else ""
    API_KEY_STATE["drive_folder_name"] = drive_folders[0]["name"] if drive_folders else ""
    API_KEY_STATE["drive_output_folder_id"] = ""
    API_KEY_STATE["drive_output_folder_name"] = "outputs"

    API_KEY_STATE["gemini_model"] = (
        find_config_value(
            values,
            ("GEMINI_MODEL", "GOOGLE_AI_MODEL", "MODEL"),
        )
        or "gemini-3.5-flash-lite"
    )

    # Keep the non-secret Drive folder configuration across backend restarts.
    persist_drive_configuration()

    # Google Drive is an OAuth service, not another API-key credential.
    if drive_oauth_available() and drive_folders:
        API_KEY_STATE["keys"][DRIVE_OAUTH_KEY_ID] = {
            "key_name": DRIVE_OAUTH_KEY_ID,
            "value": "",
            "display_name": "Google Drive API",
            "display_icon": "",
            "auth_type": "oauth",
            "service": "google-drive",
        }

    response_keys = []
    name_counts: dict[str, int] = {}
    for key_id, item in API_KEY_STATE["keys"].items():
        # Show the actual API key name from the uploaded configuration, not a
        # provider label. Duplicate credentials remain visible as 1, 2, 3...
        base_name = str(item.get("key_name") or key_id).strip()
        base_name = re.sub(r"_\d+$", "", base_name)
        occurrence = name_counts.get(base_name, 0) + 1
        name_counts[base_name] = occurrence
        display_name = base_name if occurrence == 1 and name_counts.get(base_name, 0) == 1 else f"{base_name} {occurrence}"
        # If the name only occurs once, remove any temporary numbering.
        response_keys.append({
            "id": key_id,
            "name": display_name,
            "keyName": key_id,
            "authType": item.get("auth_type", "api-key"),
            "icon": item.get("display_icon", ""),
        })

    # Convert a repeated provider/key name to 1, 2, 3 only when duplicated.
    totals: dict[str, int] = {}
    for item in response_keys:
        base = re.sub(r" \d+$", "", item["name"])
        totals[base] = totals.get(base, 0) + 1
    seen: dict[str, int] = {}
    for item in response_keys:
        base = re.sub(r" \d+$", "", item["name"])
        seen[base] = seen.get(base, 0) + 1
        if totals[base] == 1:
            item["name"] = base
        else:
            item["name"] = f"{base} {seen[base]}"

    return {
        "success": True,
        "keys": response_keys,
    }


@app.post(
    "/api/api-keys/select"
)
def select_api_keys(
    selection: ApiKeySelection,
):
    available = API_KEY_STATE["keys"]

    if not available:
        raise HTTPException(
            status_code=400,
            detail="Upload an API key file first.",
        )

    selected_ids = [
        key_id
        for key_id in selection.selected_ids
        if key_id in available
    ]

    # Empty selection is valid while the user is still on API Setup.
    # Actual AI operations require at least one stage-capable selected key.
    API_KEY_STATE["selected_ids"] = list(selected_ids)
    API_KEY_STATE["pipeline_key_id"] = ""

    configure_selected_environment()

    selected_names = [
        available[key_id]["display_name"]
        for key_id in selected_ids
    ]

    return {
        "success": True,
        "selected": selected_names,
    }


@app.get(
    "/api/api-keys/status"
)
def api_key_status():
    available = API_KEY_STATE["keys"]

    return {
        "configured": bool(available),
        "selected": [
            available[key_id]["display_name"]
            for key_id in API_KEY_STATE["selected_ids"]
            if key_id in available
        ],
    }


# -------------------------------------------------------------------
# Google Drive input files
# -------------------------------------------------------------------

@app.get(
    "/api/drive/inputs"
)
def get_drive_inputs():
    folder_id, folder_name = require_drive_configuration()

    try:
        service = get_drive_service()
        resolved_folder_id = resolve_drive_folder_id(
            service,
            folder_id,
            folder_name,
        )

        API_KEY_STATE["drive_folder_id"] = resolved_folder_id
        # Create the generated-image destination once the reference folder is available.
        output_folder_id = ensure_drive_outputs_folder(service, resolved_folder_id)
        API_KEY_STATE["drive_output_folder_id"] = output_folder_id
        API_KEY_STATE["drive_output_folder_name"] = "outputs"
        persist_drive_configuration()

        return get_drive_files(
            service,
            resolved_folder_id,
        )

    except HTTPException:
        raise

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=str(exc),
        ) from exc


@app.get(
    "/api/drive/file/{file_id}"
)
def get_drive_file(
    file_id: str,
):
    """
    Serve a Google Drive image through the backend.

    The browser never needs direct access to Google Drive. The file is
    downloaded once into the backend uploads cache and returned with an
    inline image response.
    """

    # A Drive file ID returned by /api/drive/inputs is already a backend-known
    # reference. Previewing that file should depend on Drive authorization and
    # the file's own access permissions, not on the mutable folder-config state.
    # This prevents a valid preview request from returning 400 when the folder
    # configuration has not been restored into application state yet.
    file_id = str(
        file_id or ""
    ).strip()

    if not file_id:
        raise HTTPException(
            status_code=400,
            detail="Google Drive file ID is required.",
        )

    try:
        service = get_drive_service()

        metadata = (
            service.files()
            .get(
                fileId=file_id,
                fields="id,name,mimeType,size",
            )
            .execute()
        )

        mime_type = str(
            metadata.get(
                "mimeType",
                "",
            )
        ).strip()

        if not mime_type.startswith("image/"):
            raise HTTPException(
                status_code=400,
                detail=(
                    "The selected Google Drive file is "
                    "not a supported image."
                ),
            )

        clean_name = (
            Path(
                str(
                    metadata.get(
                        "name",
                        "reference",
                    )
                )
            ).name
            or "reference"
        )

        # v2 avoids preview files created by older code that could contain
        # Google Drive metadata JSON instead of the actual image.
        cache_path = (
            UPLOADS_DIR
            / f"drive_preview_v3_{file_id}_{clean_name}"
        )

        if not is_valid_image_file(cache_path):
            if cache_path.exists():
                try:
                    cache_path.unlink()
                except OSError:
                    pass

            download_drive_file(
                service,
                file_id,
                cache_path,
            )

        # Do not return a file just because it exists. Verify its bytes first.
        if not is_valid_image_file(cache_path):
            raise RuntimeError(
                "Google Drive did not return valid image bytes. "
                "The response was not a readable image."
            )

        return FileResponse(
            path=cache_path,
            media_type=(
                mime_type
                or get_mime_type(cache_path)
            ),
            filename=clean_name,
            headers={
                "Content-Disposition":
                    f'inline; filename="{clean_name}"',
                "Cache-Control":
                    "no-cache, no-store, must-revalidate",
                "Pragma":
                    "no-cache",
                "Expires":
                    "0",
            },
        )

    except HTTPException:
        raise

    except Exception as exc:
        print(
            "Google Drive preview failed:",
            repr(exc),
        )

        raise HTTPException(
            status_code=500,
            detail=(
                "Unable to retrieve the Google Drive "
                f"reference: {exc}"
            ),
        ) from exc


# -------------------------------------------------------------------
# Input files
# -------------------------------------------------------------------

def get_input_files() -> list[dict]:
    """
    Return references from the configured Google Drive folder plus
    locally uploaded images.

    Google Drive loading is based on the uploaded folder configuration and
    the OAuth files in the backend folder. It does not depend on a frontend
    checkbox.
    """
    # The API-key session is intentionally in memory, but Drive folder
    # configuration is non-secret and persisted so references survive a
    # backend restart.
    if not (
        normalize_drive_folder_id(
            API_KEY_STATE.get("drive_folder_id", "")
        )
        or API_KEY_STATE.get("drive_folder_name", "")
    ):
        load_persisted_drive_configuration()

    drive_files: list[dict] = []

    has_drive_configuration = bool(
        normalize_drive_folder_id(
            API_KEY_STATE.get("drive_folder_id", "")
        )
        or API_KEY_STATE.get("drive_folder_name", "")
    )

    if (
        has_drive_configuration
        and CREDENTIALS_FILE.exists()
        and TOKEN_FILE.exists()
    ):
        try:
            service = get_drive_service()
            configured_folders = API_KEY_STATE.get("drive_folders", [])
            if not configured_folders:
                configured_folders = [{
                    "id": normalize_drive_folder_id(API_KEY_STATE.get("drive_folder_id", "")),
                    "name": str(API_KEY_STATE.get("drive_folder_name", "") or "").strip(),
                }]

            all_drive_files: list[dict] = []
            resolved_folders: list[dict[str, str]] = []
            for folder in configured_folders:
                folder_id = normalize_drive_folder_id(str(folder.get("id", "") or ""))
                folder_name = str(folder.get("name", "") or "").strip()
                if not folder_id and not folder_name:
                    continue
                resolved_folder_id = resolve_drive_folder_id(service, folder_id, folder_name)
                resolved_folders.append({"id": resolved_folder_id, "name": folder_name})
                for drive_file in get_drive_files(service, resolved_folder_id):
                    drive_file["driveFolderId"] = resolved_folder_id
                    drive_file["driveFolderName"] = folder_name
                    all_drive_files.append(drive_file)

            drive_files = all_drive_files
            API_KEY_STATE["drive_folders"] = resolved_folders
            if resolved_folders:
                API_KEY_STATE["drive_folder_id"] = resolved_folders[0]["id"]
                API_KEY_STATE["drive_folder_name"] = resolved_folders[0]["name"]
            persist_drive_configuration()

        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=(
                    "Unable to load Google Drive references: "
                    f"{exc}"
                ),
            ) from exc

    manual_files = get_manual_upload_files()

    return sorted(
        drive_files + manual_files,
        key=lambda item: item["name"].lower(),
    )


# -------------------------------------------------------------------
# Manual uploads
# -------------------------------------------------------------------

def get_manual_upload_files() -> list[dict]:
    files = []

    metadata = load_tag_metadata(
        METADATA_FILE
    )

    supported_extensions = IMAGE_EXTENSIONS | DOCUMENT_EXTENSIONS

    for path in sorted(
        MANUAL_UPLOADS_DIR.iterdir(),
        key=lambda item: item.name.lower(),
    ):
        if not path.is_file():
            continue

        if path.suffix.lower() not in supported_extensions:
            continue

        file_type = get_file_type(path)
        file_data = {
            "id": f"manual:{path.name}",
            "name": path.name,
            "type": file_type,
            "mimeType": get_mime_type(path),
            "size": path.stat().st_size,
            "sizeFormatted": format_file_size(path.stat().st_size),
            "url": f"/api/manual/file/{path.name}",
            "source": "manual-upload",
        }

        cached_item = metadata.get(path.name)

        if (
            isinstance(cached_item, dict)
            and cached_item.get("tag")
        ):
            file_data["tag"] = str(
                cached_item["tag"]
            )

        files.append(file_data)

    return files


# -------------------------------------------------------------------
# Health check
# -------------------------------------------------------------------

@app.get("/")
def root():

    return {
        "message":
            "Image Generator API is running"
    }


@app.get("/api/health")
def health():

    return {
        "status": "ok"
    }


# -------------------------------------------------------------------
# Canva MCP OAuth / connection endpoints
# -------------------------------------------------------------------

@app.get("/api/canva/oauth/start")
async def canva_oauth_start():
    """Initialize the Canva MCP OAuth provider.

    The actual browser authorization is completed by the MCP OAuth
    flow. This endpoint intentionally does not expose OAuth tokens.
    """

    try:
        service = canva_mcp_service

        if service.oauth_provider is None:
            service.create_oauth_provider()

        return {
            "success": True,
            "message": (
                "Canva OAuth service is initialized. "
                "The MCP connection will start the authorization flow "
                "when authentication is required."
            ),
            "server": service.server_url,
            "redirect_uri": (
                "http://127.0.0.1:8000/api/canva/oauth/callback"
            ),
        }

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Canva OAuth initialization failed: {exc}",
        ) from exc


@app.get("/api/canva/oauth/status")
async def canva_oauth_status():
    """Return the current Canva MCP OAuth state."""

    try:
        return await canva_mcp_service.get_connection_status()

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Unable to read Canva OAuth status: {exc}",
        ) from exc


@app.get("/api/canva/oauth/callback")
async def canva_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    iss: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
):
    """
    Receive the OAuth redirect from Canva and pass it to the MCP OAuth
    provider. The authorization code is never returned to the frontend.
    """

    try:
        result = await canva_mcp_service.complete_oauth_callback(
            code=code,
            state=state,
            iss=iss,
            error=error,
            error_description=error_description,
        )

        if not result.get("success"):
            message = str(
                result.get("message")
                or result.get("error_description")
                or result.get("error")
                or "Canva authorization failed."
            )
            return HTMLResponse(
                content=(
                    "<!doctype html>"
                    "<html><head><title>Canva Authorization</title></head>"
                    "<body style=\"font-family:Arial,sans-serif;padding:40px;\">"
                    "<h2>Canva authorization failed</h2>"
                    f"<p>{message}</p>"
                    "<p>You can close this window and return to the Image Generator.</p>"
                    "</body></html>"
                ),
                status_code=400,
            )

        return HTMLResponse(
            content=(
                "<!doctype html>"
                "<html><head><title>Canva Authorization</title></head>"
                "<body style=\"font-family:Arial,sans-serif;padding:40px;\">"
                "<h2>Canva authorization received</h2>"
                "<p>The Image Generator received the Canva authorization response.</p>"
                "<p>You can close this window and return to the Image Generator.</p>"
                "</body></html>"
            ),
            status_code=200,
        )

    except Exception as exc:
        import traceback

        print()
        print("=" * 80)
        print("CANVA OAUTH CALLBACK ERROR")
        print("=" * 80)
        traceback.print_exc()
        print("=" * 80)
        print()

        raise HTTPException(
            status_code=500,
            detail=f"Canva OAuth callback failed: {type(exc).__name__}: {exc}",
        ) from exc



@app.get("/api/canva/connect/oauth/status")
async def canva_connect_oauth_status():
    try:
        return await canva_connect_service.status()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/canva/connect/oauth/start")
async def canva_connect_oauth_start():
    try:
        return {
            "success": True,
            "authorization_url": canva_connect_service.authorization_url(),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/canva/connect/oauth/callback")
async def canva_connect_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
):
    if error:
        message = error_description or error or "Canva authorization failed."
        return HTMLResponse(
            content=(
                "<!doctype html><html><body style='font-family:Arial;padding:40px'>"
                "<h2>Canva authorization failed</h2>"
                f"<p>{message}</p><p>Close this window and return to Image Generator.</p>"
                "</body></html>"
            ),
            status_code=400,
        )

    if not code:
        return HTMLResponse(
            content=(
                "<!doctype html><html><body style='font-family:Arial;padding:40px'>"
                "<h2>Canva authorization failed</h2>"
                "<p>No authorization code was returned.</p>"
                "</body></html>"
            ),
            status_code=400,
        )

    try:
        await canva_connect_service.exchange_code(code, state)
        return HTMLResponse(
            content=(
                "<!doctype html><html><body style='font-family:Arial;padding:40px'>"
                "<h2>Canva connected successfully</h2>"
                "<p>You can close this window and return to the Image Generator.</p>"
                "</body></html>"
            ),
            status_code=200,
        )
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return HTMLResponse(
            content=(
                "<!doctype html><html><body style='font-family:Arial;padding:40px'>"
                "<h2>Canva authorization failed</h2>"
                f"<p>{str(exc)}</p>"
                "<p>Close this window and try Connect Canva again.</p>"
                "</body></html>"
            ),
            status_code=500,
        )


@app.post("/api/canva/create-from-generated-image")
async def canva_create_from_generated_image(
    filename: str = Form(...),
    design_type: str = Form("poster"),
):
    """Create an editable Canva design from a local generated image.

    This uses Canva Connect's direct binary asset upload. The local image is
    never exposed through a public URL and no Cloudflare/ngrok tunnel is used.
    """
    safe_name = Path(filename).name
    if not safe_name:
        raise HTTPException(status_code=400, detail="Generated image filename is required.")

    local_path = IMAGE_OUTPUT_DIR / safe_name
    if not local_path.exists() or not local_path.is_file():
        raise HTTPException(status_code=404, detail="Generated image was not found on the server.")

    try:
        return await canva_connect_service.create_editable_design_from_local_image(local_path)
    except Exception as exc:
        message = str(exc)
        lowered = message.lower()
        if "not authorized" in lowered or "not authenticated" in lowered or "connect canva" in lowered:
            try:
                authorization_url = canva_connect_service.authorization_url()
            except Exception:
                authorization_url = ""
            raise HTTPException(
                status_code=401,
                detail=message,
                headers={"X-Canva-Authorization-URL": authorization_url},
            ) from exc
        raise HTTPException(status_code=502, detail=f"Unable to create the editable Canva design: {message}") from exc


def _canva_ai_text_completion(pipeline_item: dict, instruction: str) -> str:
    """Use the selected text-capable API to turn an edit command into MCP operations."""
    service = str(pipeline_item.get("service") or "").strip().lower()
    if service == "claude":
        result = _claude_request(
            str(pipeline_item.get("value", "")).strip(),
            {
                "model": _provider_text_model(pipeline_item),
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": instruction}],
            },
        )
        content = result.get("content") or []
        return "".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ).strip()

    if service == "openrouter":
        result = _openrouter_request(
            str(pipeline_item.get("value", "")).strip(),
            "chat/completions",
            {
                "model": _provider_text_model(pipeline_item),
                "messages": [{"role": "user", "content": instruction}],
                "temperature": 0.1,
            },
        )
        return _chat_response_text(result, "OpenRouter")

    result = _generic_request(
        pipeline_item,
        "chat/completions",
        {
            "model": _provider_text_model(pipeline_item),
            "messages": [{"role": "user", "content": instruction}],
            "temperature": 0.1,
        },
    )
    return _chat_response_text(result, str(pipeline_item.get("display_name") or "Selected API"))


@app.post("/api/canva/ai-edit/prepare")
async def canva_ai_edit_prepare(
    design_id: str = Form(...),
    command: str = Form(...),
):
    """Translate a natural-language edit command into Canva MCP operations.

    The operation is intentionally prepared in a draft editing transaction.
    The frontend must explicitly click Save AI Changes before commit.
    """
    design_id = str(design_id or "").strip()
    command = str(command or "").strip()
    if not design_id or not command:
        raise HTTPException(status_code=400, detail="design_id and command are required.")

    transaction_id = ""
    try:
        start_result = await canva_mcp_service.call_tool(
            "start-editing-transaction",
            {"design_id": design_id},
        )
        start_payload = canva_mcp_service._extract_payload(start_result)
        transaction_id = str(
            canva_mcp_service._find_value(
                start_payload,
                {"transaction_id", "transactionId"},
            )
            or ""
        )
        if not transaction_id:
            raise RuntimeError("Canva MCP did not return an editing transaction ID.")

        _, pipeline_item = _require_pipeline_key("text")
        instruction = f"""
You are generating structured operations for Canva MCP editing.
The user command is:
{command}

The Canva start-editing-transaction response below contains the editable design
content, text regions, media/fills and page information:
{json.dumps(start_payload, ensure_ascii=False, default=str)[:60000]}

Return ONLY valid JSON with this exact top-level shape:
{{"operations":[...]}}

Use only these Canva MCP operation types when applicable:
- replace_text
- find_and_replace_text
- update_title
- update_fill
- insert_fill
- delete_element
- position_element
- resize_element
- format_text

Use the exact element/page identifiers and existing text from the design response.
Do not invent IDs. Do not delete pages. Do not return markdown or explanations.
If the command cannot be safely mapped to an operation using the supplied design
content, return {{"operations":[]}}.
"""
        raw = _canva_ai_text_completion(pipeline_item, instruction)
        parsed = _extract_json_object(raw)
        operations = parsed.get("operations")
        if not isinstance(operations, list) or not operations:
            raise RuntimeError("The selected AI API could not map that command to a safe Canva edit.")
        if not all(isinstance(item, dict) for item in operations):
            raise RuntimeError("The AI edit operations response was invalid.")

        perform_result = await canva_mcp_service.call_tool(
            "perform-editing-operations",
            {
                "transaction_id": transaction_id,
                "operations": operations,
            },
        )
        perform_payload = canva_mcp_service._extract_payload(perform_result)
        CANVA_AI_TRANSACTIONS[transaction_id] = {
            "design_id": design_id,
            "operations": operations,
        }

        preview_url = canva_mcp_service._find_value(
            perform_payload,
            {"thumbnail_url", "thumbnail", "preview_url", "url"},
        )
        return {
            "success": True,
            "design_id": design_id,
            "transaction_id": transaction_id,
            "operations": operations,
            "preview_url": str(preview_url or ""),
            "message": "AI edits are prepared in Canva draft mode. Review them, then click Save AI Changes.",
        }
    except Exception as exc:
        if transaction_id:
            try:
                await canva_mcp_service.call_tool(
                    "cancel-editing-transaction",
                    {"transaction_id": transaction_id},
                )
            except Exception:
                pass
        raise HTTPException(status_code=502, detail=f"Canva AI editing failed: {exc}") from exc


@app.post("/api/canva/ai-edit/commit")
async def canva_ai_edit_commit(transaction_id: str = Form(...)):
    transaction_id = str(transaction_id or "").strip()
    if not transaction_id:
        raise HTTPException(status_code=400, detail="transaction_id is required.")
    if transaction_id not in CANVA_AI_TRANSACTIONS:
        raise HTTPException(status_code=404, detail="The Canva AI editing transaction is no longer available. Prepare the edit again.")

    try:
        result = await canva_mcp_service.call_tool(
            "commit-editing-transaction",
            {"transaction_id": transaction_id},
        )
        info = CANVA_AI_TRANSACTIONS.pop(transaction_id)
        return {
            "success": True,
            "design_id": info["design_id"],
            "result": canva_mcp_service._extract_payload(result),
            "message": "AI changes were saved to the Canva design.",
        }
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Unable to save Canva AI changes: {exc}") from exc


@app.post("/api/canva/ai-edit/cancel")
async def canva_ai_edit_cancel(transaction_id: str = Form(...)):
    transaction_id = str(transaction_id or "").strip()
    if not transaction_id:
        raise HTTPException(status_code=400, detail="transaction_id is required.")
    if transaction_id not in CANVA_AI_TRANSACTIONS:
        return {"success": True, "message": "No active Canva AI editing transaction."}
    try:
        await canva_mcp_service.call_tool(
            "cancel-editing-transaction",
            {"transaction_id": transaction_id},
        )
    finally:
        CANVA_AI_TRANSACTIONS.pop(transaction_id, None)
    return {"success": True, "message": "AI draft changes were cancelled."}


@app.post("/api/canva/export-to-drive")
async def canva_export_to_drive(
    design_id: str = Form(...),
    folder_id: str = Form(...),
    filename: str = Form("enhanced-image.png"),
    file_format: str = Form("png"),
):
    """
    Export the latest saved Canva design and save it directly to the
    selected Google Drive/outputs folder.

    IMPORTANT:
    - The Canva export is kept in memory as bytes.
    - No final Canva export is written/downloaded to the local filesystem.
    """

    design_id = str(design_id or "").strip()
    requested_folder_id = normalize_drive_folder_id(folder_id)

    original_name = (
        Path(str(filename or "enhanced-image.png")).stem
        or "enhanced-image"
    )

    normalized_format = str(file_format or "png").strip().lower()
    if normalized_format not in {"png", "pdf", "pptx"}:
        raise HTTPException(status_code=400, detail="Supported Canva output formats are PNG, PDF, and PPTX.")
    extension = "pptx" if normalized_format == "pptx" else normalized_format
    safe_name = f"{original_name}_enhanced.{extension}"

    if not design_id:
        raise HTTPException(
            status_code=400,
            detail="Canva design ID is required.",
        )

    if not requested_folder_id:
        raise HTTPException(
            status_code=400,
            detail="Select a Google Drive output folder before saving.",
        )

    configured_outputs = (
        API_KEY_STATE.get("drive_output_folders", []) or []
    )

    allowed_ids = {
        normalize_drive_folder_id(str(item.get("id", "") or ""))
        for item in configured_outputs
        if isinstance(item, dict) and item.get("id")
    }

    if requested_folder_id not in allowed_ids:
        raise HTTPException(
            status_code=400,
            detail=(
                "The selected Google Drive output folder is not "
                "currently configured. Refresh the Google Drive "
                "folders and select an accessible folder."
            ),
        )

    # ------------------------------------------------------------
    # STEP 1: Export Canva design
    # ------------------------------------------------------------
    try:
        print()
        print("=" * 80)
        print("CANVA → GOOGLE DRIVE")
        print("=" * 80)
        print(f"Design ID      : {design_id}")
        print(f"Drive folder   : {requested_folder_id}")
        print(f"Output filename: {safe_name}")
        print()
        print(f"STEP 1: Exporting Canva design as {normalized_format.upper()}...")
        print()

        exported_bytes = await canva_connect_service.export_design(
            design_id,
            normalized_format,
        )

        if not exported_bytes:
            raise RuntimeError(
                f"Canva returned an empty {normalized_format.upper()} export."
            )

        print(
            f"STEP 1 SUCCESS: Canva export received "
            f"({len(exported_bytes):,} bytes)."
        )
        print()

    except Exception as exc:
        import traceback

        print()
        print("=" * 80)
        print("CANVA EXPORT FAILED")
        print("=" * 80)
        traceback.print_exc()
        print("=" * 80)
        print()

        raise HTTPException(
            status_code=502,
            detail=(
                "Canva export failed: "
                f"{type(exc).__name__}: {exc}"
            ),
        ) from exc

    # ------------------------------------------------------------
    # STEP 2: Connect to Google Drive
    # ------------------------------------------------------------
    try:
        print("STEP 2: Connecting to Google Drive...")

        service = get_drive_service()

        print("STEP 2 SUCCESS: Google Drive service connected.")
        print()

    except Exception as exc:
        import traceback

        print()
        print("=" * 80)
        print("GOOGLE DRIVE CONNECTION FAILED")
        print("=" * 80)
        traceback.print_exc()
        print("=" * 80)
        print()

        raise HTTPException(
            status_code=502,
            detail=(
                "Google Drive connection failed: "
                f"{type(exc).__name__}: {exc}"
            ),
        ) from exc

    # ------------------------------------------------------------
    # STEP 3: Find/create outputs folder
    # ------------------------------------------------------------
    try:
        print("STEP 3: Resolving outputs folder...")

        resolved_parent_id, resolved_parent_name = (
            resolve_configured_drive_folder(
                service,
                requested_folder_id,
                configured_outputs,
                label="Google Drive output folder",
            )
        )

        for item in configured_outputs:
            if not isinstance(item, dict):
                continue
            if normalize_drive_folder_id(
                str(item.get("id", "") or "")
            ) == requested_folder_id:
                item["id"] = resolved_parent_id
                item["name"] = resolved_parent_name

        outputs_folder_id = ensure_drive_outputs_folder(
            service,
            resolved_parent_id,
            resolved_parent_name,
        )

        API_KEY_STATE["drive_folder_id"] = resolved_parent_id
        API_KEY_STATE["drive_folder_name"] = resolved_parent_name
        API_KEY_STATE["drive_output_folder_id"] = outputs_folder_id
        API_KEY_STATE["drive_output_folder_name"] = "outputs"
        persist_drive_configuration()

        if not outputs_folder_id:
            raise RuntimeError(
                "The outputs folder ID could not be resolved."
            )

        print(
            f"STEP 3 SUCCESS: outputs folder = "
            f"{outputs_folder_id}"
        )
        print()

    except Exception as exc:
        import traceback

        print()
        print("=" * 80)
        print("OUTPUTS FOLDER RESOLUTION FAILED")
        print("=" * 80)
        traceback.print_exc()
        print("=" * 80)
        print()

        raise HTTPException(
            status_code=502,
            detail=(
                "Unable to resolve the Google Drive outputs folder: "
                f"{type(exc).__name__}: {exc}"
            ),
        ) from exc

    # ------------------------------------------------------------
    # STEP 4: Check whether the file already exists
    # ------------------------------------------------------------
    try:
        print("STEP 4: Checking for an existing output file...")

        escaped_name = safe_name.replace(
            chr(39),
            chr(92) + chr(39),
        )

        existing = (
            service.files()
            .list(
                q=(
                    f"'{outputs_folder_id}' in parents "
                    f"and name = '{escaped_name}' "
                    "and trashed = false"
                ),
                pageSize=10,
                fields="files(id,name,webViewLink)",
            )
            .execute()
            .get("files", [])
        )

        print(
            f"STEP 4 SUCCESS: "
            f"{len(existing)} existing file(s) found."
        )
        print()

    except Exception as exc:
        import traceback

        print()
        print("=" * 80)
        print("GOOGLE DRIVE FILE LOOKUP FAILED")
        print("=" * 80)
        traceback.print_exc()
        print("=" * 80)
        print()

        raise HTTPException(
            status_code=502,
            detail=(
                "Google Drive file lookup failed: "
                f"{type(exc).__name__}: {exc}"
            ),
        ) from exc

    # ------------------------------------------------------------
    # STEP 5: Upload Canva export directly from memory
    # ------------------------------------------------------------
    try:
        print(
            "STEP 5: Uploading Canva export directly "
            "from memory to Google Drive..."
        )

        output_mime = {
            "png": "image/png",
            "pdf": "application/pdf",
            "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        }[normalized_format]
        media = MediaIoBaseUpload(
            io.BytesIO(exported_bytes),
            mimetype=output_mime,
            resumable=False,
        )

        if existing:
            drive_file = (
                service.files()
                .update(
                    fileId=existing[0]["id"],
                    media_body=media,
                    fields="id,name,webViewLink",
                )
                .execute()
            )

            operation = "updated"

        else:
            drive_file = (
                service.files()
                .create(
                    body={
                        "name": safe_name,
                        "parents": [outputs_folder_id],
                    },
                    media_body=media,
                    fields="id,name,webViewLink",
                )
                .execute()
            )

            operation = "created"

        print(
            f"STEP 5 SUCCESS: Google Drive file {operation}."
        )
        print(
            f"Drive file ID: "
            f"{drive_file.get('id', '')}"
        )
        print()

    except Exception as exc:
        import traceback

        print()
        print("=" * 80)
        print("GOOGLE DRIVE UPLOAD FAILED")
        print("=" * 80)
        traceback.print_exc()
        print("=" * 80)
        print()

        raise HTTPException(
            status_code=502,
            detail=(
                "Google Drive upload failed: "
                f"{type(exc).__name__}: {exc}"
            ),
        ) from exc

    # ------------------------------------------------------------
    # SUCCESS
    # ------------------------------------------------------------
    print("=" * 80)
    print("CANVA → GOOGLE DRIVE SUCCESS")
    print("=" * 80)
    print(
        "The Canva design was exported and uploaded "
        "directly to Google Drive."
    )
    print("=" * 80)
    print()

    return {
        "success": True,
        "filename": safe_name,
        "drive_file_id": drive_file.get("id", ""),
        "drive_folder": "outputs",
        "parent_folder_id": requested_folder_id,
        "drive_url": drive_file.get("webViewLink", ""),
        "message": (
            "The latest saved Canva design was exported "
            "and saved directly to the selected "
            "Google Drive/outputs folder."
        ),
    }

@app.get("/api/canva/tools")
async def canva_list_tools():
    """Connect to Canva MCP and list the tools available to the backend."""

    try:
        tools = await canva_mcp_service.list_tools()

        return {
            "success": True,
            "count": len(tools),
            "tools": tools,
        }

    except BaseException as exc:
        import traceback

        print()
        print("=" * 80)
        print("CANVA MCP ERROR")
        print("=" * 80)
        print()
        traceback.print_exception(type(exc), exc, exc.__traceback__)

        # Python 3.11+ TaskGroup errors are ExceptionGroups. Print every
        # nested exception so the real MCP/OAuth/network error is visible
        # instead of only reporting "unhandled errors in a TaskGroup".
        if isinstance(exc, BaseExceptionGroup):
            print()
            print("=" * 80)
            print("NESTED TASKGROUP EXCEPTIONS")
            print("=" * 80)

            def _print_nested(group: BaseExceptionGroup, indent: int = 0) -> None:
                prefix = " " * indent
                for index, nested in enumerate(group.exceptions, start=1):
                    print(
                        f"{prefix}[{index}] "
                        f"{type(nested).__name__}: {nested}"
                    )
                    if isinstance(nested, BaseExceptionGroup):
                        _print_nested(nested, indent + 4)

            _print_nested(exc)
            print("=" * 80)

        print("END CANVA MCP ERROR")
        print("=" * 80)
        print()

        raise HTTPException(
            status_code=500,
            detail=(
                "Canva MCP connection failed: "
                f"{type(exc).__name__}: {exc}"
            ),
        ) from exc


# -------------------------------------------------------------------
# Get input files
# -------------------------------------------------------------------

@app.get("/api/inputs")
def get_inputs():
    return get_input_files()


# -------------------------------------------------------------------
# Serve input file
# -------------------------------------------------------------------

@app.get(
    "/api/inputs/file/{filename:path}"
)
def get_input_file(
    filename: str,
):

    file_path = (
        INPUT_DIR /
        filename
    )

    if not file_path.exists():

        raise HTTPException(
            status_code=404,
            detail="Input file not found.",
        )

    if not file_path.is_file():

        raise HTTPException(
            status_code=404,
            detail="Input file not found.",
        )

    return FileResponse(
        file_path,
        media_type=
            get_mime_type(
                file_path
            ),
    )


# -------------------------------------------------------------------
# Serve manual upload
# -------------------------------------------------------------------

@app.get(
    "/api/manual/file/{filename:path}"
)
def get_manual_file(
    filename: str,
):
    file_path = MANUAL_UPLOADS_DIR / Path(filename).name

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="Manual upload not found.",
        )

    return FileResponse(
        file_path,
        media_type=get_mime_type(file_path),
    )


# -------------------------------------------------------------------
# Upload image / GIF as a manual reference
# -------------------------------------------------------------------

@app.post(
    "/api/inputs/upload"
)
async def upload_input_file(
    file: UploadFile =
        File(...),
):

    if not file.filename:

        raise HTTPException(
            status_code=400,
            detail="No filename provided.",
        )

    original_name = Path(
        file.filename
    ).name

    extension = Path(
        original_name
    ).suffix.lower()

    if extension not in (IMAGE_EXTENSIONS | DOCUMENT_EXTENSIONS):
        raise HTTPException(
            status_code=400,
            detail="Supported manual references are PNG, JPG, WEBP, GIF, PDF, PPT, and PPTX.",
        )

    destination = (
        MANUAL_UPLOADS_DIR /
        original_name
    )

    try:

        file_bytes = (
            await file.read()
        )

        if not file_bytes:

            raise HTTPException(
                status_code=400,
                detail="Uploaded file is empty.",
            )

        destination.write_bytes(
            file_bytes
        )

        if extension in DOCUMENT_EXTENSIONS:
            tag = "document-reference"
        elif API_KEY_STATE.get("selected_ids"):
            _, pipeline_item = _require_pipeline_key("text")
            tag = _pipeline_tag(pipeline_item, destination)
            try:
                metadata = load_tag_metadata(METADATA_FILE)
                metadata[destination.name] = {"tag": tag}
                METADATA_FILE.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
        else:
            tag = ""

    except HTTPException:

        if destination.exists():
            destination.unlink()

        raise

    except Exception as exc:

        if destination.exists():
            destination.unlink()

        raise HTTPException(
            status_code=500,
            detail=(
                "Tagging failed: "
                f"{exc}"
            ),
        ) from exc

    return {
        "id": destination.name,
        "name": destination.name,
        "type": get_document_kind(extension) if extension in DOCUMENT_EXTENSIONS else (
            "gif" if extension == ".gif" else "image"
        ),
        "mimeType":
            get_mime_type(
                destination
            ),
        "size":
            destination.stat().st_size,
        "sizeFormatted":
            format_file_size(
                destination.stat().st_size
            ),
        "url":
            (
                f"/api/manual/file/"
                f"{destination.name}"
            ),
        "tag": tag,
    }


# -------------------------------------------------------------------
# Tag one image
# -------------------------------------------------------------------

@app.post(
    "/api/inputs/tag/{filename:path}"
)
def tag_input_file(
    filename: str,
):

    file_path = (
        INPUT_DIR /
        filename
    )

    if not file_path.exists():

        raise HTTPException(
            status_code=404,
            detail="Input file not found.",
        )

    if (
        file_path.suffix.lower()
        not in IMAGE_EXTENSIONS
    ):

        raise HTTPException(
            status_code=400,
            detail="Only images can be tagged.",
        )

    try:

        _, pipeline_item = _require_pipeline_key("text")
        tag = _pipeline_tag(pipeline_item, file_path)

    except Exception as exc:

        raise HTTPException(
            status_code=500,
            detail=(
                "Tagging failed: "
                f"{exc}"
            ),
        ) from exc

    return {
        "filename": file_path.name,
        "tag": tag,
        **_pipeline_metadata(pipeline_item, "text"),
    }


# -------------------------------------------------------------------
# Tag all images
# -------------------------------------------------------------------

@app.post(
    "/api/inputs/tag-all"
)
def tag_all_input_files():
    """
    Generate tags for every image available to the workspace.

    Google Drive images are identified by their stable Drive file ID.
    Manual uploads are identified by their filename.

    Existing cached tags are reused. Only images without a cached tag
    are sent to Gemini.
    """

    results = []
    errors = []

    _, pipeline_item = _require_pipeline_key("text")

    # ---------------------------------------------------------------
    # Manual uploads
    # ---------------------------------------------------------------

    if MANUAL_UPLOADS_DIR.exists():
        for path in sorted(
            MANUAL_UPLOADS_DIR.iterdir(),
            key=lambda item: item.name.lower(),
        ):
            if (
                not path.is_file()
                or path.suffix.lower()
                not in IMAGE_EXTENSIONS
            ):
                continue

            cache_key = (
                f"manual:{path.name}"
            )

            try:
                tag = _pipeline_tag(pipeline_item, path)

                results.append(
                    {
                        "id":
                            f"manual:{path.name}",
                        "filename":
                            path.name,
                        "tag": tag,
                        **_pipeline_metadata(pipeline_item, "text"),
                    }
                )

            except Exception as exc:
                errors.append(
                    {
                        "id":
                            f"manual:{path.name}",
                        "filename":
                            path.name,
                        "error":
                            str(exc),
                    }
                )

    # ---------------------------------------------------------------
    # Google Drive images
    # ---------------------------------------------------------------

    if (
        drive_oauth_available()
        and TOKEN_FILE.exists()
        and (
            API_KEY_STATE.get("drive_folder_id")
            or API_KEY_STATE.get("drive_folder_name")
        )
    ):

        try:
            folder_id, folder_name = (
                require_drive_configuration()
            )

            service = get_drive_service()

            resolved_folder_id = (
                resolve_drive_folder_id(
                    service,
                    folder_id,
                    folder_name,
                )
            )

            API_KEY_STATE[
                "drive_folder_id"
            ] = resolved_folder_id
            persist_drive_configuration()

            drive_files = get_drive_files(
                service,
                resolved_folder_id,
            )

            metadata = load_tag_metadata(
                METADATA_FILE
            )

            for drive_file in drive_files:

                drive_id = str(
                    drive_file.get(
                        "driveFileId",
                        "",
                    )
                ).strip()

                if not drive_id:
                    continue

                cache_key = (
                    f"drive:{drive_id}"
                )

                cached = metadata.get(
                    cache_key
                )

                # Reuse an existing tag.
                if (
                    isinstance(
                        cached,
                        dict,
                    )
                    and cached.get("tag")
                ):
                    results.append(
                        {
                            "id":
                                f"drive:{drive_id}",
                            "filename":
                                drive_file["name"],
                            "tag":
                                str(
                                    cached["tag"]
                                ),
                        }
                    )
                    continue

                try:
                    cache_path = (
                        safe_drive_filename(
                            drive_id,
                            drive_file["name"],
                        )
                    )

                    # Download the actual Drive media so Gemini receives
                    # image bytes rather than Drive metadata JSON.
                    download_drive_file(
                        service,
                        drive_id,
                        cache_path,
                    )

                    tag = _pipeline_tag(pipeline_item, cache_path)

                    results.append(
                        {
                            "id":
                                f"drive:{drive_id}",
                            "filename":
                                drive_file["name"],
                            "tag":
                                tag,
                        }
                    )

                except Exception as exc:
                    errors.append(
                        {
                            "id":
                                f"drive:{drive_id}",
                            "filename":
                                drive_file["name"],
                            "error":
                                str(exc),
                        }
                    )

        except Exception as exc:
            errors.append(
                {
                    "filename":
                        "Google Drive",
                    "error":
                        str(exc),
                }
            )

    return {
        "success": len(errors) == 0,
        "count": len(results),
        "results": results,
        "errors": errors,
        **_pipeline_metadata(pipeline_item, "text"),
    }



# -------------------------------------------------------------------
# -------------------------------------------------------------------
# PDF / PPT / PPTX document references
# -------------------------------------------------------------------

def _resolve_document_reference(source_type: str, source: str, filename: str) -> tuple[Path, str]:
    import uuid

    source_type = str(source_type or "").strip().lower()
    filename = Path(filename or "reference.pdf").name
    extension = Path(filename).suffix.lower()
    if not is_supported_document(extension):
        raise HTTPException(status_code=400, detail="Only PDF, PPT, and PPTX references are supported in document mode.")

    if source_type == "input-folder":
        path = INPUT_DIR / Path(source).name
    elif source_type == "upload":
        path = MANUAL_UPLOADS_DIR / Path(source).name
    elif source_type == "google-drive":
        require_drive_configuration()
        service = get_drive_service()
        path = UPLOADS_DIR / f"drive_document_{source}_{filename}"
        if path.exists():
            try: path.unlink()
            except OSError: pass
        download_drive_file(service, source, path)
    else:
        raise HTTPException(status_code=400, detail="Document references must come from an upload, input folder, or Google Drive.")

    if not path.exists() or not path.is_file() or path.stat().st_size == 0:
        raise HTTPException(status_code=400, detail="The selected document reference is empty or unavailable.")
    return path, document_mime_type(path)


@app.post("/api/canva/create-from-reference-document")
async def canva_create_from_reference_document(
    source_type: str = Form(...),
    source: str = Form(...),
    filename: str = Form(...),
    content_type: str = Form(""),
):
    """Import a local PDF/PPT/PPTX reference directly into Canva.

    Canva's Design Import API keeps the document structure editable where Canva
    can preserve it. No public URL, Cloudflare, ngrok, or temporary file host is used.
    """
    local_path, mime_type = _resolve_document_reference(source_type, source, filename)
    try:
        result = await canva_connect_service.import_design_from_local_file(local_path)
        result["source_filename"] = local_path.name
        result["source_mime_type"] = content_type or mime_type
        result["editable_scope"] = "canva-design-import"
        result["public_tunnel_required"] = False
        result["message"] = (
            "The document was imported directly into Canva. Canva preserved editable "
            "elements where supported by the source file. You can now use manual Canva "
            "editing or the Canva MCP AI Edit workflow."
        )
        return result
    except Exception as exc:
        message = str(exc)
        lowered = message.lower()
        if "not authorized" in lowered or "not authenticated" in lowered or "connect canva" in lowered:
            try:
                authorization_url = canva_connect_service.authorization_url()
            except Exception:
                authorization_url = ""
            raise HTTPException(status_code=401, detail=message, headers={"X-Canva-Authorization-URL": authorization_url}) from exc
        raise HTTPException(status_code=502, detail=f"Unable to import the document into Canva: {message}") from exc


# -------------------------------------------------------------------
# Image generation
# -------------------------------------------------------------------

IMAGE_OUTPUT_DIR = BASE_DIR / "output" / "images"
IMAGE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


SOCIAL_MEDIA_ACCOUNTS = {
    "LinkedIn": {
        "tag": "@Meera Marrakula",
        "url": "https://www.linkedin.com/in/meera-marrakula/",
    },
    "X / Twitter": {
        "tag": "@tinitiateai",
        "url": "https://x.com/tinitiateai",
    },
    "Facebook": {
        "tag": "@Tinitiate AI",
        "url": "https://www.facebook.com/profile.php?id=61589182754060",
    },
    "Instagram": {
        "tag": "@tinitiate.ai",
        "url": "https://www.instagram.com/tinitiate.ai/",
    },
}

def _safe_social_media_name(image_filename: str) -> str:
    """Return the matching TXT filename for a generated image."""
    stem = Path(image_filename or "generated").stem
    return f"{stem}_description.txt"

def _word_count(text: str) -> int:
    return len(re.findall(r"\S+", str(text or "")))

def _trim_social_copy(text: str, max_chars: int) -> str:
    text = re.sub(r"\n{3,}", "\n\n", str(text or "").strip())
    if len(text) <= max_chars:
        return text
    clipped = text[: max_chars - 1].rsplit(" ", 1)[0].rstrip()
    return clipped + "…"

def _extract_chat_text(result: dict, provider_name: str) -> str:
    """Extract text from common chat-completions response shapes."""
    return _chat_response_text(result, provider_name).strip()

def _pipeline_social_text(pipeline_item: dict, instruction: str) -> str:
    """Generate social copy with the currently selected text-capable pipeline."""
    service = str(pipeline_item.get("service") or "").strip().lower()
    model = _provider_text_model(pipeline_item)
    api_key = str(pipeline_item.get("value") or "").strip()
    if not api_key:
        raise RuntimeError("The selected pipeline API key is empty.")
    if not model:
        raise RuntimeError("The selected API key has no text-generation model configured.")

    if service == "openrouter":
        result = _openrouter_request(
            api_key,
            "chat/completions",
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": "You write platform-specific social-media captions. Follow the requested format and limits exactly. Keep emojis/icons and tags."},
                    {"role": "user", "content": instruction},
                ],
                "temperature": 0.7,
            },
        )
        return _extract_chat_text(result, "OpenRouter")

    if service == "gemini":
        def call_gemini():
            from google import genai
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model=model,
                contents=instruction,
            )
            text = getattr(response, "text", None)
            if text:
                return str(text).strip()
            raise RuntimeError("Gemini returned no text for the social-media description.")
        return _with_pipeline_key(pipeline_item, call_gemini)

    result = _generic_request(
        pipeline_item,
        "chat/completions",
        {
            "model": model,
            "messages": [
                {"role": "system", "content": "You write platform-specific social-media captions. Follow the requested format and limits exactly. Keep emojis/icons and tags."},
                {"role": "user", "content": instruction},
            ],
            "temperature": 0.7,
        },
    )
    return _extract_chat_text(result, str(pipeline_item.get("display_name") or "API"))

def _normalize_social_sections(raw: str) -> str:
    """Normalize the four platform sections and enforce hard character/word caps."""
    text = str(raw or "").strip()
    headings = ["[LINKEDIN]", "[X / TWITTER]", "[FACEBOOK]", "[INSTAGRAM]"]
    limits = {"[LINKEDIN]": (180, 3000), "[X / TWITTER]": (None, 280), "[FACEBOOK]": (120, 10000), "[INSTAGRAM]": (100, 2200)}
    minimums = {"[LINKEDIN]": 120, "[FACEBOOK]": 80, "[INSTAGRAM]": 60}
    sections: list[str] = []
    positions = [(heading, text.find(heading)) for heading in headings]
    positions = [(h, p) for h, p in positions if p >= 0]
    if not positions:
        return text
    positions.sort(key=lambda item: item[1])
    for index, (heading, start) in enumerate(positions):
        content_start = start + len(heading)
        end = positions[index + 1][1] if index + 1 < len(positions) else len(text)
        body = re.sub(r"\n{3,}", "\n\n", text[content_start:end].strip())
        max_words, max_chars = limits[heading]
        if max_words is not None:
            words = re.findall(r"\S+", body)
            if len(words) > max_words:
                body = " ".join(words[:max_words]).rstrip(" ,;:-") + "…"
        if len(body) > max_chars:
            body = _trim_social_copy(body, max_chars)
        sections.append(f"{heading}\n{body}")
    return "\n\n".join(sections).strip()


def _build_social_media_description(prompt: str, template_json: str, image_filename: str, pipeline_item: dict) -> str:
    """Create three separate, platform-specific descriptions for one generated image."""
    safe_prompt = re.sub(r"\s+", " ", str(prompt or "").strip())
    if not safe_prompt:
        safe_prompt = "the generated image"
    template_context = str(template_json or "").strip()[:10000]
    instruction = f"""Create social-media copy for the generated image file '{image_filename}'.

SOURCE CONTENT REQUEST:
{safe_prompt}

TEMPLATE CONTEXT:
{template_context}

Generate EXACTLY four sections with these headings:
[LINKEDIN]
[X / TWITTER]
[FACEBOOK]
[INSTAGRAM]

Requirements:
- The copy must be related to the generated image and the source content request.
- Do not invent company claims, statistics, achievements, prices, dates, people, products or facts that are not present in the source request/template.
- Use natural emojis/icons throughout the copy; do NOT produce plain text only.
- Include a clear call to action.
- Include the supplied platform-specific tag and contact/profile URL in the matching section.
- LinkedIn: professional tone, approximately 120-180 words, maximum 3000 characters. Tag: {SOCIAL_MEDIA_ACCOUNTS['LinkedIn']['tag']} URL: {SOCIAL_MEDIA_ACCOUNTS['LinkedIn']['url']}
- X / Twitter: concise, engaging tone, maximum 280 characters for a standard post, including hashtags and the supplied tag when possible. Tag: {SOCIAL_MEDIA_ACCOUNTS['X / Twitter']['tag']} URL: {SOCIAL_MEDIA_ACCOUNTS['X / Twitter']['url']}
- Facebook: friendly/community tone, approximately 80-120 words. Tag: {SOCIAL_MEDIA_ACCOUNTS['Facebook']['tag']} URL: {SOCIAL_MEDIA_ACCOUNTS['Facebook']['url']}
- Instagram: concise visual-first tone, approximately 60-100 words, maximum 2200 characters. Tag: {SOCIAL_MEDIA_ACCOUNTS['Instagram']['tag']} URL: {SOCIAL_MEDIA_ACCOUNTS['Instagram']['url']}
- Add relevant hashtags to each section.
- Do not add explanations outside the three sections.
"""
    raw = _pipeline_social_text(pipeline_item, instruction)
    # Keep the sections usable even if a provider adds extra whitespace and
    # enforce the hard platform limits before writing the TXT file.
    return _normalize_social_sections(raw)




def _safe_output_name(filename: str) -> str:
    import uuid
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(filename or "generated").stem).strip("_") or "generated"
    return f"{stem}_generated_{uuid.uuid4().hex[:8]}.png"

def _resolve_generation_reference(source_type: str, source: str, filename: str, content_type: str = "") -> tuple[Path, str]:
    import uuid
    source_type = source_type.strip().lower()
    filename = Path(filename or "reference.png").name
    if source_type == "input-folder": path = INPUT_DIR / Path(source).name
    elif source_type == "upload": path = MANUAL_UPLOADS_DIR / Path(source).name
    elif source_type == "google-drive":
        require_drive_configuration(); service = get_drive_service(); path = safe_drive_filename(source, filename)
        if path.exists():
            try: path.unlink()
            except OSError: pass
        download_drive_file(service, source, path)
    elif source_type in {"external-url", "youtube"}:
        if not is_valid_http_url(source): raise HTTPException(status_code=400, detail="A valid HTTP or HTTPS image URL is required.")
        path = UPLOADS_DIR / f"generation_{uuid.uuid4().hex}_{filename}"
        try:
            req = Request(source, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=30) as response: path.write_bytes(response.read())
        except Exception as exc: raise HTTPException(status_code=400, detail=f"Unable to download the reference image: {exc}") from exc
    else: raise HTTPException(status_code=400, detail="Unsupported reference source for image generation.")
    if not path.exists() or not is_valid_image_file(path): raise HTTPException(status_code=400, detail="The selected reference is not a readable image.")
    return path, content_type or get_mime_type(path)


def _prepare_reference_paths(reference_entries: list[tuple[int, Path, str]]) -> list[Path]:
    """Return the original reference files individually.

    Multiple references must stay as separate visual inputs. Do not build a
    contact sheet/collage because that changes the meaning of the references
    and encourages image models to reproduce the references as panels.
    """
    if not reference_entries:
        raise RuntimeError("At least one reference image is required.")
    return [entry[1] for entry in reference_entries]


def _generation_instruction(
    prompt: str,
    template_json: str,
    reference_map: str = "",
) -> str:
    reference_context = (
        f"REFERENCE MAP (the images are separate inputs): {reference_map}\n"
        if reference_map
        else ""
    )
    return f"""Create ONE coherent final image by synthesizing the supplied reference images.

The references are separate source images, NOT a collage, contact sheet, grid, or set of panels.
Never reproduce the references side-by-side, as a grid, as thumbnails, or as multiple frames.
Instead, naturally combine the useful visual characteristics from the references into one final
composition. A reference may contribute a person, object, clothing, pose, product, logo, color
palette, background, lighting, texture, visual style, composition, or another feature. Preserve
identity and important visual details when the user asks for them, but merge the selected
characteristics into a single believable result.

If the user mentions reference numbers, use the corresponding separate reference image(s).
If multiple references are selected, synthesize the requested features from all of them into ONE image.
Do not omit a selected reference merely because another reference is also present.

{reference_context}
USER CONTENT REQUEST:
{prompt}

TEMPLATE CONTEXT:
{template_json.strip()[:12000]}
"""
def _gemini_image(api_key: str, paths: list[Path], instruction: str, model: str | None = None) -> bytes:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    contents = [types.Part.from_text(text=instruction)]

    for path in paths:
        contents.append(
            types.Part.from_bytes(
                data=path.read_bytes(),
                mime_type=get_mime_type(path),
            )
        )

    response = client.models.generate_content(
        model=str(model or "gemini-3.1-flash-image").strip(),
        contents=contents,
        config=types.GenerateContentConfig(response_modalities=["IMAGE"]),
    )

    for part in response.parts or []:
        inline_data = getattr(part, "inline_data", None)
        if inline_data is not None:
            image_data = getattr(inline_data, "data", None)
            if image_data:
                return bytes(image_data)

            image_obj = part.as_image()
            out = io.BytesIO()
            image_obj.convert("RGB").save(out, "PNG")
            return out.getvalue()

    raise RuntimeError(
        "The Gemini API returned no image. This key may not have access to the image-generation model."
    )


def _multipart_image_body(
    paths: list[Path],
    instruction: str,
    model: str,
) -> tuple[bytes, str]:
    """Build an OpenAI-compatible multipart image edit request.

    Repeated image[] fields allow providers that support multiple edit inputs
    to receive every reference independently instead of receiving a collage.
    """
    import mimetypes

    boundary = "----ImageGeneratorBoundary"
    body = bytearray()

    def field(name: str, value: str) -> None:
        body.extend(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode()
        )

    field("model", model)
    field("prompt", instruction)

    for index, path in enumerate(paths, start=1):
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        body.extend(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="image[]"; '
                f'filename="reference_{index}.png"\r\n'
                f"Content-Type: {mime}\r\n\r\n"
            ).encode()
        )
        body.extend(path.read_bytes())
        body.extend(b"\r\n")

    body.extend(f"--{boundary}--\r\n".encode())
    return bytes(body), boundary


def _openai_image(api_key: str, paths: list[Path], instruction: str, model: str | None = None) -> bytes:
    body, boundary = _multipart_image_body(paths, instruction, str(model or "gpt-image-2").strip())
    req = Request(
        "https://api.openai.com/v1/images/edits",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    try:
        with urlopen(req, timeout=180) as response:
            payload = json.loads(response.read().decode())
    except Exception as exc:
        raise RuntimeError(f"OpenAI image generation failed: {exc}") from exc

    item = (payload.get("data") or [None])[0]
    if not item:
        raise RuntimeError("OpenAI returned no image output.")
    if item.get("b64_json"):
        return base64.b64decode(item["b64_json"])
    if item.get("url"):
        with urlopen(item["url"], timeout=60) as response:
            return response.read()
    raise RuntimeError("OpenAI returned no image data.")


def _generic_image(item: dict, paths: list[Path], instruction: str) -> bytes:
    base_url = _provider_base_url(item)
    model = _provider_image_model(item)
    if not base_url or not model:
        raise _generic_compatible_error(item)

    body, boundary = _multipart_image_body(paths, instruction, model)
    request = Request(
        f"{base_url.rstrip('/')}/images/edits",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {str(item.get('value', '')).strip()}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    try:
        with urlopen(request, timeout=240) as response:
            payload = json.loads(response.read().decode())
    except Exception as exc:
        detail = str(exc)
        if hasattr(exc, "read"):
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
        raise RuntimeError(
            f"{item.get('display_name', 'API')} image generation failed: {detail}"
        ) from exc

    data = payload.get("data") or []
    if not data:
        raise RuntimeError(
            f"{item.get('display_name', 'API')} returned no image output."
        )

    first = data[0]
    if first.get("b64_json"):
        return base64.b64decode(first["b64_json"])
    if first.get("url"):
        with urlopen(first["url"], timeout=120) as response:
            return response.read()
    raise RuntimeError(
        f"{item.get('display_name', 'API')} returned no image data."
    )


def _pipeline_image(
    pipeline_item: dict,
    reference_paths: list[Path],
    instruction: str,
) -> tuple[bytes, str]:
    """Generate one image from multiple independent reference inputs."""
    service = str(pipeline_item.get("service") or "").strip().lower()
    key = str(pipeline_item.get("value", "")).strip()

    if not reference_paths:
        raise RuntimeError("At least one reference image is required.")

    if not _pipeline_key_is_usable(pipeline_item, "image"):
        raise RuntimeError(
            f"{pipeline_item.get('display_name', 'Selected API')} does not provide "
            "image-generation capability."
        )

    if service == "gemini":
        return (
            _gemini_image(key, reference_paths, instruction, _provider_image_model(pipeline_item)),
            _provider_image_model(pipeline_item),
        )

    if service == "openrouter":
        model = _provider_image_model(pipeline_item)
        return (
            _openrouter_image(key, reference_paths, instruction, model),
            model,
        )

    if service == "openai":
        model = _provider_image_model(pipeline_item)
        return _openai_image(key, reference_paths, instruction, model), model

    model = _provider_image_model(pipeline_item)
    return _generic_image(pipeline_item, reference_paths, instruction), model


def _openrouter_image(
    api_key: str,
    paths: list[Path],
    instruction: str,
    model: str | None = None,
) -> bytes:
    selected_model = str(model or OPENROUTER_IMAGE_MODEL).strip()
    if not selected_model:
        raise RuntimeError(
            "No OpenRouter image model is configured for the selected API key."
        )

    payload = {
        "model": selected_model,
        "prompt": instruction,
        "input_references": [
            {
                "type": "image_url",
                "image_url": {"url": _image_data_url(path)},
            }
            for path in paths
        ],
        "output_format": "png",
    }

    result = _openrouter_request(
        api_key,
        "images",
        payload,
        timeout=240,
    )
    data = result.get("data") or []
    if not data:
        raise RuntimeError("OpenRouter returned no image output.")

    first = data[0]
    if first.get("b64_json"):
        return base64.b64decode(first["b64_json"])
    if first.get("url"):
        with urlopen(first["url"], timeout=120) as response:
            return response.read()

    raise RuntimeError("OpenRouter returned no image data.")


def _create_editable_template_from_generated_image(
    image_path: Path,
    prompt: str,
) -> dict:
    """Analyze the newly generated image and create an editable PPTX template.

    The PNG/JPG remains the visual output. A second AI/text-vision pass analyzes
    that exact generated image, writes a template JSON artifact, and converts
    the template into a PPTX whose supported elements can be edited in Canva.
    """
    text_candidates = _selected_stage_candidates("text")
    if not text_candidates:
        raise RuntimeError(
            "The image was generated, but no selected text/vision-capable API key "
            "is available to create its editable template."
        )

    errors: list[str] = []
    template_result: dict | None = None
    template_pipeline_item: dict | None = None

    for candidate_id, candidate_item in text_candidates:
        try:
            template_result = _pipeline_template(
                candidate_item,
                reference_path=image_path,
            )
            template_pipeline_item = candidate_item
            API_KEY_STATE["pipeline_key_id"] = candidate_id
            break
        except Exception as exc:
            provider_name = str(candidate_item.get("display_name") or candidate_id)
            errors.append(f"{provider_name}: {exc}")

    if template_result is None:
        raise RuntimeError(
            "Unable to create an editable template from the generated image. "
            + " | ".join(errors)
        )

    template = template_result.get("template")
    if not isinstance(template, dict):
        raise RuntimeError("The AI template generator returned no valid template object.")

    template["source"] = {
        "type": "generated-image",
        "filename": image_path.name,
        "prompt": prompt,
    }

    template_id = str(template.get("template_id") or "")
    if not template_id:
        import uuid
        template_id = uuid.uuid4().hex
        template["template_id"] = template_id

    json_path = EDITABLE_DESIGNS_DIR / f"{template_id}.json"
    json_path.write_text(
        json.dumps(template, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    pptx_path = EDITABLE_DESIGNS_DIR / f"{template_id}.pptx"
    build_editable_pptx(
        template=template,
        source_image=image_path,
        output_path=pptx_path,
    )

    metadata = _pipeline_metadata(template_pipeline_item or {}, "text")
    return {
        "template_id": template_id,
        "template_filename": json_path.name,
        "editable_design_filename": pptx_path.name,
        "editable_design_mime_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "template": template,
        **metadata,
    }


@app.post("/api/images/generate")
async def generate_output_image(
    source_type: str = Form(...),
    source: str = Form(...),
    filename: str = Form("reference.png"),
    content_type: str = Form(""),
    prompt: str = Form(...),
    template_json: str = Form("{}"),
    references_json: str = Form(""),
):
    if not prompt.strip():
        raise HTTPException(
            status_code=400,
            detail="Enter a content prompt before generating the output image.",
        )

    # Do not force one selected API to perform the entire pipeline.
    # For example, Claude can handle template/prompt analysis while Gemini,
    # OpenAI, OpenRouter, or a configured custom provider handles the image.
    image_candidates = _selected_stage_candidates("image")
    if not image_candidates:
        raise HTTPException(
            status_code=400,
            detail=_stage_capability_message(
                "image",
                API_KEY_STATE.get("selected_ids", []),
            ),
        )

    # Resolve every selected reference. Google Drive IDs are downloaded by the
    # backend, so the browser never needs to send image bytes or Drive URLs.
    reference_entries: list[tuple[int, Path, str]] = []
    if references_json.strip():
        try:
            raw_references = json.loads(references_json)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="Invalid references_json payload.") from exc

        if not isinstance(raw_references, list) or not raw_references:
            raise HTTPException(status_code=400, detail="At least one reference image is required.")

        for position, item in enumerate(raw_references, start=1):
            if not isinstance(item, dict):
                raise HTTPException(status_code=400, detail="Invalid reference entry.")
            item_number = int(item.get("number") or position)
            item_source_type = str(item.get("source_type") or "").strip().lower()
            item_source = str(item.get("source") or "").strip()
            item_filename = str(item.get("filename") or f"reference_{item_number}.png")
            item_content_type = str(item.get("content_type") or "")
            if not item_source:
                raise HTTPException(status_code=400, detail=f"Reference {item_number} has no source.")
            path, _ = _resolve_generation_reference(
                item_source_type, item_source, item_filename, item_content_type
            )
            reference_entries.append((item_number, path, item_filename))
    else:
        reference_path, _ = _resolve_generation_reference(
            source_type, source, filename, content_type
        )
        reference_entries = [(1, reference_path, filename)]

    reference_paths = _prepare_reference_paths(reference_entries)
    reference_map = "; ".join(
        f"{number} = {name}" for number, _, name in reference_entries
    )
    instruction = _generation_instruction(
        prompt,
        template_json,
        reference_map=reference_map,
    )

    errors: list[str] = []
    key_id = ""
    item: dict = {}
    image_bytes: bytes | None = None
    image_model = ""

    # Try each selected image-capable credential once. This means a quota-limited
    # Gemini key does not prevent a second selected image provider from working.
    for candidate_id, candidate_item in image_candidates:
        try:
            key_id = candidate_id
            item = candidate_item
            image_bytes, image_model = _pipeline_image(
                candidate_item,
                reference_paths,
                instruction,
            )
            API_KEY_STATE["pipeline_key_id"] = candidate_id
            break
        except Exception as exc:
            provider_name = str(
                candidate_item.get("display_name", candidate_id)
            )
            if _exception_is_quota_error(exc):
                errors.append(
                    f"{provider_name}: quota/rate limit exceeded ({exc})"
                )
            else:
                errors.append(f"{provider_name}: {exc}")

    if image_bytes is None:
        if errors and all(_exception_is_quota_error(RuntimeError(error)) for error in errors):
            raise HTTPException(
                status_code=429,
                detail=(
                    "All selected image-capable API keys are currently unavailable "
                    "because of quota or rate limits. " + " | ".join(errors)
                ),
            )

        raise HTTPException(
            status_code=502,
            detail=(
                "Image generation failed for all selected image-capable API keys. "
                + " | ".join(errors)
            ),
        )

    output_name = _safe_output_name(filename)
    generated_image_path = IMAGE_OUTPUT_DIR / output_name
    generated_image_path.write_bytes(image_bytes)

    try:
        editable_template = _create_editable_template_from_generated_image(
            generated_image_path,
            prompt,
        )
    except Exception as exc:
        # Keep the generated image available, but explicitly report that the
        # required editable-template stage failed. Canva will not be opened
        # from the frontend unless an editable PPTX was produced.
        raise HTTPException(
            status_code=502,
            detail=(
                "Image generation succeeded, but editable template generation "
                f"failed: {exc}"
            ),
        ) from exc

    return {
        "success": True,
        "image_url": f"/api/images/output/{quote(output_name)}",
        "filename": output_name,
        "model": image_model,
        "provider": item.get("display_name", "Selected API"),
        "api_id": key_id,
        "pipeline_api_id": key_id,
        "selected_api_count": len(API_KEY_STATE.get("selected_ids", [])),
        "changes": {},
        "editable_template": editable_template,
    }


@app.post("/api/canva/create-from-generated-template")
async def canva_create_from_generated_template(
    filename: str = Form(...),
):
    """Import the AI-generated editable PPTX directly into Canva.

    This endpoint intentionally imports PPTX instead of sending the generated
    PNG through Canva's image-to-design/Magic Layers flow.
    """
    safe_name = Path(filename).name
    if not safe_name or Path(safe_name).suffix.lower() != ".pptx":
        raise HTTPException(
            status_code=400,
            detail="An editable PPTX template filename is required.",
        )

    local_path = EDITABLE_DESIGNS_DIR / safe_name
    if not local_path.exists() or not local_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="The generated editable PPTX template was not found on the server.",
        )

    try:
        result = await canva_connect_service.import_design_from_local_file(local_path)
        result["editable_source"] = "ai-generated-pptx-template"
        result["magic_layers_required"] = False
        result["source_file"] = safe_name
        return result
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Unable to import the generated editable template into Canva: {exc}",
        ) from exc


@app.get("/api/images/output/{filename}")
def get_generated_image(filename: str):
    path=IMAGE_OUTPUT_DIR/Path(filename).name
    if not path.exists(): raise HTTPException(status_code=404, detail="Generated image was not found.")
    return FileResponse(path,media_type="image/png",filename=path.name)


@app.post("/api/social-media/generate")
async def generate_social_media_description(
    filename: str = Form(...),
    prompt: str = Form(""),
    template_json: str = Form("{}"),
):
    """Generate the optional LinkedIn/Facebook/Instagram TXT file after image generation."""
    safe_name = Path(filename).name
    if not safe_name:
        raise HTTPException(status_code=400, detail="Generated image filename is required.")

    image_path = IMAGE_OUTPUT_DIR / safe_name
    if not image_path.exists() or not image_path.is_file():
        raise HTTPException(status_code=404, detail="Generated image was not found on the server.")

    key_id, pipeline_item = _require_pipeline_key()
    try:
        content = _build_social_media_description(
            prompt,
            template_json,
            safe_name,
            pipeline_item,
        )
        description_name = _safe_social_media_name(safe_name)
        description_path = IMAGE_OUTPUT_DIR / description_name
        description_path.write_text(content, encoding="utf-8")
        return {
            "success": True,
            "filename": safe_name,
            "social_media_filename": description_name,
            "social_media_file_url": f"/api/social-media/output/{quote(description_name)}",
            "provider": pipeline_item.get("display_name", "Selected API"),
            "model": _provider_text_model(pipeline_item),
            "api_id": key_id,
            "word_count": _word_count(content),
            "content": content,
            "message": "Social-media description file generated successfully.",
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Unable to generate the social-media description: {exc}") from exc


@app.get("/api/social-media/output/{filename}")
def get_social_media_file(filename: str):
    path = IMAGE_OUTPUT_DIR / Path(filename).name
    if not path.exists() or not path.is_file() or path.suffix.lower() != ".txt":
        raise HTTPException(status_code=404, detail="Social-media text file was not found.")
    return FileResponse(path, media_type="text/plain; charset=utf-8", filename=path.name)


@app.post("/api/social-media/save-to-drive")
def save_social_media_description_to_drive(filename: str = Form(...)):
    """Save only the generated social-media TXT file into Google Drive/outputs."""
    require_drive_configuration()
    safe_name = Path(filename).name
    if not safe_name or not safe_name.lower().endswith(".txt"):
        raise HTTPException(status_code=400, detail="A valid social-media description filename is required.")

    local_path = IMAGE_OUTPUT_DIR / safe_name
    if not local_path.exists() or not local_path.is_file():
        raise HTTPException(status_code=404, detail="The social-media description file was not found on the server.")

    try:
        service = get_drive_service()
        parent_folder_id = resolve_drive_folder_id(
            service,
            normalize_drive_folder_id(API_KEY_STATE.get("drive_folder_id", "")),
            str(API_KEY_STATE.get("drive_folder_name", "") or ""),
        )
        outputs_folder_id = ensure_drive_outputs_folder(service, parent_folder_id)
        API_KEY_STATE["drive_output_folder_id"] = outputs_folder_id
        API_KEY_STATE["drive_output_folder_name"] = "outputs"
        persist_drive_configuration()

        escaped_name = safe_name.replace(chr(39), chr(92) + chr(39))
        existing = service.files().list(
            q=(f"'{outputs_folder_id}' in parents "
               f"and name = '{escaped_name}' "
               "and trashed = false"),
            pageSize=10,
            fields="files(id,name,webViewLink)",
        ).execute().get("files", [])

        media = MediaIoBaseUpload(
            io.BytesIO(local_path.read_bytes()),
            mimetype="text/plain",
            resumable=False,
        )
        if existing:
            drive_file = service.files().update(
                fileId=existing[0]["id"],
                media_body=media,
                fields="id,name,webViewLink",
            ).execute()
        else:
            drive_file = service.files().create(
                body={"name": safe_name, "parents": [outputs_folder_id]},
                media_body=media,
                fields="id,name,webViewLink",
            ).execute()

        return {
            "success": True,
            "filename": safe_name,
            "drive_file_id": drive_file.get("id", ""),
            "drive_folder": "outputs",
            "drive_url": drive_file.get("webViewLink", ""),
            "message": "Social-media description saved to Google Drive/outputs.",
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Unable to save the social-media description to Google Drive: {exc}") from exc

@app.get("/api/drive/folders")
def get_configured_drive_folders():
    """
    Return only Drive folders accessible by the current OAuth account.
    Stale IDs can be recovered by exact configured folder name.
    """
    folders = API_KEY_STATE.get("drive_output_folders", [])

    if not isinstance(folders, list) or not folders:
        load_persisted_drive_configuration()
        folders = API_KEY_STATE.get("drive_output_folders", [])

    if not isinstance(folders, list):
        folders = []

    result = []

    try:
        service = get_drive_service()

        for index, folder in enumerate(folders, start=1):
            if not isinstance(folder, dict):
                continue

            folder_id = normalize_drive_folder_id(
                str(folder.get("id", "") or "")
            )
            folder_name = str(
                folder.get("name", "") or ""
            ).strip()

            if not folder_id and not folder_name:
                continue

            try:
                resolved_id = resolve_drive_folder_id(
                    service,
                    folder_id,
                    folder_name,
                )
                metadata = _drive_folder_metadata(
                    service,
                    resolved_id,
                )

                resolved_name = str(
                    metadata.get("name")
                    or folder_name
                    or f"Google Drive Output {index}"
                ).strip()

                folder["id"] = resolved_id
                folder["name"] = resolved_name

                result.append(
                    {
                        "id": resolved_id,
                        "name": resolved_name,
                        "label": resolved_name
                        or f"Google Drive Output {index}",
                    }
                )

            except Exception as exc:
                print(
                    f"Skipping inaccessible configured Drive folder "
                    f"{folder_id or folder_name}: {exc}"
                )

        API_KEY_STATE["drive_output_folders"] = [
            {"id": item["id"], "name": item["name"]}
            for item in result
        ]

        if result:
            API_KEY_STATE["drive_folder_id"] = result[0]["id"]
            API_KEY_STATE["drive_folder_name"] = result[0]["name"]

        persist_drive_configuration()

    except HTTPException:
        raise
    except Exception as exc:
        import traceback
        print()
        print("=" * 80)
        print("GOOGLE DRIVE FOLDER LIST FAILED")
        print("=" * 80)
        traceback.print_exc()
        print("=" * 80)
        print()
        raise HTTPException(
            status_code=500,
            detail=(
                "Unable to load configured Google Drive output folders: "
                f"{type(exc).__name__}: {exc}"
            ),
        ) from exc

    return {"folders": result}


@app.post("/api/images/save-to-drive")
def save_generated_image_to_drive(
    filename: str = Form(...),
    folder_id: str = Form(""),
):
    """
    Save a generated local image into the outputs folder.

    The selected parent folder is validated against the currently
    authorized Google account before outputs is accessed.
    """
    require_drive_configuration()

    safe_name = Path(filename).name
    if not safe_name:
        raise HTTPException(
            status_code=400,
            detail="Generated image filename is required.",
        )

    local_path = IMAGE_OUTPUT_DIR / safe_name
    if not local_path.exists() or not local_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="Generated image was not found on the server.",
        )

    try:
        service = get_drive_service()
        requested_folder_id = normalize_drive_folder_id(folder_id)

        if not requested_folder_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Select one of the configured Google Drive output "
                    "folders before saving."
                ),
            )

        configured_outputs = (
            API_KEY_STATE.get("drive_output_folders", []) or []
        )

        allowed_ids = {
            normalize_drive_folder_id(str(item.get("id", "") or ""))
            for item in configured_outputs
            if isinstance(item, dict) and item.get("id")
        }

        if requested_folder_id not in allowed_ids:
            raise HTTPException(
                status_code=400,
                detail=(
                    "The selected Google Drive output folder is not "
                    "currently configured. Refresh the Google Drive "
                    "folders and select an accessible folder."
                ),
            )

        resolved_parent_id, resolved_parent_name = (
            resolve_configured_drive_folder(
                service,
                requested_folder_id,
                configured_outputs,
                label="Google Drive output folder",
            )
        )

        outputs_folder_id = ensure_drive_outputs_folder(
            service,
            resolved_parent_id,
            resolved_parent_name,
        )

        API_KEY_STATE["drive_folder_id"] = resolved_parent_id
        API_KEY_STATE["drive_folder_name"] = resolved_parent_name
        API_KEY_STATE["drive_output_folder_id"] = outputs_folder_id
        API_KEY_STATE["drive_output_folder_name"] = "outputs"

        # Replace a stale configured ID with the current account's ID.
        for item in configured_outputs:
            if not isinstance(item, dict):
                continue
            if normalize_drive_folder_id(
                str(item.get("id", "") or "")
            ) == requested_folder_id:
                item["id"] = resolved_parent_id
                item["name"] = resolved_parent_name

        persist_drive_configuration()

        escaped_name = safe_name.replace(
            chr(39),
            chr(92) + chr(39),
        )

        existing = (
            service.files()
            .list(
                q=(
                    f"'{outputs_folder_id}' in parents "
                    f"and name = '{escaped_name}' "
                    "and trashed = false"
                ),
                pageSize=10,
                fields="files(id,name,webViewLink)",
            )
            .execute()
            .get("files", [])
        )

        image_bytes = local_path.read_bytes()
        if not image_bytes:
            raise HTTPException(
                status_code=400,
                detail="The generated image file is empty.",
            )

        media = MediaIoBaseUpload(
            io.BytesIO(image_bytes),
            mimetype="image/png",
            resumable=False,
        )

        if existing:
            drive_file = (
                service.files()
                .update(
                    fileId=existing[0]["id"],
                    media_body=media,
                    fields="id,name,webViewLink",
                )
                .execute()
            )
        else:
            drive_file = (
                service.files()
                .create(
                    body={
                        "name": safe_name,
                        "parents": [outputs_folder_id],
                    },
                    media_body=media,
                    fields="id,name,webViewLink",
                )
                .execute()
            )

        return {
            "success": True,
            "filename": safe_name,
            "drive_file_id": drive_file.get("id", ""),
            "drive_folder": "outputs",
            "parent_folder_id": resolved_parent_id,
            "drive_url": drive_file.get("webViewLink", ""),
            "message": (
                "Generated image saved to the selected "
                "Google Drive/outputs folder."
            ),
        }

    except HTTPException:
        raise
    except Exception as exc:
        import traceback
        print()
        print("=" * 80)
        print("SAVE TO GOOGLE DRIVE: ERROR")
        print("=" * 80)
        print(f"Error type    : {type(exc).__name__}")
        print(f"Error message : {exc}")
        print(f"Filename      : {safe_name}")
        print(f"Folder ID     : {folder_id}")
        traceback.print_exc()
        print("=" * 80)
        print()

        raise HTTPException(
            status_code=500,
            detail=(
                "Unable to save generated image to Google Drive: "
                f"{type(exc).__name__}: {exc}"
            ),
        ) from exc



@app.post(
    "/api/prompts/generate"
)
async def generate_prompt_with_ai(
    source_type: str =
        Form(...),

    source: str =
        Form(...),

    filename: str =
        Form("reference"),

    content_type: str =
        Form(""),
):

    source_type = (
        source_type
        .strip()
        .lower()
    )

    source = source.strip()

    key_id, pipeline_item = _require_pipeline_key("text")

    def run_prompt(**kwargs):
        return _pipeline_prompt(pipeline_item, **kwargs)

    filename = Path(
        filename
    ).name

    if not source:

        raise HTTPException(
            status_code=400,
            detail="Reference source is empty.",
        )

    if source_type not in {
        "input-folder",
        "google-drive",
        "upload",
        "external-url",
        "youtube",
    }:

        raise HTTPException(
            status_code=400,
            detail="Unsupported reference source.",
        )

    if (
        source_type ==
        "input-folder"
    ):

        reference_path = (
            INPUT_DIR /
            Path(source).name
        )

        if (
            not reference_path.exists()
            or not reference_path.is_file()
        ):

            raise HTTPException(
                status_code=404,
                detail=(
                    "Selected input reference "
                    "was not found."
                ),
            )

        if (
            reference_path.suffix.lower()
            not in IMAGE_EXTENSIONS
        ):

            raise HTTPException(
                status_code=400,
                detail=(
                    "AI prompt generation supports "
                    "images and GIFs only."
                ),
            )

        try:

            prompt = (
                run_prompt(
                    image_path=
                        reference_path,
                    filename=
                        reference_path.name,
                    content_type=
                        get_mime_type(
                            reference_path
                        ),
                )
            )

            return {
                "success": True,
                "prompt": prompt,
                **_pipeline_metadata(pipeline_item, "text"),
                "api_id": key_id,
            }

        except ValueError as exc:

            raise HTTPException(
                status_code=400,
                detail=str(exc),
            ) from exc

        except Exception as exc:

            print(
                "AI prompt generation failed:",
                repr(exc),
            )

            raise HTTPException(
                status_code=500,
                detail=(
                    "AI prompt generation failed: "
                    f"{exc}"
                ),
            ) from exc

    if source_type == "upload":
        reference_path = MANUAL_UPLOADS_DIR / Path(source).name

        if not reference_path.exists() or not reference_path.is_file():
            raise HTTPException(
                status_code=404,
                detail="Manual uploaded reference was not found.",
            )

        if reference_path.suffix.lower() not in IMAGE_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail="AI prompt generation supports images and GIFs only.",
            )

        try:
            prompt = run_prompt(
                image_path=reference_path,
                filename=reference_path.name,
                content_type=get_mime_type(reference_path),
            )
            return {
                "success": True,
                "prompt": prompt,
                **_pipeline_metadata(pipeline_item, "text"),
                "api_id": key_id,
            }
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"AI prompt generation failed: {exc}",
            ) from exc

    if source_type == "google-drive":
        require_drive_configuration()

        try:
            service = get_drive_service()
            cache_path = safe_drive_filename(
                source,
                filename,
            )

            # Do not reuse an old cached Drive response.
            if cache_path.exists():
                try:
                    cache_path.unlink()
                except OSError:
                    pass

            download_drive_file(
                service,
                source,
                cache_path,
            )

            prompt = run_prompt(
                image_path=cache_path,
                filename=filename,
                content_type=(
                    content_type
                    or get_mime_type(cache_path)
                ),
            )

            return {
                "success": True,
                "prompt": prompt,
                **_pipeline_metadata(pipeline_item, "text"),
                "api_id": key_id,
            }

        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=str(exc),
            ) from exc

        except Exception as exc:
            print(
                "Google Drive AI prompt generation failed:",
                repr(exc),
            )

            raise HTTPException(
                status_code=500,
                detail=(
                    "Google Drive AI prompt generation failed: "
                    f"{exc}"
                ),
            ) from exc

    if not is_valid_http_url(
        source
    ):

        raise HTTPException(
            status_code=400,
            detail=(
                "A valid HTTP or HTTPS URL is required."
            ),
        )

    try:

        prompt = (
            run_prompt(
                image_url=source,
                filename=filename,
                content_type=
                    content_type or None,
                is_youtube=(
                    source_type ==
                    "youtube"
                ),
            )
        )

        return {
            "success": True,
            "prompt": prompt,
        }

    except ValueError as exc:

        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    except Exception as exc:

        print(
            "AI prompt generation failed:",
            repr(exc),
        )

        raise HTTPException(
            status_code=500,
            detail=(
                "AI prompt generation failed: "
                f"{exc}"
            ),
        ) from exc


# -------------------------------------------------------------------
# Generate template automatically from reference
# -------------------------------------------------------------------

@app.post(
    "/api/templates/generate"
)
async def generate_template_endpoint(
    source_type: str =
        Form(...),

    source: str =
        Form(...),

    filename: str =
        Form("reference"),

    content_type: str =
        Form(""),
):

    source_type = (
        source_type
        .strip()
        .lower()
    )

    source = source.strip()

    filename = Path(
        filename
    ).name

    if not source:

        raise HTTPException(
            status_code=400,
            detail="Reference source is empty.",
        )

    if source_type not in {
        "input-folder",
        "google-drive",
        "upload",
        "external-url",
        "youtube",
    }:

        raise HTTPException(
            status_code=400,
            detail="Unsupported reference source.",
        )

    key_id, pipeline_item = _require_pipeline_key("text")

    def run_template(**kwargs):
        return _pipeline_template(pipeline_item, **kwargs)

    def run_template_url(**kwargs):
        if pipeline_item.get("service") == "openrouter":
            # External URL/YouTube references are downloaded by the existing
            # helper only for Gemini. For OpenRouter, use its downloaded local
            # reference path after resolving the URL below.
            raise RuntimeError("OpenRouter external template references must be resolved to a local image first.")
        return _with_pipeline_key(
            pipeline_item,
            lambda: generate_template_from_url(**kwargs),
        )

    try:

        if (
            source_type ==
            "input-folder"
        ):

            reference_path = (
                INPUT_DIR /
                Path(source).name
            )

            if (
                not reference_path.exists()
                or not reference_path.is_file()
            ):

                raise HTTPException(
                    status_code=404,
                    detail=(
                        "Selected input reference "
                        "was not found."
                    ),
                )

            if (
                reference_path.suffix.lower()
                not in IMAGE_EXTENSIONS
            ):

                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Automatic template generation "
                        "currently supports images "
                        "and GIFs only."
                    ),
                )

            if (
                reference_path.suffix.lower()
                == ".gif"
            ):

                from PIL import Image

                with Image.open(
                    reference_path
                ) as image:

                    image.seek(0)

                    frame = (
                        image.convert("RGB")
                    )

                    frame_path = (
                        UPLOADS_DIR /
                        f"{reference_path.stem}_frame.png"
                    )

                    frame.save(
                        frame_path,
                        format="PNG",
                    )

                    result = (
                        run_template(
                            reference_path=
                                frame_path,
                            prompt="",
                        )
                    )

            else:

                result = (
                    run_template(
                        reference_path=
                            reference_path,
                        prompt="",
                    )
                )

            return {
                "success": True,
                "template": result,
                **_pipeline_metadata(pipeline_item, "text"),
                "api_id": key_id,
            }


        if source_type == "upload":
            reference_path = MANUAL_UPLOADS_DIR / Path(source).name

            if not reference_path.exists() or not reference_path.is_file():
                raise HTTPException(
                    status_code=404,
                    detail="Manual uploaded reference was not found.",
                )

            if reference_path.suffix.lower() not in IMAGE_EXTENSIONS:
                raise HTTPException(
                    status_code=400,
                    detail="Automatic template generation currently supports images and GIFs only.",
                )

            if reference_path.suffix.lower() == ".gif":
                from PIL import Image

                with Image.open(reference_path) as image:
                    image.seek(0)
                    frame = image.convert("RGB")
                    frame_path = UPLOADS_DIR / f"{reference_path.stem}_frame.png"
                    frame.save(frame_path, format="PNG")
                    result = run_template(
                        reference_path=frame_path,
                        prompt="",
                    )
            else:
                result = run_template(
                    reference_path=reference_path,
                    prompt="",
                )

            return {
                "success": True,
                "template": result,
                **_pipeline_metadata(pipeline_item, "text"),
                "api_id": key_id,
            }

        if source_type == "google-drive":
            require_drive_configuration()

            try:
                cache_path = safe_drive_filename(
                    source,
                    filename,
                )

                # Do not reuse an old cached Drive response.
                if cache_path.exists():
                    try:
                        cache_path.unlink()
                    except OSError:
                        pass

                service = get_drive_service()
                download_drive_file(
                    service,
                    source,
                    cache_path,
                )

                if (
                    cache_path.suffix.lower()
                    == ".gif"
                ):
                    from PIL import Image

                    with Image.open(
                        cache_path
                    ) as image:
                        image.seek(0)

                        frame = image.convert(
                            "RGB"
                        )

                        frame_path = (
                            UPLOADS_DIR /
                            f"{cache_path.stem}_frame.png"
                        )

                        frame.save(
                            frame_path,
                            format="PNG",
                        )

                        result = run_template(
                            reference_path=frame_path,
                            prompt="",
                        )
                else:
                    result = run_template(
                        reference_path=cache_path,
                        prompt="",
                    )

                return {
                    "success": True,
                    "template": result,
                    **_pipeline_metadata(pipeline_item, "text"),
                }

            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=str(exc),
                ) from exc

            except Exception as exc:
                print(
                    "Google Drive template generation failed:",
                    repr(exc),
                )

                raise HTTPException(
                    status_code=500,
                    detail=(
                        "Google Drive template generation failed: "
                        f"{exc}"
                    ),
                ) from exc

        if not is_valid_http_url(
            source
        ):

            raise HTTPException(
                status_code=400,
                detail=(
                    "A valid HTTP or HTTPS URL is required."
                ),
            )

        result = (
            run_template_url(
                url=source,
                uploads_dir=
                    UPLOADS_DIR,
                filename=filename,
                content_type=
                    content_type,
                is_youtube=(
                    source_type ==
                    "youtube"
                ),
            )
        )

        return {
            "success": True,
            "template": result,
            **_pipeline_metadata(pipeline_item, "text"),
        }


    except HTTPException:
        raise

    except ValueError as exc:

        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    except Exception as exc:

        print(
            "Automatic template generation failed:",
            repr(exc),
        )

        raise HTTPException(
            status_code=500,
            detail=(
                "Automatic template generation failed: "
                f"{exc}"
            ),
        ) from exc

# -------------------------------------------------------------------
# Outermost CORS wrapper
# -------------------------------------------------------------------
# Wrapping the complete FastAPI application ensures CORS headers are also
# present when an unexpected exception escapes the route/middleware stack.
# Without this, the browser can report a misleading CORS error for a real
# backend 500 response.
app = CORSMiddleware(
    app,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "https://frontend-production-14d5.up.railway.app",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
