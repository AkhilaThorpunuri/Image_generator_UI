from pathlib import Path
import io
import json
import base64
import os
import re
from urllib.parse import quote
from urllib.request import Request, urlopen

from fastapi import (
FastAPI,
File,
Form,
HTTPException,
UploadFile,
Body,
)
from app.services.canva_connect_service import canva_connect_service
from fastapi.responses import (
FileResponse,
StreamingResponse,
HTMLResponse,
)

from fastapi.middleware.cors import (
CORSMiddleware,
)
from pydantic import BaseModel

from fastapi.responses import (
FileResponse,
StreamingResponse,
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

-------------------------------------------------------------------

Application

-------------------------------------------------------------------

app = FastAPI(
title="Image Generator API",
version="1.0.0",
)

-------------------------------------------------------------------

Directories

-------------------------------------------------------------------

BASE_DIR = (
Path(file)
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

-------------------------------------------------------------------

API key setup / in-memory session

-------------------------------------------------------------------

DRIVE_OAUTH_KEY_ID = "GOOGLE_DRIVE_OAUTH"
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
CREDENTIALS_FILE = BASE_DIR / "credentials.json"
TOKEN_FILE = BASE_DIR / "token.json"
DRIVE_CONFIG_FILE = BASE_DIR / "drive_config.json"

Railway deployment support:

Keep the existing local credentials.json/token.json flow, but also allow

the same OAuth files to be supplied securely through environment variables.

This avoids committing Google OAuth secrets to Git.

DRIVE_CREDENTIALS_ENV = "GOOGLE_DRIVE_CREDENTIALS_JSON_B64"
DRIVE_TOKEN_ENV = "GOOGLE_DRIVE_TOKEN_JSON_B64"

Backward-compatible names supported for existing local/deployment setups.

DRIVE_CREDENTIALS_ENV_LEGACY = "GOOGLE_OAUTH_CREDENTIALS_JSON"
DRIVE_TOKEN_ENV_LEGACY = "GOOGLE_OAUTH_TOKEN_JSON"
DRIVE_FOLDER_ENV = "GOOGLE_DRIVE_FOLDER_ID"
DRIVE_OUTPUT_FOLDER_ENV = "GOOGLE_DRIVE_OUTPUT_FOLDER_ID"

API_KEY_STATE = {
"keys": {},
"selected_ids": [],
"pipeline_key_id": "",
"config": {},
"drive_folder_id": "",
"drive_folder_name": "",
"drive_output_folder_id": "",
"drive_output_folder_name": "outputs",
"gemini_model": "gemini-3.5-flash-lite",
}

def persist_drive_configuration() -> None:
"""
Persist only non-secret Google Drive configuration.

API keys and OAuth tokens are never written here. This file only keeps
the Drive reference/output folder configuration so a FastAPI restart
does not make the configured Drive references disappear.
"""
payload = {
    "drive_folder_id": normalize_drive_folder_id(
        API_KEY_STATE.get("drive_folder_id", "")
    ),
    "drive_folder_name": str(
        API_KEY_STATE.get("drive_folder_name", "")
    ).strip(),
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
    # Railway provides non-secret Drive folder configuration as service
    # variables. Keep drive_config.json authoritative when it exists, but
    # use these variables on a fresh deployment.
    env_folder_id = normalize_drive_folder_id(
        str(os.getenv(DRIVE_FOLDER_ENV, "") or "")
    )
    env_output_folder_id = str(
        os.getenv(DRIVE_OUTPUT_FOLDER_ENV, "") or ""
    ).strip()
    if env_folder_id:
        API_KEY_STATE["drive_folder_id"] = env_folder_id
    if env_output_folder_id:
        API_KEY_STATE["drive_output_folder_id"] = env_output_folder_id
    if env_folder_id or env_output_folder_id:
        API_KEY_STATE["drive_output_folder_name"] = "outputs"
        print("Loaded Google Drive folder configuration from environment.")
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

    if folder_id or folder_name:
        API_KEY_STATE["drive_folder_id"] = folder_id
        API_KEY_STATE["drive_folder_name"] = folder_name

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
"",
name.strip().upper(),
).strip("")

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
while f"{base}{index}" in values:
index += 1
return f"{base}{index}"

def logical_api_service(key_name: str) -> str:
normalized = normalize_key_name(key_name)
base = re.sub(r"_\d+$", "", normalized)
if base in {"GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_AI_API_KEY", "GENERATIVE_AI_API_KEY"} or "GEMINI" in base:
return "gemini"
if "OPENROUTER" in base or "OPEN_ROUTER" in base:
return "openrouter"
if "OPENAI" in base:
return "openai"
return "other"

def service_can_generate_image(service: str) -> bool:
return service != "google-drive"

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
        r'^\s*["\']?([A-Za-z0-9_.\-\s]+)["\']?\s*(?:=|:)\s*(.*?)\s*$',
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

def _pipeline_key_is_usable(item: dict) -> bool:
"""A selected credential must be able to generate the final image."""
if not item or item.get("auth_type") == "oauth":
return False
if not str(item.get("value", "")).strip():
return False
service = str(item.get("service", "other"))
if service == "google-drive":
return False
try:
return bool(_provider_base_url(item) and _provider_image_model(item))
except Exception:
return False

def _require_pipeline_key() -> tuple[str, dict]:
"""Return a selected image-capable credential without template/prompt gates."""
selected_ids = API_KEY_STATE.get("selected_ids", [])
if not selected_ids:
raise HTTPException(
status_code=400,
detail="No API key is selected. Select at least one API key in the Image Generator header.",
)

pipeline_id = str(API_KEY_STATE.get("pipeline_key_id", "")).strip()
if pipeline_id:
    item = API_KEY_STATE.get("keys", {}).get(pipeline_id)
    if pipeline_id in selected_ids and _pipeline_key_is_usable(item):
        return pipeline_id, item
    API_KEY_STATE["pipeline_key_id"] = ""

candidates = _selected_pipeline_candidates()
capable = [(key_id, item) for key_id, item in candidates if _pipeline_key_is_usable(item)]
if not capable:
    names = ", ".join(
        str(API_KEY_STATE.get("keys", {}).get(k, {}).get("display_name", k))
        for k in selected_ids
        if k in API_KEY_STATE.get("keys", {})
    ) or "none"
    raise HTTPException(
        status_code=400,
        detail=(
            f"None of the selected API keys can generate the final image. Selected: {names}. "
            "Template and AI prompt generation are disabled; select an image-capable API key."
        ),
    )

key_id, item = capable[0]
API_KEY_STATE["pipeline_key_id"] = key_id
return key_id, item

def key_prefix(key_name: str) -> str:
base = re.sub(r"\d+$", "", normalize_key_name(key_name))
for suffix in ("_API_KEY", "_KEY", "TOKEN"):
if base.endswith(suffix):
return base[:-len(suffix)].strip("")
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
service = str(item.get("service", "other")).strip().lower()

# Built-in Gemini adapter does not require a user-supplied base URL.
# _gemini_image() uses the official Gemini client directly.
if service == "gemini":
    return "https://generativelanguage.googleapis.com"

if service == "openai":
    return "https://api.openai.com/v1"

if service == "openrouter":
    return OPENROUTER_BASE_URL

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

-------------------------------------------------------------------

Provider adapters

-------------------------------------------------------------------

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_TEXT_MODEL = os.getenv("OPENROUTER_TEXT_MODEL", "google/gemini-3.1-flash-lite").strip()
OPENROUTER_IMAGE_MODEL = os.getenv("OPENROUTER_IMAGE_MODEL", "google/gemini-3.1-flash-image").strip()

def _openrouter_request(api_key: str, endpoint: str, payload: dict, timeout: int = 180) -> dict:
body=json.dumps(payload).encode("utf-8")
request=Request(f"{OPENROUTER_BASE_URL}/{endpoint.lstrip('/')}",data=body,method="POST",headers={"Authorization""Bearer {api_key}","Content-Type":"application/json","HTTP-Referer":"http://localhost:5173","X-Title":"Objectives Image Generator"})
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
payload={"model","messages":[{"role":"user","content":[{"type":"text","text"},{"type":"image_url","image_url":{"url":_image_data_url(path)}}]}],"temperature":0.2}
return _chat_response_text(_openrouter_request(api_key,"chat/completions",payload),"OpenRouter")

def _extract_json_object(text: str) -> dict:
cleaned=text.strip()
if cleaned.startswith(""):
        cleaned=re.sub(r"^(?)?\s*","",cleaned,flags=re.I); cleaned=re.sub(r"\s*```$","",cleaned)
try:
value=json.loads(cleaned)
if isinstance(value,dict): return value
except json.JSONDecodeError: pass
match=re.search(r"{.*}",cleaned,flags=re.S)
if not match: raise RuntimeError("The selected API did not return valid JSON for template generation.")
try: value=json.loads(match.group(0))
except json.JSONDecodeError as exc: raise RuntimeError("The selected API returned malformed template JSON.") from exc
if not isinstance(value,dict): raise RuntimeError("Template response was not a JSON object.")
return value

def _template_from_text(item: dict, raw: str, reference_path: Path) -> dict:
from PIL import Image
with Image.open(reference_path) as image: width,height=image.size
parsed=extract_json_object(raw); elements=parsed.get("text_elements")
if not isinstance(elements,list): raise RuntimeError("The selected API did not return text_elements for template generation.")
normalized=[]
for i,value in enumerate(elements[:100]):
if not isinstance(value,dict): continue
normalized.append({"id"(value.get("id") or f"text{i+1}"),"text"(value.get("text") or ""),"x"(value.get("x") or 0),"y"(value.get("y") or 0),"width"(value.get("width") or width),"height"(value.get("height") or 50),"font_size"(value.get("font_size") or 32),"font_weight"(value.get("font_weight") or "normal"),"alignment"(value.get("alignment") or "left"),"color"(value.get("color") or "#FFFFFF"),"role"(value.get("role") or "content")})
template_id=import("uuid").uuid4().hex
template={"template_id","version":"4.0","name":"Generated Reference Template","canvas":{"width","height","orientation":"landscape" if width>=height else "portrait","aspect_ratio"(width/height,4) if height else 1},"layout":{"type":"reference-based","alignment":"reference","preserve_reference_structure","preserve_reference_composition","editable_text_only"},"regions":[],"text_elements","text_groups":[],"style":{"keywords":["reference-preserved","professional","editable-text"],"preserve_reference_colors"},"content":{"keywords":["editable-text","content-replacement"],"source_prompt":""},"reference_analysis":{"canvas":{"width","height"}}}
filename=f"{template_id}.json"; (TEMPLATES_DIR/filename).write_text(json.dumps(template,indent=2,ensure_ascii=False),encoding="utf-8")
return {"template_id","template_name","reference","prompt":{"original_prompt":"","layout":{"type":"reference-based","alignment":"reference"},"style_keywords"["keywords"],"content_keywords"["keywords"]},"template","template_file"(Path("templates")/filename)}

def _openrouter_template(api_key: str, reference_path: Path, model: str | None = None) -> dict:
from PIL import Image
with Image.open(reference_path) as image: width,height=image.size
instruction=f"""Analyze this reference poster exactly as a design-template extraction task. Do not redesign it. Identify editable text regions and approximate bounding boxes in pixel coordinates using the reference canvas {width}x{height}. Preserve composition, visual hierarchy, colors, decorative elements, images, logos and spacing. Return ONLY valid JSON: {{"text_elements":[{{"id":"text_1","text":"exact visible text","x":0,"y":0,"width":100,"height":50,"font_size":32,"font_weight":"normal","alignment":"left","color":"#FFFFFF","role":"title"}}]}}"""
return _template_from_text(None,_openrouter_chat_with_image(api_key,reference_path,instruction,model or OPENROUTER_TEXT_MODEL),reference_path)

def _openrouter_prompt(api_key: str, reference_path: Path, model: str | None = None) -> str:
instruction="""Analyze the supplied reference design and create a concise production-ready content prompt for replacing its text while preserving the reference layout. Describe subject/content, important text regions, hierarchy and visual intent. Do not redesign it or invent factual details. Return only the prompt text."""
return _openrouter_chat_with_image(api_key,reference_path,instruction,model)

def _generic_request(item: dict, endpoint: str, payload: dict, timeout: int = 180) -> dict:
base_url=_provider_base_url(item); model=_provider_text_model(item)
if not base_url or not model: raise _generic_compatible_error(item)
request=Request(f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}",data=json.dumps(payload).encode("utf-8"),method="POST",headers={"Authorization""Bearer {str(item.get('value','')).strip()}","Content-Type":"application/json"})
try:
with urlopen(request,timeout=timeout) as response: return json.loads(response.read().decode("utf-8"))
except Exception as exc:
detail=str(exc)
if hasattr(exc,"read"):
try: detail=exc.read().decode("utf-8",errors="replace")
except Exception: pass
raise RuntimeError(f"{item.get('display_name','API')} request failed: {detail}") from exc

def _generic_chat_with_image(item: dict, path: Path, instruction: str) -> str:
payload={"model":_provider_text_model(item),"messages":[{"role":"user","content":[{"type":"text","text"},{"type":"image_url","image_url":{"url":_image_data_url(path)}}]}],"temperature":0.2}
return _chat_response_text(_generic_request(item,"chat/completions",payload),item.get("display_name","API"))

def _generic_template(item: dict, reference_path: Path) -> dict:
from PIL import Image
with Image.open(reference_path) as image: width,height=image.size
instruction=f"""Analyze this reference poster as a design-template extraction task. Do not redesign it. Identify editable text regions and approximate bounding boxes in pixel coordinates for canvas {width}x{height}. Preserve composition, colors, decorative elements, logos and hierarchy. Return ONLY JSON: {{"text_elements":[{{"id":"text_1","text":"exact visible text","x":0,"y":0,"width":100,"height":50,"font_size":32,"font_weight":"normal","alignment":"left","color":"#FFFFFF","role":"title"}}]}}"""
return _template_from_text(item,_generic_chat_with_image(item,reference_path,instruction),reference_path)

def _generic_prompt(item: dict, reference_path: Path) -> str:
return _generic_chat_with_image(item,reference_path,"""Analyze the supplied reference design and create a concise production-ready content prompt for replacing its text while preserving the reference layout. Describe subject/content, important text regions, hierarchy and visual intent. Do not redesign it or invent factual details. Return only the prompt text.""")

def _generic_image(item: dict, path: Path, instruction: str) -> bytes:
base_url=_provider_base_url(item); model=_provider_image_model(item)
if not base_url or not model: raise _generic_compatible_error(item)
import mimetypes
boundary="----ImageGeneratorBoundary"; mime=mimetypes.guess_type(path.name)[0] or "image/png"; body=bytearray()
def field(name,value): body.extend((f"--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n").encode())
field("model",model); field("prompt",instruction)
body.extend((f"--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="reference.png"\r\nContent-Type: {mime}\r\n\r\n").encode()); body.extend(path.read_bytes()); body.extend(f"\r\n--{boundary}--\r\n".encode())
request=Request(f"{base_url.rstrip('/')}/images/edits",data=bytes(body),method="POST",headers={"Authorization""Bearer {str(item.get('value','')).strip()}","Content-Type""multipart/form-data; boundary={boundary}"})
try:
with urlopen(request,timeout=240) as response: payload=json.loads(response.read().decode())
except Exception as exc:
detail=str(exc)
if hasattr(exc,"read"):
try: detail=exc.read().decode("utf-8",errors="replace")
except Exception: pass
raise RuntimeError(f"{item.get('display_name','API')} image generation failed: {detail}") from exc
data=payload.get("data") or []
if not data: raise RuntimeError(f"{item.get('display_name','API')} returned no image output.")
first=data[0]
if first.get("b64_json"): return base64.b64decode(first["b64_json"])
if first.get("url"):
with urlopen(first["url"],timeout=120) as response: return response.read()
raise RuntimeError(f"{item.get('display_name','API')} returned no image data.")

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
if service=="gemini": return _with_pipeline_key(pipeline_item,lambda(**kwargs))
return _generic_prompt(pipeline_item,Path(path))

def _pipeline_template(pipeline_item: dict, **kwargs):
path=kwargs.get("reference_path")
if path is None: raise RuntimeError("Template generation requires a local reference image.")
service=pipeline_item.get("service")
if service=="openrouter": return _openrouter_template(str(pipeline_item["value"]).strip(),Path(path),_provider_text_model(pipeline_item))
if service=="gemini": return _with_pipeline_key(pipeline_item,lambda(**kwargs))
return _generic_template(pipeline_item,Path(path))

def _pipeline_text_key_is_usable(item: dict) -> bool:
"""Return True when a selected API can analyze an image and text."""
if not item or item.get("auth_type") == "oauth":
return False

if not str(item.get("value", "")).strip():
    return False

try:
    return bool(
        _provider_base_url(item)
        and _provider_text_model(item)
    )
except Exception:
    return False

def _selected_text_candidates() -> list[tuple[str, dict]]:
"""Return selected APIs that can perform edit planning."""
return [
(key_id, item)
for key_id, item in _selected_pipeline_candidates()
if _pipeline_text_key_is_usable(item)
]

def _gemini_chat_with_image(
api_key: str,
path: Path,
instruction: str,
model: str,
) -> str:
from google import genai
from google.genai import types

client = genai.Client(api_key=api_key)

response = client.models.generate_content(
    model=model,
    contents=[
        types.Part.from_text(text=instruction),
        types.Part.from_bytes(
            data=path.read_bytes(),
            mime_type=get_mime_type(path),
        ),
    ],
)

text = str(
    getattr(response, "text", "") or ""
).strip()

if text:
    return text

candidates = getattr(
    response,
    "candidates",
    None,
) or []

parts = []

for candidate in candidates:
    content = getattr(
        candidate,
        "content",
        None,
    )

    for part in getattr(
        content,
        "parts",
        None,
    ) or []:
        part_text = getattr(
            part,
            "text",
            None,
        )

        if part_text:
            parts.append(
                str(part_text)
            )

text = "\n".join(parts).strip()

if not text:
    raise RuntimeError(
        "Gemini returned no text for the image edit plan."
    )

return text

def _pipeline_chat_with_image(
pipeline_item: dict,
reference_path: Path,
instruction: str,
) -> str:
"""Run the vision/text planning request using a selected API."""
service = str(
pipeline_item.get("service") or ""
).strip().lower()

api_key = str(
    pipeline_item.get("value") or ""
).strip()

model = str(
    _provider_text_model(pipeline_item) or ""
).strip()

if not api_key or not model:
    raise RuntimeError(
        "The selected API cannot perform edit planning."
    )

if service == "gemini":
    return _with_pipeline_key(
        pipeline_item,
        lambda: _gemini_chat_with_image(
            api_key,
            reference_path,
            instruction,
            model,
        ),
    )

if service == "openrouter":
    return _openrouter_chat_with_image(
        api_key,
        reference_path,
        instruction,
        model,
    )

return _generic_chat_with_image(
    pipeline_item,
    reference_path,
    instruction,
)

def _normalize_change_plan(raw: str) -> dict:
parsed = _extract_json_object(raw)

if not isinstance(parsed, dict):
    raise RuntimeError(
        "The selected API returned an invalid edit plan."
    )

changes = parsed.get("changes", [])
visual_changes = parsed.get(
    "visual_changes",
    [],
)
global_instructions = parsed.get(
    "global_instructions",
    [],
)
must_preserve = parsed.get(
    "must_preserve",
    [],
)

if not isinstance(changes, list):
    changes = []

if not isinstance(visual_changes, list):
    visual_changes = []

if not isinstance(global_instructions, list):
    global_instructions = []

if not isinstance(must_preserve, list):
    must_preserve = []

normalized_changes = []

for index, value in enumerate(
    changes[:100],
    start=1,
):
    if not isinstance(value, dict):
        continue

    action = str(
        value.get("action") or "replace"
    ).strip().lower()

    if action not in {
        "replace",
        "remove",
        "add",
        "update",
        "keep",
    }:
        action = "update"

    normalized_changes.append(
        {
            "id": str(
                value.get("id")
                or f"change_{index}"
            ),
            "target": str(
                value.get("target") or ""
            ),
            "action": action,
            "original_text": str(
                value.get("original_text")
                or ""
            ),
            "new_text": str(
                value.get("new_text")
                or ""
            ),
            "details": str(
                value.get("details")
                or ""
            ),
        }
    )

normalized_visual = []

for index, value in enumerate(
    visual_changes[:100],
    start=1,
):
    if not isinstance(value, dict):
        continue

    normalized_visual.append(
        {
            "id": str(
                value.get("id")
                or f"visual_{index}"
            ),
            "target": str(
                value.get("target") or ""
            ),
            "action": str(
                value.get("action")
                or "update"
            ),
            "from": str(
                value.get("from") or ""
            ),
            "to": str(
                value.get("to") or ""
            ),
            "details": str(
                value.get("details")
                or ""
            ),
        }
    )

return {
    "changes": normalized_changes,
    "visual_changes": normalized_visual,
    "global_instructions": [
        str(value).strip()
        for value in global_instructions
        if str(value).strip()
    ][:50],
    "must_preserve": [
        str(value).strip()
        for value in must_preserve
        if str(value).strip()
    ][:50],
}

def _build_change_plan(
pipeline_item: dict,
reference_path: Path,
user_prompt: str,
template: dict,
) -> dict:
"""Convert free-form user instructions into explicit edit operations."""

template_payload = (
    template.get("template", template)
    if isinstance(template, dict)
    else {}
)

instruction = f"""

You are a precise image/poster editor.

Analyze the supplied reference image, its template information,
and the user's request.

Your job is to convert ONLY the requested changes into a structured
edit plan for another image-generation model.

IMPORTANT RULES:

Preserve the original design unless the user explicitly asks
for a change.

Preserve layout, composition, logos, people, objects, colors,
background, spacing and typography hierarchy by default.

When the user says CHANGE X TO Y:
create an explicit replacement from X to Y.

When the user says REMOVE X:
create action "remove".

When the user says ADD X:
create action "add" and copy the requested text exactly.

NEVER invent dates, names, phone numbers, URLs, titles,
contact details or other factual content.

Requested replacement text must be copied exactly,
including spelling, capitalization, numbers and punctuation.

Put visual changes in visual_changes.

Include every explicit requested change separately.

Do not create additional changes that the user did not request.

Return ONLY valid JSON:

{{
"changes": [
{{
"id": "change_1",
"target": "main heading",
"action": "replace",
"original_text": "OLD TEXT",
"new_text": "NEW TEXT",
"details": "Keep the same position and typography hierarchy"
}}
],
"visual_changes": [
{{
"id": "visual_1",
"target": "background",
"action": "update",
"from": "blue",
"to": "green",
"details": "Only change the requested background"
}}
],
"global_instructions": [],
"must_preserve": []
}}

REFERENCE TEMPLATE:
{json.dumps(
template_payload,
ensure_ascii=False
)[:20000]}

USER REQUEST:
{user_prompt.strip()}
"""

return _normalize_change_plan(
    _pipeline_chat_with_image(
        pipeline_item,
        reference_path,
        instruction,
    )
)

def _pipeline_image(pipeline_item: dict, reference_path: Path, instruction: str) -> tuple[bytes,str]:
service=pipeline_item.get("service")
key=str(pipeline_item.get("value","")).strip()
if service=="gemini": return _gemini_image(key,reference_path,instruction),_provider_image_model(pipeline_item)
if service=="openrouter": return _openrouter_image(key,reference_path,instruction,_provider_image_model(pipeline_item)),_provider_image_model(pipeline_item)
if service=="openai": return _generic_image(pipeline_item,reference_path,instruction),_provider_image_model(pipeline_item)
return _generic_image(pipeline_item,reference_path,instruction),_provider_image_model(pipeline_item)

def _pipeline_social_text(pipeline_item: dict, instruction: str) -> str:
"""Generate social-media text using the selected text-capable API."""
service = str(
pipeline_item.get("service") or ""
).strip().lower()

model = str(
    _provider_text_model(pipeline_item) or ""
).strip()

api_key = str(
    pipeline_item.get("value") or ""
).strip()

if not api_key:
    raise RuntimeError(
        "The selected pipeline API key is empty."
    )

if not model:
    raise RuntimeError(
        "The selected API key has no text-generation model configured."
    )

# ---------------------------------------------------------
# Gemini
# ---------------------------------------------------------
if service == "gemini":

    def call_gemini() -> str:
        from google import genai

        client = genai.Client(
            api_key=api_key
        )

        response = client.models.generate_content(
            model=model,
            contents=instruction,
        )

        # Google GenAI normally exposes generated text through
        # response.text. Keep extraction defensive so a provider
        # response cannot produce an undefined-variable error.
        generated_text = getattr(
            response,
            "text",
            None,
        )

        if generated_text:
            generated_text = str(
                generated_text
            ).strip()

        if generated_text:
            return generated_text

        # Defensive fallback for responses where .text is unavailable.
        response_candidates = []

        candidates = getattr(
            response,
            "candidates",
            None,
        )

        if candidates:
            for candidate in candidates:
                candidate_content = getattr(
                    candidate,
                    "content",
                    None,
                )

                parts = getattr(
                    candidate_content,
                    "parts",
                    None,
                ) or []

                for part in parts:
                    part_text = getattr(
                        part,
                        "text",
                        None,
                    )

                    if part_text:
                        response_candidates.append(
                            str(part_text)
                        )

        generated_text = "\n".join(
            response_candidates
        ).strip()

        if not generated_text:
            raise RuntimeError(
                "Gemini returned no text for the social-media description."
            )

        return generated_text

    return _with_pipeline_key(
        pipeline_item,
        call_gemini,
    )

# ---------------------------------------------------------
# OpenRouter
# ---------------------------------------------------------
if service == "openrouter":

    result = _openrouter_request(
        api_key,
        "chat/completions",
        {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You write platform-specific "
                        "social-media captions. "
                        "Follow the requested format "
                        "and limits exactly. "
                        "Keep emojis, icons, hashtags "
                        "and supplied profile tags."
                    ),
                },
                {
                    "role": "user",
                    "content": instruction,
                },
            ],
            "temperature": 0.7,
        },
    )

    return _extract_chat_text(
        result,
        "OpenRouter",
    )

