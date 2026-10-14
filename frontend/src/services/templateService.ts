export interface ReferenceForTemplate {
  type:
    | "image"
    | "gif"
    | "pdf"
    | "ppt"
    | "pptx"
    | "video"
    | "youtube"
    | "image-link";
  name: string;
  /**
   * Browser preview URL.
   *
   * This may be a backend URL, external HTTPS URL,
   * or browser blob URL.
   *
   * It is NOT necessarily the value sent to the backend.
   */
  url: string;
  source?:
    | "input-folder"
    | "external-url"
    | "upload"
    | "google-drive";
  /**
   * Real backend source.
   *
   * Google Drive -> Drive file ID
   * Manual upload -> stored filename
   * Input folder -> filename
   * External URL / YouTube -> HTTP/HTTPS URL
   */
  sourceId?: string;
  mimeType?: string;
}

export interface GeneratedTemplate {
  template_id: string;
  version: string;
  name: string;
  canvas: {
    width: number;
    height: number;
    orientation: string;
    aspect_ratio: number;
  };
  layout: {
    type: string;
    alignment: string;
    preserve_reference_structure: boolean;
    [key: string]: unknown;
  };
  regions: Array<{
    name: string;
    x: number;
    y: number;
    width: number;
    height: number;
    order: number;
  }>;
  text_groups?: Array<{
    id: string;
    role: string;
    text: string;
    element_ids: string[];
    bbox: {
      x: number;
      y: number;
      width: number;
      height: number;
    };
    line_count: number;
    lines: Array<{
      element_id: string;
      text: string;
      x: number;
      y: number;
      width: number;
      height: number;
      font_size: number;
      font_family: string;
      font_weight: string;
      alignment: string;
      color: string;
      uppercase: boolean;
      line_spacing: number;
    }>;
  }>;
  text_elements?: Array<{
    id: string;
    text: string;
    x: number;
    y: number;
    width: number;
    height: number;
    font_size: number;
    font_family: string;
    font_weight: string;
    alignment: string;
    color: string;
    uppercase: boolean;
    line_spacing: number;
    confidence: number;
  }>;
  style: {
    keywords: string[];
    dominant_colors: Array<{
      hex: string;
      percentage: number;
    }>;
    average_brightness: number;
    preserve_reference_colors: boolean;
  };
  content: {
    keywords: string[];
    source_prompt: string;
  };
  reference_analysis: {
    width: number;
    height: number;
    format: string;
    mode: string;
    canvas: {
      width: number;
      height: number;
      aspect_ratio: number;
      orientation: string;
    };
    dominant_colors: Array<{
      hex: string;
      percentage: number;
    }>;
    average_brightness: number;
  };
  source?: {
    type: string;
    filename: string;
  };
}

export interface GenerateTemplateResponse {
  template_id: string;
  template_name: string;
  reference: GeneratedTemplate["reference_analysis"];
  prompt: {
    original_prompt: string;
    layout: {
      type: string;
      alignment: string;
      sections: string[];
    };
    style_keywords: string[];
    content_keywords: string[];
  };
  template: GeneratedTemplate;
  template_file: string;
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

function getSourceType(
  reference: ReferenceForTemplate,
): BackendSourceType {
  if (reference.type === "youtube") {
    return "youtube";
  }
  if (reference.source === "google-drive") {
    return "google-drive";
  }
  if (reference.source === "upload") {
    return "upload";
  }
  if (reference.source === "input-folder") {
    return "input-folder";
  }
  return "external-url";
}

function getSource(
  reference: ReferenceForTemplate,
): string {
  const sourceType = getSourceType(reference);

  /*
   * Google Drive
   *
   * The browser preview URL must NOT be sent
   * to the backend.
   *
   * The actual Drive file ID is sent.
   */
  if (sourceType === "google-drive") {
    const driveFileId = reference.sourceId?.trim();
    if (!driveFileId) {
      throw new Error("Google Drive reference ID is missing.");
    }
    return driveFileId.replace(/^drive:/i, "");
  }

  /*
   * Local input / manual upload
   */
  if (sourceType === "upload" || sourceType === "input-folder") {
    const filename = (reference.sourceId || reference.name).trim();
    if (!filename) {
      throw new Error("Uploaded reference filename is missing.");
    }
    return filename;
  }

  /*
   * External URL / YouTube
   */
  const externalSource = (reference.sourceId || reference.url).trim();
  if (!externalSource) {
    throw new Error("Reference source is missing.");
  }
  if (!/^https?:\/\//i.test(externalSource)) {
    throw new Error("A valid HTTP or HTTPS URL is required.");
  }
  return externalSource;
}

function getErrorMessage(data: unknown): string {
  if (typeof data !== "object" || data === null) {
    return "Unable to generate template.";
  }

  const errorData = data as {
    detail?: unknown;
    message?: unknown;
  };

  /* FastAPI validation errors */
  if (Array.isArray(errorData.detail)) {
    const messages = errorData.detail
      .map((item) => {
        if (typeof item === "object" && item !== null) {
          const validationItem = item as {
            msg?: unknown;
            loc?: unknown[];
          };
          const message =
            typeof validationItem.msg === "string"
              ? validationItem.msg
              : "Invalid request.";
          const location = Array.isArray(validationItem.loc)
            ? validationItem.loc
                .filter((part) => part !== "body")
                .join(" → ")
            : "";
          return location ? `${location}: ${message}` : message;
        }
        return String(item);
      })
      .filter(Boolean);

    if (messages.length > 0) {
      return messages.join(" | ");
    }
  }

  if (typeof errorData.detail === "string") {
    return errorData.detail;
  }
  if (typeof errorData.message === "string") {
    return errorData.message;
  }
  return "Unable to generate template.";
}

export async function generateTemplate(
  reference: ReferenceForTemplate,
): Promise<GenerateTemplateResponse> {
  const sourceType = getSourceType(reference);
  const source = getSource(reference);
  const formData = new FormData();

  formData.append("source_type", sourceType);
  formData.append("source", source);
  formData.append("filename", reference.name || "reference");

  if (reference.mimeType) {
    formData.append("content_type", reference.mimeType);
  }

  const response = await fetch(
    `${API_BASE_URL}/api/templates/generate`,
    {
      method: "POST",
      body: formData,
      credentials: "include",
    },
  );

  let data: unknown = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }

  if (!response.ok) {
    throw new Error(getErrorMessage(data));
  }

  if (typeof data !== "object" || data === null) {
    throw new Error(
      "Invalid response received from the template service.",
    );
  }

  const responseData = data as {
    success?: boolean;
    template?: GenerateTemplateResponse;
  };

  if (!responseData.template) {
    throw new Error("Template service returned an empty template.");
  }

  return responseData.template;
}
