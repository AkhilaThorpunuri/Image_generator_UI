export interface ReferenceForImageGeneration {
  type:
    | "image"
    | "gif"
    | "pdf"
    | "video"
    | "youtube"
    | "image-link";
  name: string;
  url: string;
  source?:
    | "input-folder"
    | "external-url"
    | "upload"
    | "google-drive";
  sourceId?: string;
  mimeType?: string;
}

export interface GeneratedImageResponse {
  success: boolean;
  image_url: string;
  filename: string;
  template_id?: string;
  model: string;
  provider?: string;
  api_id?: string;
  pipeline_api_id?: string;
  reference_count?: number;
  reference_names?: string[];
  description?: string;
  description_warning?: string;
}

const API_BASE_URL = (
  import.meta.env.VITE_API_BASE_URL || "http://localhost:8000"
).replace(/\/+$/, "");

type BackendSourceType =
  | "input-folder"
  | "google-drive"
  | "upload"
  | "external-url"
  | "youtube";

function getSourceType(reference: ReferenceForImageGeneration): BackendSourceType {
  if (reference.type === "youtube") return "youtube";
  if (reference.source === "google-drive") return "google-drive";
  if (reference.source === "upload") return "upload";
  if (reference.source === "input-folder") return "input-folder";
  return "external-url";
}

function getSource(reference: ReferenceForImageGeneration): string {
  const sourceType = getSourceType(reference);

  if (sourceType === "google-drive") {
    const driveFileId = reference.sourceId?.trim();
    if (!driveFileId) throw new Error("Google Drive reference ID is missing.");
    return driveFileId.replace(/^drive:/i, "");
  }

  if (sourceType === "upload" || sourceType === "input-folder") {
    const filename = (reference.sourceId || reference.name).trim();
    if (!filename) throw new Error("Reference filename is missing.");
    return filename;
  }

  const externalSource = (reference.sourceId || reference.url).trim();
  if (!externalSource || !/^https?:\/\//i.test(externalSource)) {
    throw new Error("A valid HTTP or HTTPS reference URL is required.");
  }
  return externalSource;
}

export async function generateImage(args: {
  reference?: ReferenceForImageGeneration;
  references?: ReferenceForImageGeneration[];
  prompt: string;
  template?: unknown;
  selectedApiIds?: string[];
}): Promise<GeneratedImageResponse> {
  const prompt = args.prompt.trim();
  if (!prompt) throw new Error("Content prompt is required.");

  const references =
    args.references && args.references.length > 0
      ? args.references
      : args.reference
        ? [args.reference]
        : [];

  if (references.length === 0) {
    throw new Error("At least one reference image is required.");
  }

  if (references.length > 16) {
    throw new Error("A maximum of 16 reference images can be selected.");
  }

  // Synchronize the CURRENT UI API selection immediately before generation.
  // This prevents a previously successful/cached API from being reused after
  // the user changes the selected keys.
  if (!args.selectedApiIds || args.selectedApiIds.length === 0) {
    throw new Error("No API key is selected. Select at least one API key before generating an image.");
  }

  const selectionResponse = await fetch(`${API_BASE_URL}/api/api-keys/select`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ selected_ids: args.selectedApiIds }),
    credentials: "include",
  });

  const selectionData = await selectionResponse.json().catch(() => null);
  if (!selectionResponse.ok) {
    throw new Error(
      String(selectionData?.detail || "Unable to synchronize the selected API keys."),
    );
  }

  const selected = Array.isArray(selectionData?.selected)
    ? selectionData.selected
    : [];
  if (selected.length === 0) {
    throw new Error("None of the selected API keys are available on the backend.");
  }

  const selectedReferences = references.map((reference, index) => ({
    number: index + 1,
    source_type: getSourceType(reference),
    source: getSource(reference),
    filename: reference.name || `reference_${index + 1}.png`,
    content_type: reference.mimeType || "",
  }));

  const first = selectedReferences[0];
  const formData = new FormData();

  // Legacy fields are kept for backend compatibility.
  formData.append("source_type", first.source_type);
  formData.append("source", first.source);
  formData.append("filename", first.filename);
  formData.append("content_type", first.content_type);

  // Authoritative multi-reference field.
  formData.append("references_json", JSON.stringify(selectedReferences));

  formData.append("prompt", prompt);

  // Template generation is disabled in the current workflow.
  formData.append("template_json", "{}");

  const response = await fetch(`${API_BASE_URL}/api/images/generate`, {
    method: "POST",
    body: formData,
    credentials: "include",
  });

  const data = (await response.json().catch(() => ({}))) as Partial<GeneratedImageResponse> & {
    detail?: string;
  };

  if (!response.ok) {
    throw new Error(data.detail || "Unable to generate the output image.");
  }

  if (!data.image_url) {
    throw new Error("Image generation completed without an output image.");
  }

  return data as GeneratedImageResponse;
}