# ---------------------------------------------------------
# Generic OpenAI-compatible provider
# ---------------------------------------------------------
result = _generic_request(
    pipeline_item,
    "chat/completions",
    {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You write platform-specific "
                    "social-media captions. "
                    "Follow the requested format "
                    "and limits exactly. "
                    "Keep emojis, icons, hashtags "
                    "and supplied profile tags."
                ),
            },
            {
                "role": "user",
                "content": instruction,
            },
        ],
        "temperature": 0.7,
    },
)

return _extract_chat_text(
    result,
    str(
        pipeline_item.get("display_name")
        or "API"
    ),
)

def _openrouter_image(api_key: str, path: Path, instruction: str, model: str | None = None) -> bytes:
selected_model = str(model or OPENROUTER_IMAGE_MODEL).strip()
if not selected_model:
raise RuntimeError("No OpenRouter image model is configured for the selected API key.")
payload={"model","prompt","input_references":[{"type":"image_url","image_url":{"url":_image_data_url(path)}}],"output_format":"png"}
result=_openrouter_request(api_key,"images",payload,timeout=240); data=result.get("data") or []
if not data: raise RuntimeError("OpenRouter returned no image output.")
first=data[0]
if first.get("b64_json"): return base64.b64decode(first["b64_json"])
if first.get("url"):
with urlopen(first["url"],timeout=120) as response: return response.read()
raise RuntimeError("OpenRouter returned no image data.")

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
text = re.sub(r"^['"]+|['\"]+$", "", text).strip()
text = re.sub(r"^(?|semantic tag)\s*[:=-]\s*", "", text, flags=re.I).strip()
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

def _decode_json_secret(value: str, label: str) -> dict | None:
"""Decode a JSON secret supplied through an environment variable.

Railway variables are kept as strings. Accept normal JSON as well as
base64-encoded JSON so multiline OAuth files can be stored safely.
"""
raw = str(value or "").strip()
if not raw:
    return None

candidates = [raw]
try:
    decoded = base64.b64decode(raw, validate=True).decode("utf-8")
    if decoded.strip():
        candidates.append(decoded)
except Exception:
    pass

for candidate in candidates:
    try:
        payload = json.loads(candidate)
    except (TypeError, ValueError, json.JSONDecodeError):
        continue
    if isinstance(payload, dict):
        return payload

raise RuntimeError(
    f"Google Drive {label} environment variable does not contain valid JSON."
)

def _load_drive_client_config() -> dict | None:
"""Load OAuth client configuration from env first, then local file."""
env_value = (
os.getenv(DRIVE_CREDENTIALS_ENV, "").strip()
or os.getenv(DRIVE_CREDENTIALS_ENV_LEGACY, "").strip()
)
if env_value:
return _decode_json_secret(env_value, "OAuth credentials")

if CREDENTIALS_FILE.exists():
    try:
        payload = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return payload
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "Google Drive credentials.json could not be read."
        ) from exc

return None

def _load_drive_token_config() -> dict | None:
"""Load the authorized-user token from env first, then local token.json."""
env_value = (
os.getenv(DRIVE_TOKEN_ENV, "").strip()
or os.getenv(DRIVE_TOKEN_ENV_LEGACY, "").strip()
)
if env_value:
return _decode_json_secret(env_value, "OAuth token")

if TOKEN_FILE.exists():
    try:
        payload = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return payload
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "Google Drive token.json could not be read."
        ) from exc

return None

def _drive_credentials_configured() -> bool:
return _load_drive_client_config() is not None

def _drive_token_configured() -> bool:
return _load_drive_token_config() is not None

def drive_oauth_available() -> bool:
return _drive_credentials_configured()

def drive_oauth_selected() -> bool:
"""
Google Drive is available when its OAuth files and folder configuration
are present. It does not depend on the frontend selecting a synthetic
"Google Drive API" checkbox.
"""
return (
_drive_credentials_configured()
and _drive_token_configured()
and bool(
normalize_drive_folder_id(
API_KEY_STATE.get("drive_folder_id", "")
)
or API_KEY_STATE.get("drive_folder_name", "")
)
)

def get_drive_service():
"""Return an authenticated Google Drive service.

Local development keeps using backend/credentials.json and backend/token.json.
Railway can instead provide those same JSON documents through
GOOGLE_OAUTH_CREDENTIALS_JSON and GOOGLE_OAUTH_TOKEN_JSON.
"""
client_config = _load_drive_client_config()
if not client_config:
    raise RuntimeError(
        "Google Drive OAuth credentials were not found. Provide "
        "backend/credentials.json locally or set GOOGLE_OAUTH_CREDENTIALS_JSON on Railway."
    )

token_config = _load_drive_token_config()
if not token_config:
    raise RuntimeError(
        "Google Drive OAuth token was not found. Provide backend/token.json locally "
        "or set GOOGLE_OAUTH_TOKEN_JSON on Railway."
    )

credentials = None

try:
    # IMPORTANT: do not pass DRIVE_SCOPES here. For an existing authorized-user
    # refresh token, google-auth must refresh using the scopes originally
    # granted with that refresh token. Supplying a new scope list on the
    # refresh request can cause Google to return `invalid_scope`.
    credentials = Credentials.from_authorized_user_info(token_config)
except Exception as exc:
    raise RuntimeError(
        "Google Drive token.json/OAuth token is invalid or incomplete."
    ) from exc

if credentials and credentials.valid:
    return build(
        "drive",
        "v3",
        credentials=credentials,
    )

if credentials and credentials.expired and credentials.refresh_token:
    try:
        credentials.refresh(GoogleAuthRequest())
    except Exception as exc:
        # Do not mislabel every refresh failure as an expired/invalid token.
        # Keep the original Google exception so the actual failure is visible.
        error_text = str(exc).strip() or repr(exc)
        raise RuntimeError(
            f"Google Drive OAuth refresh failed: {error_text}"
        ) from exc

    # Keep the old local behavior: refreshed credentials are persisted
    # when token.json exists. On Railway, secrets come from environment
    # variables, so do not attempt to write them into the container.
    if not os.getenv(DRIVE_TOKEN_ENV, "").strip() and not os.getenv(DRIVE_TOKEN_ENV_LEGACY, "").strip():
        TOKEN_FILE.write_text(
            credentials.to_json(),
            encoding="utf-8",
        )

    return build(
        "drive",
        "v3",
        credentials=credentials,
    )

raise RuntimeError(
    "Google Drive is not authorized yet. Provide a valid authorized-user token "
    "through backend/token.json locally or GOOGLE_OAUTH_TOKEN_JSON on Railway."
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

if not _drive_credentials_configured():
    raise HTTPException(
        status_code=500,
        detail=(
            "Google Drive OAuth credentials were not configured. Provide "
            "backend/credentials.json locally or set GOOGLE_OAUTH_CREDENTIALS_JSON on Railway."
        ),
    )

if not _drive_token_configured():
    raise HTTPException(
        status_code=500,
        detail=(
            "Google Drive OAuth token was not configured. Provide backend/token.json locally "
            "or set GOOGLE_OAUTH_TOKEN_JSON on Railway."
        ),
    )

return folder_id, folder_name

def resolve_drive_folder_id(
service,
folder_id: str,
folder_name: str,
) -> str:
if folder_id:
return folder_id

escaped_name = folder_name.replace("'", "\\'")

result = service.files().list(
    q=(
        f"name = '{escaped_name}' "
        "and mimeType = 'application/vnd.google-apps.folder' "
        "and trashed = false"
    ),
    pageSize=20,
    fields="files(id,name)",
).execute()

folders = result.get("files", [])

if not folders:
    raise RuntimeError(
        f'Google Drive folder "{folder_name}" was not found.'
    )

return str(folders[0]["id"])

def ensure_drive_outputs_folder(service, parent_folder_id: str) -> str:
"""Return the outputs child folder, creating it when necessary."""
result = service.files().list(
q=(
f"'{parent_folder_id}' in parents "
"and name = 'outputs' "
"and mimeType = 'application/vnd.google-apps.folder' "
"and trashed = false"
),
pageSize=10,
fields="files(id,name)",
).execute()
folders = result.get("files", [])
if folders:
return str(folders[0]["id"])

created = service.files().create(
    body={
        "name": "outputs",
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_folder_id],
    },
    fields="id,name",
).execute()
return str(created["id"])

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

        if not mime_type.startswith("image/"):
            continue

        if mime_type == "image/svg+xml":
            continue

        name = str(item.get("name", "Drive Reference"))
        size = int(item.get("size", 0) or 0)
        extension = Path(name).suffix.lower()

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

Restore the last non-secret Drive folder configuration when the

FastAPI process starts. This prevents /api/inputs from losing the Drive

references after a backend restart.

load_persisted_drive_configuration()

-------------------------------------------------------------------

CORS

-------------------------------------------------------------------

app.add_middleware(
CORSMiddleware,
allow_origins=[
"http://localhost:5173",
"http://127.0.0.1:5173",
"https://frontend-production-14d5.up.railway.app",
],
allow_credentials=True,
allow_methods=[""],
allow_headers=[""],
)

-------------------------------------------------------------------

Supported files

-------------------------------------------------------------------

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

-------------------------------------------------------------------

Helpers

-------------------------------------------------------------------

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

if extension in PDF_EXTENSIONS:
    return "pdf"

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

-------------------------------------------------------------------

API key setup

-------------------------------------------------------------------

def is_api_credential_name(key_name: str) -> bool:
"""Return True only for actual credential entries, not configuration."""
name = normalize_key_name(key_name)
if not name:
return False
configuration_suffixes = (
"_BASE_URL", "_API_BASE_URL", "_ENDPOINT", "_MODEL",
"_TEXT_MODEL", "_IMAGE_MODEL", "_MODEL_IMAGE",
"_FOLDER_ID", "_FOLDER_URL", "_FOLDER_NAME", "_DRIVE_NAME",
"_NAME", "_TIMEOUT", "_HOST", "_PORT",
)
if name in {
"MODEL", "TEXT_MODEL", "IMAGE_MODEL", "AI_TEXT_MODEL",
"AI_IMAGE_MODEL", "AI_BASE_URL", "API_BASE_URL",
"GOOGLE_DRIVE_FOLDER_ID", "GOOGLE_DRIVE_FOLDER_NAME",
"GOOGLE_DRIVE_FOLDER_URL", "GDRIVE_FOLDER_ID",
"GDRIVE_FOLDER_NAME", "GDRIVE_FOLDER_URL",
}:
return False
if name.endswith(configuration_suffixes):
return False
# Google Drive is reference/storage only and must never appear as an AI key.
if "DRIVE" in name or "GDRIVE" in name:
return False
return (
"API_KEY" in name
or name.endswith("_TOKEN")
or name.endswith("_SECRET")
or name.endswith("_CREDENTIAL")
or name.endswith("_CREDENTIALS")
)

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
}
provider_counts: dict[str, int] = {}
for key_name, value in values.items():
    normalized = normalize_key_name(key_name)
    base_name = re.sub(r"_\d+$", "", normalized)
    if not value or base_name in configuration_names or not is_api_credential_name(normalized):
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
        "display_name": display_name, "auth_type": "api-key",
        "service": service, "image_generation": service_can_generate_image(service),
    }

drive_folder_id = find_config_value(
    values,
    (
        "GOOGLE_DRIVE_FOLDER_ID",
        "GDRIVE_FOLDER_ID",
        "DRIVE_FOLDER_ID",
        "GOOGLE_DRIVE_FOLDER_URL",
        "GDRIVE_FOLDER_URL",
        "DRIVE_FOLDER_URL",
    ),
)
drive_folder_id = normalize_drive_folder_id(drive_folder_id)

drive_folder_name = find_config_value(
    values,
    (
        "GOOGLE_DRIVE_FOLDER_NAME",
        "GDRIVE_FOLDER_NAME",
        "GOOGLE_DRIVE_NAME",
        "GDRIVE_NAME",
        "DRIVE_FOLDER_NAME",
        "DRIVE_NAME",
    ),
)

API_KEY_STATE["drive_folder_id"] = drive_folder_id
API_KEY_STATE["drive_folder_name"] = drive_folder_name
API_KEY_STATE["gemini_model"] = (
    find_config_value(
        values,
        ("GEMINI_MODEL", "GOOGLE_AI_MODEL", "MODEL"),
    )
    or "gemini-3.5-flash-lite"
)

# Keep the non-secret Drive folder configuration across backend restarts.
persist_drive_configuration()

response_keys = [
    {
        "id": key_id,
        "name": item["display_name"],
        "keyName": key_id,
        "authType": item.get("auth_type", "api-key"),
    }
    for key_id, item in API_KEY_STATE["keys"].items()
]

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

API_KEY_STATE["selected_ids"] = list(selected_ids)
API_KEY_STATE["pipeline_key_id"] = ""

if selected_ids:
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

-------------------------------------------------------------------

Google Drive output-folder configuration

-------------------------------------------------------------------

@app.get("/api/drive/folders")
def get_drive_folders():
"""Return the configured Google Drive parent/output folders.

The frontend uses this endpoint only for the Save-to-Drive folder picker.
It does not expose OAuth secrets or API-key values.
"""
folder_id, folder_name = require_drive_configuration()

try:
    service = get_drive_service()
    resolved_parent_id = resolve_drive_folder_id(
        service,
        folder_id,
        folder_name,
    )

    # Keep the configured parent as the selectable folder. The actual
    # generated image is always placed in its `outputs` child folder.
    parent_name = str(folder_name or "").strip()
    if not parent_name:
        metadata = service.files().get(
            fileId=resolved_parent_id,
            fields="id,name",
        ).execute()
        parent_name = str(metadata.get("name") or "Google Drive folder")

    outputs_folder_id = ensure_drive_outputs_folder(
        service,
        resolved_parent_id,
    )

    API_KEY_STATE["drive_folder_id"] = resolved_parent_id
    API_KEY_STATE["drive_folder_name"] = parent_name
    API_KEY_STATE["drive_output_folder_id"] = outputs_folder_id
    API_KEY_STATE["drive_output_folder_name"] = "outputs"
    persist_drive_configuration()

    return {
        "success": True,
        "folders": [
            {
                "id": resolved_parent_id,
                "name": parent_name,
                "label": parent_name,
            }
        ],
    }

except HTTPException:
    raise
except Exception as exc:
    print("Google Drive folder loading failed:", repr(exc))
    raise HTTPException(
        status_code=500,
        detail=f"Unable to load configured Google Drive output folders: {exc}",
    ) from exc

-------------------------------------------------------------------

Google Drive input files

-------------------------------------------------------------------

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

require_drive_configuration()

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

-------------------------------------------------------------------

Input files

-------------------------------------------------------------------

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

if has_drive_configuration:
    try:
        folder_id = normalize_drive_folder_id(
            API_KEY_STATE.get("drive_folder_id", "")
        )
        folder_name = str(
            API_KEY_STATE.get("drive_folder_name", "")
        ).strip()

        service = get_drive_service()

        resolved_folder_id = resolve_drive_folder_id(
            service,
            folder_id,
            folder_name,
        )

        API_KEY_STATE["drive_folder_id"] = resolved_folder_id
        persist_drive_configuration()

        drive_files = get_drive_files(
            service,
            resolved_folder_id,
        )

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

-------------------------------------------------------------------

Manual uploads

-------------------------------------------------------------------

def get_manual_upload_files() -> list[dict]:
files = []

metadata = load_tag_metadata(
    METADATA_FILE
)

supported_extensions = IMAGE_EXTENSIONS

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

-------------------------------------------------------------------

Health check

-------------------------------------------------------------------

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

-------------------------------------------------------------------

Canva Connect OAuth / generated-image editing

-------------------------------------------------------------------

@app.get("/api/canva/connect/oauth/status")
async def canva_connect_oauth_status():
"""Return whether Canva Connect is configured and authenticated."""
try:
return await canva_connect_service.status()
except Exception as exc:
raise HTTPException(
status_code=500,
detail=f"Unable to check Canva connection: {exc}",
) from exc

@app.get("/api/canva/connect/oauth/start")
async def canva_connect_oauth_start():
"""Create the Canva OAuth authorization URL."""
try:
return {
"authorization_url": canva_connect_service.authorization_url(),
}
except Exception as exc:
raise HTTPException(
status_code=400,
detail=str(exc),
) from exc

@app.get("/api/canva/connect/oauth/callback")
async def canva_connect_oauth_callback(
code: str | None = None,
state: str | None = None,
error: str | None = None,
error_description: str | None = None,
):
"""Receive Canva OAuth callback and store the resulting token."""
import html

if error:
    message = html.escape(error_description or error)

    return HTMLResponse(
        content=(
            "<!doctype html><html><body style='font-family:Arial;padding:40px'>"
            "<h2>Canva connection failed</h2>"
            f"<p>{message}</p>"
            "<p>Close this window and return to the Image Generator.</p>"
            "</body></html>"
        ),
        status_code=400,
    )

if not code:
    return HTMLResponse(
        content=(
            "<!doctype html><html><body style='font-family:Arial;padding:40px'>"
            "<h2>Canva connection failed</h2>"
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
    message = html.escape(str(exc))

    return HTMLResponse(
        content=(
            "<!doctype html><html><body style='font-family:Arial;padding:40px'>"
            "<h2>Canva authorization failed</h2>"
            f"<p>{message}</p>"
            "<p>Close this window and try Connect Canva again.</p>"
            "</body></html>"
        ),
        status_code=400,
    )

@app.post("/api/canva/create-from-generated-image")
async def canva_create_from_generated_image(
filename: str = Form(...),
design_type: str = Form("poster"),
):
"""Create an editable Canva design from the generated local image."""

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
    result = (
        await canva_connect_service.create_editable_design_from_local_image(
            local_path
        )
    )

    return {
        **result,
        "message": result.get(
            "message",
            "The generated image was uploaded to Canva and placed in an editable Canva design.",
        ),
    }

except Exception as exc:
    message = str(exc)
    lowered = message.lower()

    if any(
        phrase in lowered
        for phrase in (
            "not authorized",
            "not configured",
            "authorization",
            "oauth",
            "connect canva",
            "reconnect canva",
        )
    ):
        try:
            authorization_url = (
                canva_connect_service.authorization_url()
            )
        except Exception:
            authorization_url = ""

        raise HTTPException(
            status_code=401,
            detail=message,
            headers={
                "X-Canva-Authorization-URL": authorization_url,
            },
        ) from exc

    raise HTTPException(
        status_code=502,
        detail=(
            "Unable to create the editable Canva design: "
            f"{message}"
        ),
    ) from exc

-------------------------------------------------------------------

Get input files

-------------------------------------------------------------------

@app.get("/api/inputs")
def get_inputs():
return get_input_files()

-------------------------------------------------------------------

Serve input file

-------------------------------------------------------------------

@app.get(
"/api/inputs/file/{filename}"
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

-------------------------------------------------------------------

Serve manual upload

-------------------------------------------------------------------

@app.get(
"/api/manual/file/{filename}"
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

-------------------------------------------------------------------

Upload image / GIF as a manual reference

-------------------------------------------------------------------

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

if (
    extension
    not in IMAGE_EXTENSIONS
):

    raise HTTPException(
        status_code=400,
        detail=(
            "Only image and GIF files "
            "are supported for manual references."
        ),
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

    if API_KEY_STATE.get("selected_ids"):
        _, pipeline_item = _require_pipeline_key()
        tag = _pipeline_tag(pipeline_item, destination)
        # Keep the existing metadata cache format used by the frontend.
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
    "type": (
        "gif"
        if extension == ".gif"
        else "image"
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

-------------------------------------------------------------------

Tag one image

-------------------------------------------------------------------

@app.post(
"/api/inputs/tag/{filename}"
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

    _, pipeline_item = _require_pipeline_key()
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

-------------------------------------------------------------------

Tag all images

-------------------------------------------------------------------

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

_, pipeline_item = _require_pipeline_key()

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



-------------------------------------------------------------------

-------------------------------------------------------------------

Image generation

-------------------------------------------------------------------

IMAGE_OUTPUT_DIR = BASE_DIR / "output" / "images"
IMAGE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

def safe_output_name(filename: str) -> str:
import uuid
stem = re.sub(r"[^A-Za-z0-9-]+", "", Path(filename or "generated").stem).strip("") or "generated"
return f"{stem}generated{uuid.uuid4().hex[:8]}.png"

def resolve_generation_reference(source_type: str, source: str, filename: str, content_type: str = "") -> tuple[Path, str]:
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
path = UPLOADS_DIR / f"generation{uuid.uuid4().hex}_{filename}"
try:
req = Request(source, headers={"User-Agent": "Mozilla/5.0"})
with urlopen(req, timeout=30) as response: path.write_bytes(response.read())
except Exception as exc: raise HTTPException(status_code=400, detail=f"Unable to download the reference image: {exc}") from exc
else: raise HTTPException(status_code=400, detail="Unsupported reference source for image generation.")
if not path.exists() or not is_valid_image_file(path): raise HTTPException(status_code=400, detail="The selected reference is not a readable image.")
return path, content_type or get_mime_type(path)

def _generation_instruction(
prompt: str,
template_json: str = "{}",
) -> str:
return f"""
Edit the supplied reference image according to the user's request.

IMPORTANT:
Apply ONLY the changes explicitly requested by the user.

PRESERVE EVERYTHING ELSE:

original composition

original layout

logo placement

branding

people

objects

background

colors

decorative elements

spacing

typography hierarchy

overall visual style

TEXT CHANGES:

Change only the text explicitly mentioned by the user.

Use the exact replacement text supplied by the user.

Preserve spelling exactly.

Preserve capitalization exactly.

Preserve numbers exactly.

Preserve punctuation exactly.

Keep the original text position and style unless the user explicitly asks for them to change.

REMOVALS:

Remove only the item explicitly requested by the user.

Do not remove other text, objects, logos, or decorations.

ADDITIONS:

Add only what the user explicitly requests.

Use the exact text supplied by the user.

Do not invent any extra content.

VISUAL CHANGES:

Apply only explicitly requested visual changes.

Do not redesign the reference.

Do not introduce additional colors, objects, backgrounds, decorations, or effects unless requested.

DO NOT INVENT:

names

dates

phone numbers

email addresses

URLs

prices

contact details

titles

any other factual content

USER REQUEST:
{prompt.strip()}
"""

# Template generation is intentionally disabled. The reference image and
# the user's manual prompt are the only design inputs.
return f"""Edit the supplied reference image into the requested final poster. Preserve the reference composition, layout, colors, decorative elements, logo placement, people/objects, and overall visual style. Do not redesign it from scratch. Replace only the content requested by the user. Keep text in the same regions and hierarchy, with correct spelling and readable typography. Do not invent contact details or extra content.\n\nUSER CONTENT REQUEST:\n{prompt}"""

def _gemini_image(api_key: str, path: Path, instruction: str) -> bytes:
from google import genai
from google.genai import types
client = genai.Client(api_key=api_key)
response = client.models.generate_content(model="gemini-3.1-flash-image", contents=[types.Part.from_text(text=instruction), types.Part.from_bytes(data=path.read_bytes(), mime_type=get_mime_type(path))], config=types.GenerateContentConfig(response_modalities=["IMAGE"]))
for part in response.parts or []:
inline_data = getattr(part, "inline_data", None)
if inline_data is not None:
# google-genai can expose an SDK image wrapper whose save() method
# does not accept PIL's format= keyword. Prefer the raw image bytes
# supplied by Gemini.
image_data = getattr(inline_data, "data", None)
if image_data:
return bytes(image_data)

        # Compatibility fallback for SDK versions that do not expose the
        # inline bytes directly.
        image_obj = part.as_image()
        out = io.BytesIO()
        image_obj.convert("RGB").save(out, "PNG")
        return out.getvalue()
raise RuntimeError("The Gemini API returned no image. This key may not have access to the image-generation model.")

def _openai_image(api_key: str, path: Path, instruction: str) -> bytes:
import mimetypes
boundary="----ImageGeneratorBoundary"
mime=mimetypes.guess_type(path.name)[0] or "image/png"
body=bytearray()
def field(name,value): return (f"--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n").encode()
body.extend(field("model","gpt-image-2")); body.extend(field("prompt",instruction))
body.extend((f"--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="reference.png"\r\nContent-Type: {mime}\r\n\r\n").encode()); body.extend(path.read_bytes()); body.extend(f"\r\n--{boundary}--\r\n".encode())
req=Request("https://api.openai.com/v1/images/edits",data=bytes(body),method="POST",headers={"Authorization":f"Bearer {api_key}","Content-Type""multipart/form-data; boundary={boundary}"})
try:
with urlopen(req,timeout=180) as response: payload=json.loads(response.read().decode())
except Exception as exc: raise RuntimeError(f"OpenAI image generation failed: {exc}") from exc
item=(payload.get("data") or [None])[0]
if not item: raise RuntimeError("OpenAI returned no image output.")
if item.get("b64_json"): return base64.b64decode(item["b64_json"])
if item.get("url"):
with urlopen(item["url"],timeout=60) as response: return response.read()
raise RuntimeError("OpenAI returned no image data.")

def _pipeline_description(pipeline_item: dict, image_path: Path, user_prompt: str) -> str:
"""Generate a concise description of the finished image."""
instruction = (
"Describe the supplied generated poster/image in 2 to 4 concise sentences. "
"Mention the main subject, important visible text or message, visual style, "
"and notable composition. Do not invent facts that are not visible. "
f"The user's content request was: {user_prompt.strip()}"
)
service = str(pipeline_item.get("service", "other"))
key = str(pipeline_item.get("value", "")).strip()

if service == "openrouter":
    return _openrouter_chat_with_image(
        key, image_path, instruction, _provider_text_model(pipeline_item)
    )

if service == "gemini":
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=key)
    response = client.models.generate_content(
        model=_provider_text_model(pipeline_item),
        contents=[
            types.Part.from_text(text=instruction),
            types.Part.from_bytes(
                data=image_path.read_bytes(),
                mime_type=get_mime_type(image_path),
            ),
        ],
    )
    text = str(getattr(response, "text", "") or "").strip()
    if not text:
        raise RuntimeError("The selected API returned an empty image description.")
    return text

return _generic_chat_with_image(pipeline_item, image_path, instruction)

@app.post("/api/images/description")
async def describe_generated_image(payload: dict = Body(...)):
filename = Path(str(payload.get("filename", ""))).name
prompt = str(payload.get("prompt", "")).strip()
if not filename:
raise HTTPException(status_code=400, detail="Generated image filename is required.")

image_path = IMAGE_OUTPUT_DIR / filename
if not image_path.exists() or not image_path.is_file() or not is_valid_image_file(image_path):
    raise HTTPException(status_code=404, detail="Generated image was not found or is not a valid image.")

selected_candidates = [
    (candidate_id, candidate_item)
    for candidate_id, candidate_item in _selected_pipeline_candidates()
    if _pipeline_key_is_usable(candidate_item)
]
if not selected_candidates:
    raise HTTPException(status_code=400, detail="Select at least one image-capable API key before generating a description.")

errors = []
for key_id, pipeline_item in selected_candidates:
    try:
        description = _pipeline_description(pipeline_item, image_path, prompt)
        if description:
            API_KEY_STATE["pipeline_key_id"] = key_id
            return {
                "success": True,
                "description": description,
                "api_id": key_id,
                "provider": pipeline_item.get("display_name", "Selected API"),
            }
    except Exception as exc:
        errors.append(f"{pipeline_item.get('display_name', key_id)}: {exc}")

fallback = (
    f"Generated image based on the requested content: {prompt}"
    if prompt
    else "Generated image created successfully from the selected reference."
)
return {
    "success": True,
    "description": fallback,
    "api_id": "fallback",
    "provider": "Generated-image fallback",
    "warning": " | ".join(errors),
}

@app.post("/api/social-media/generate")
async def generate_social_media_description(
filename: str = Form(...),
prompt: str = Form(""),
template_json: str = Form("{}"),
):
"""
Generate social-media content for an already generated image.

This endpoint is intentionally separate from the image-generation
pipeline. It uses the generated PNG already stored in IMAGE_OUTPUT_DIR.
"""

safe_name = Path(filename).name

if not safe_name:
    raise HTTPException(
        status_code=400,
        detail="Generated image filename is required.",
    )

image_path = IMAGE_OUTPUT_DIR / safe_name

# The frontend sends the filename returned by /api/images/generate.
# Check the exact same output directory used by image generation.
if not image_path.exists() or not image_path.is_file():
    raise HTTPException(
        status_code=404,
        detail=(
            f"Generated image '{safe_name}' was not found on the backend. "
            "Generate the image again before generating its social-media description."
        ),
    )

if not is_valid_image_file(image_path):
    raise HTTPException(
        status_code=400,
        detail=(
            f"Generated image '{safe_name}' exists but is not a readable image."
        ),
    )

selected_candidates = [
    (candidate_id, candidate_item)
    for candidate_id, candidate_item in _selected_pipeline_candidates()
    if _pipeline_key_is_usable(candidate_item)
]

if not selected_candidates:
    raise HTTPException(
        status_code=400,
        detail=(
            "Select at least one image-capable API key "
            "before generating a description."
        ),
    )

errors = []

social_instruction = (
    "Analyze the supplied generated image and create social-media content "
    "for it.\n\n"

    "Return EXACTLY these four sections:\n\n"

    "[LINKEDIN]\n"
    "Write a professional LinkedIn post based on the visible image "
    "and the user's content request. Keep it concise and professional.\n\n"

    "[X / TWITTER]\n"
    "Write a concise post suitable for X/Twitter. Keep it under 280 "
    "characters when possible.\n\n"

    "[FACEBOOK]\n"
    "Write an engaging Facebook post based on the visible image.\n\n"

    "[INSTAGRAM]\n"
    "Write an engaging Instagram caption with relevant hashtags.\n\n"

    "Do not invent facts that are not visible in the image.\n"
    "Do not describe objects that cannot reasonably be seen.\n\n"

    f"User's content request: {prompt.strip()}"
)

for key_id, pipeline_item in selected_candidates:
    try:
        service = str(
            pipeline_item.get("service", "other")
        ).strip().lower()

        key = str(
            pipeline_item.get("value", "")
        ).strip()

        if not key:
            raise RuntimeError(
                "The selected API key is empty."
            )

        # ---------------------------------------------------------
        # OpenRouter
        # ---------------------------------------------------------
        if service == "openrouter":

            text = _openrouter_chat_with_image(
                key,
                image_path,
                social_instruction,
                _provider_text_model(pipeline_item),
            )

        # ---------------------------------------------------------
        # Gemini
        # ---------------------------------------------------------
        elif service == "gemini":

            from google import genai
            from google.genai import types

            client = genai.Client(
                api_key=key
            )

            response = client.models.generate_content(
                model=_provider_text_model(pipeline_item),
                contents=[
                    types.Part.from_text(
                        text=social_instruction
                    ),
                    types.Part.from_bytes(
                        data=image_path.read_bytes(),
                        mime_type=get_mime_type(image_path),
                    ),
                ],
            )

            text = str(
                getattr(response, "text", "") or ""
            ).strip()

            # Defensive extraction for Gemini SDK responses
            # where response.text is unavailable.
            if not text:

                response_candidates = []

                candidates = getattr(
                    response,
                    "candidates",
                    None,
                )

                if candidates:
                    for candidate in candidates:

                        candidate_content = getattr(
                            candidate,
                            "content",
                            None,
                        )

                        parts = getattr(
                            candidate_content,
                            "parts",
                            None,
                        ) or []

                        for part in parts:

                            part_text = getattr(
                                part,
                                "text",
                                None,
                            )

                            if part_text:
                                response_candidates.append(
                                    str(part_text)
                                )

                text = "\n".join(
                    response_candidates
                ).strip()

        # ---------------------------------------------------------
        # Other configured providers
        # ---------------------------------------------------------
        else:

            text = _generic_chat_with_image(
                pipeline_item,
                image_path,
                social_instruction,
            )

        if not text:
            raise RuntimeError(
                "The selected API returned an empty "
                "social-media description."
            )

        # Save the generated description locally.
        description_name = (
            f"{Path(safe_name).stem}_description.txt"
        )

        description_path = (
            IMAGE_OUTPUT_DIR / description_name
        )

        description_path.write_text(
            text,
            encoding="utf-8",
        )

        API_KEY_STATE["pipeline_key_id"] = key_id

        return {
            "success": True,
            "filename": safe_name,
            "content": text,
            "description": text,
            "social_media_filename": description_name,
            "social_media_file_url": (
                f"/api/social-media/output/"
                f"{quote(description_name)}"
            ),
            "provider": pipeline_item.get(
                "display_name",
                "Selected API",
            ),
            "model": _provider_text_model(
                pipeline_item
            ),
            "api_id": key_id,
            "word_count": len(text.split()),
            "message": (
                "Social-media description generated "
                "successfully."
            ),
        }

    except Exception as exc:

        errors.append(
            f"{pipeline_item.get('display_name', key_id)}: {exc}"
        )

raise HTTPException(
    status_code=502,
    detail=(
        "Unable to generate the social-media description "
        "using the selected API keys. "
        + " | ".join(errors)
    ),
)

@app.get("/api/social-media/output/{filename}")
def get_social_media_file(filename: str):
path = IMAGE_OUTPUT_DIR / Path(filename).name

if (
    not path.exists()
    or not path.is_file()
    or path.suffix.lower() != ".txt"
):
    raise HTTPException(
        status_code=404,
        detail="Social-media text file was not found.",
    )

return FileResponse(
    path,
    media_type="text/plain; charset=utf-8",
    filename=path.name,
)

@app.post("/api/social-media/save-to-drive")
def save_social_media_description_to_drive(
filename: str = Form(...)
):
"""Save only the generated social-media TXT file into Google Drive/outputs."""

require_drive_configuration()

safe_name = Path(filename).name

if not safe_name or not safe_name.lower().endswith(".txt"):
    raise HTTPException(
        status_code=400,
        detail="A valid social-media description filename is required.",
    )

local_path = IMAGE_OUTPUT_DIR / safe_name

if not local_path.exists() or not local_path.is_file():
    raise HTTPException(
        status_code=404,
        detail="The social-media description file was not found on the server.",
    )

try:
    service = get_drive_service()

    parent_folder_id = resolve_drive_folder_id(
        service,
        normalize_drive_folder_id(
            API_KEY_STATE.get("drive_folder_id", "")
        ),
        str(
            API_KEY_STATE.get("drive_folder_name", "")
            or ""
        ),
    )

    outputs_folder_id = ensure_drive_outputs_folder(
        service,
        parent_folder_id,
    )

    API_KEY_STATE["drive_output_folder_id"] = outputs_folder_id
    API_KEY_STATE["drive_output_folder_name"] = "outputs"

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

    media = MediaIoBaseUpload(
        io.BytesIO(local_path.read_bytes()),
        mimetype="text/plain",
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
        "drive_url": drive_file.get(
            "webViewLink",
            "",
        ),
        "message": (
            "Social-media description saved "
            "to Google Drive/outputs."
        ),
    }

except HTTPException:
    raise

except Exception as exc:
    raise HTTPException(
        status_code=502,
        detail=(
            "Unable to save the social-media description "
            f"to Google Drive: {exc}"
        ),
    ) from exc    

@app.post("/api/images/generate")
async def generate_output_image(
    source_type: str = Form(...),
    source: str = Form(...),
    filename: str = Form("reference.png"),
    content_type: str = Form(""),
    prompt: str = Form(...),
    template_json: str = Form("{}"),
):
    if not prompt.strip():
        raise HTTPException(
            status_code=400,
            detail="Enter a content prompt before generating the output image.",
        )

    # Require at least one selected image-capable key, then try selected keys
    # in order. A failed/limited key must not block another selected key.
    _require_pipeline_key()

    reference_path, _ = _resolve_generation_reference(
        source_type,
        source,
        filename,
        content_type,
    )

    instruction = _generation_instruction(
        prompt,
        template_json,
    )

    selected_candidates = [
        (candidate_id, candidate_item)
        for candidate_id, candidate_item in _selected_pipeline_candidates()
        if _pipeline_key_is_usable(candidate_item)
    ]

    errors = []
    image_bytes = None
    image_model = ""
    key_id = ""
    item = None

    for candidate_id, candidate_item in selected_candidates:
        try:
            candidate_bytes, candidate_model = _pipeline_image(
                candidate_item,
                reference_path,
                instruction,
            )

            image_bytes = candidate_bytes
            image_model = candidate_model
            key_id = candidate_id
            item = candidate_item

            API_KEY_STATE["pipeline_key_id"] = candidate_id
            break

        except Exception as exc:
            errors.append(
                f"{candidate_item.get('display_name', candidate_id)}: {exc}"
            )

    if image_bytes is None or item is None:
        detail = (
            " | ".join(errors)
            if errors
            else "No selected API key can generate images."
        )

        raise HTTPException(
            status_code=502,
            detail=(
                "Image generation failed for all selected image-capable API keys. "
                f"{detail}"
            ),
        )

    output_name = _safe_output_name(filename)
    (IMAGE_OUTPUT_DIR / output_name).write_bytes(image_bytes)

    return {
        "success": True,
        "image_url": f"/api/images/output/{quote(output_name)}",
        "filename": output_name,
        "model": image_model,
        "provider": item.get("display_name", "Selected API"),
        "api_id": key_id,
        "pipeline_api_id": key_id,
        "selected_api_count": len(
            API_KEY_STATE.get("selected_ids", [])
        ),
        "changes": {},
    } 

@app.post("/api/social-media/generate")
async def generate_social_media_description(
filename: str = Form(...),
prompt: str = Form(""),
template_json: str = Form("{}"),
):
"""
Compatibility endpoint used by the existing frontend Generate Description
button.

It generates a description from the already-generated image without
modifying the existing image-generation pipeline.
"""

safe_name = Path(filename).name

if not safe_name:
    raise HTTPException(
        status_code=400,
        detail="Generated image filename is required.",
    )

image_path = IMAGE_OUTPUT_DIR / safe_name

if (
    not image_path.exists()
    or not image_path.is_file()
    or not is_valid_image_file(image_path)
):
    raise HTTPException(
        status_code=404,
        detail="Generated image was not found or is not a valid image.",
    )

selected_candidates = [
    (candidate_id, candidate_item)
    for candidate_id, candidate_item
    in _selected_pipeline_candidates()
    if _pipeline_key_is_usable(candidate_item)
]

if not selected_candidates:
    raise HTTPException(
        status_code=400,
        detail=(
            "Select at least one image-capable API key "
            "before generating a description."
        ),
    )

errors = []

for key_id, pipeline_item in selected_candidates:
    try:
        description = _pipeline_description(
            pipeline_item,
            image_path,
            prompt.strip(),
        )

        if description:
            API_KEY_STATE["pipeline_key_id"] = key_id

            return {
                "success": True,
                "filename": safe_name,
                "description": description,
                "content": description,
                "provider": pipeline_item.get(
                    "display_name",
                    "Selected API",
                ),
                "model": _provider_text_model(
                    pipeline_item
                ),
                "api_id": key_id,
                "message": (
                    "Generated-image description "
                    "generated successfully."
                ),
            }

    except Exception as exc:
        errors.append(
            f"{pipeline_item.get('display_name', key_id)}: {exc}"
        )

fallback = (
    f"Generated image based on the requested content: "
    f"{prompt.strip()}"
    if prompt.strip()
    else
    "Generated image created successfully "
    "from the selected reference."
)

return {
    "success": True,
    "filename": safe_name,
    "description": fallback,
    "content": fallback,
    "provider": "Generated-image fallback",
    "model": "",
    "api_id": "fallback",
    "warning": " | ".join(errors),
    "message": (
        "Generated-image description returned "
        "using fallback text."
    ),
}

@app.get("/api/images/output/{filename}")
def get_generated_image(filename: str):
path=IMAGE_OUTPUT_DIR/Path(filename).name
if not path.exists(): raise HTTPException(status_code=404, detail="Generated image was not found.")
return FileResponse(path,media_type="image/png",filename=path.name)

@app.post("/api/images/save-to-drive")
def save_generated_image_to_drive(
filename: str = Form(...),
folder_id: str = Form(""),
):
"""Save a generated local image into an outputs folder under the selected Drive folder."""
configured_folder_id, configured_folder_name = require_drive_configuration()
safe_name = Path(filename).name
if not safe_name:
raise HTTPException(status_code=400, detail="Generated image filename is required.")

local_path = IMAGE_OUTPUT_DIR / safe_name
if not local_path.exists() or not local_path.is_file():
    raise HTTPException(status_code=404, detail="Generated image was not found on the server.")

try:
    service = get_drive_service()
    requested_folder_id = normalize_drive_folder_id(folder_id)
    parent_folder_id = requested_folder_id or resolve_drive_folder_id(
        service,
        configured_folder_id,
        configured_folder_name,
    )
    outputs_folder_id = ensure_drive_outputs_folder(service, parent_folder_id)
    API_KEY_STATE["drive_output_folder_id"] = outputs_folder_id
    API_KEY_STATE["drive_output_folder_name"] = "outputs"
    persist_drive_configuration()

    # Avoid creating duplicate files with the same name on repeated clicks.
    existing = service.files().list(
        q=(
            f"'{outputs_folder_id}' in parents "
            f"and name = '{safe_name.replace(chr(39), chr(92)+chr(39))}' "
            "and trashed = false"
        ),
        pageSize=10,
        fields="files(id,name,webViewLink)",
    ).execute().get("files", [])

    media = MediaIoBaseUpload(
        io.BytesIO(local_path.read_bytes()),
        mimetype="image/png",
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
        "message": "Generated image saved to Google Drive/outputs.",
    }
except HTTPException:
    raise
except Exception as exc:
    raise HTTPException(status_code=500, detail=f"Unable to save generated image to Google Drive: {exc}") from exc

Generate AI prompt from reference

-------------------------------------------------------------------

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

key_id, pipeline_item = _require_pipeline_key()

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

-------------------------------------------------------------------

Generate template automatically from reference

-------------------------------------------------------------------

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

key_id, pipeline_item = _require_pipeline_key()

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
