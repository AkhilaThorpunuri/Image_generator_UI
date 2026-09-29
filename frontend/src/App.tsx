// CANVA_DEPLOYMENT_TEST_2026
import {
  useEffect,
  useRef,
  useState,
} from "react";

import type {
  ChangeEvent,
  DragEvent,
  UIEvent,
} from "react";

import "./App.css";

import { generateTemplate } from "./services/templateService";
import type {
  GenerateTemplateResponse,
} from "./services/templateService";

const CANVA_DEPLOYMENT_TEST_2026 = "CANVA_DEPLOYMENT_TEST_2026";
console.log(CANVA_DEPLOYMENT_TEST_2026);
const API_BASE_URL = "http://localhost:8000";


function resolveApiUrl(url: string): string {
  if (!url) {
    return "";
  }

  if (
    /^https?:\/\//i.test(url) ||
    url.startsWith("blob:")
  ) {
    return url;
  }

  return `${API_BASE_URL}${url.startsWith("/") ? "" : "/"}${url}`;
}


type ReferenceType =
  | "image"
  | "gif"
  | "pdf"
  | "video"
  | "youtube"
  | "image-link";


interface InputFile {
  id: string;
  name: string;
  type: string;
  mimeType: string;
  size: number;
  sizeFormatted: string;
  url: string;
  tag?: string;
  tagError?: string;
  source?:
    | "google-drive"
    | "manual-upload"
    | "input-folder";
}


interface DriveOutputFolder { id: string; name: string; label?: string; }

interface ReferenceData {
  type: ReferenceType;
  name: string;
  url: string;
  size?: number;
  mimeType?: string;
  source?:
    | "input-folder"
    | "external-url"
    | "upload"
    | "google-drive";
  /*
   * Backend source is kept separate from the browser preview URL.
   * For Google Drive this is the Drive file ID. For uploaded files it
   * is the backend filename. Never send a browser blob URL to FastAPI.
   */
  sourceId?: string;
  tag?: string;
}


function getReferenceType(
  extension: string,
  mimeType = "",
): ReferenceType {
  const normalizedExtension =
    extension.toLowerCase();

  if (normalizedExtension === ".gif") {
    return "gif";
  }

  if (normalizedExtension === ".pdf") {
    return "pdf";
  }

  if (
    normalizedExtension === ".mp4" ||
    normalizedExtension === ".webm" ||
    normalizedExtension === ".mov"
  ) {
    return "video";
  }

  if (
    normalizedExtension === ".png" ||
    normalizedExtension === ".jpg" ||
    normalizedExtension === ".jpeg" ||
    normalizedExtension === ".webp" ||
    mimeType.startsWith("image/")
  ) {
    return "image";
  }

  return "image";
}


function isYouTubeUrl(url: string): boolean {
  try {
    const parsed = new URL(url);

    return (
      parsed.hostname.includes("youtube.com") ||
      parsed.hostname.includes("youtu.be")
    );
  } catch {
    return false;
  }
}


function isImageUrl(url: string): boolean {
  try {
    const parsed = new URL(url);

    const pathname =
      parsed.pathname.toLowerCase();

    return (
      pathname.endsWith(".png") ||
      pathname.endsWith(".jpg") ||
      pathname.endsWith(".jpeg") ||
      pathname.endsWith(".webp") ||
      pathname.endsWith(".gif")
    );
  } catch {
    return false;
  }
}


function formatReferenceType(
  type: ReferenceType,
): string {
  switch (type) {
    case "image":
      return "Image";

    case "gif":
      return "GIF";

    case "pdf":
      return "PDF";

    case "video":
      return "Video";

    case "youtube":
      return "YouTube";

    case "image-link":
      return "Image Link";

    default:
      return "Reference";
  }
}


interface ApiKeyOption {
  id: string;
  name: string;
  keyName: string;
  authType?: string;
  displayName?: string;
  duplicateNumber?: number;
}


interface SelectedApiService {
  id: string;
  name: string;
  keyName?: string;
}

interface ApiKeySetupProps {
  onComplete: (
    selectedKeys: string[],
    selectedServices: SelectedApiService[],
  ) => void;
  onBackToHome: () => void;
}


function ApiKeySetup({
  onComplete,
  onBackToHome,
}: ApiKeySetupProps) {
  const [apiFile, setApiFile] =
    useState<File | null>(null);

  const [apiKeys, setApiKeys] =
    useState<ApiKeyOption[]>([]);

  const [isUploading, setIsUploading] =
    useState(false);

  const [isSaving, setIsSaving] =
    useState(false);

  const [error, setError] =
    useState("");

  const apiFileInputRef =
    useRef<HTMLInputElement | null>(null);


  async function handleApiFile(
    file: File,
  ) {
    setApiFile(file);
    setApiKeys([]);
    setError("");
    setIsUploading(true);

    try {
      const formData =
        new FormData();

      formData.append(
        "file",
        file,
      );

      const response =
        await fetch(
          `${API_BASE_URL}/api/api-keys/upload`,
          {
            method: "POST",
            body: formData,
            credentials: "include",
          },
        );

      const data =
        await response
          .json()
          .catch(() => null);

      if (!response.ok) {
        throw new Error(
          String(
            data?.detail ||
              "Unable to read the API keys file.",
          ),
        );
      }

      const rawKeys:
        ApiKeyOption[] =
        Array.isArray(data?.keys)
          ? data.keys
          : [];


      /*
       * Google Drive is intentionally NOT an API-key selection option.
       * It is an OAuth-backed reference source and is used directly by
       * the Google Drive reference loader in the Image Generator.
       *
       * Keep the Drive credential/configuration on the backend, but hide
       * its synthetic OAuth entry from this API selection screen.
       */
      const visibleRawKeys = rawKeys.filter((api) => {
        const name = String(api.name || "").toLowerCase();
        const keyName = String(api.keyName || "").toLowerCase();

        return (
          api.authType !== "oauth" &&
          !name.includes("google drive") &&
          !keyName.includes("google_drive") &&
          !keyName.includes("gdrive")
        );
      });


      /*
       * Keep every supported AI/API credential. Multiple credentials for
       * the same provider can be selected independently.
       *
       * Examples:
       *   Gemini API
       *   Gemini API 2
       *   Gemini API 3
       */
      const serviceCounts =
        new Map<string, number>();

      const keys = visibleRawKeys.map((api) => {
        const baseName =
          api.name.trim() || "API";
        const normalizedName =
          baseName.toLowerCase();
        const occurrence =
          (serviceCounts.get(normalizedName) || 0) + 1;

        serviceCounts.set(
          normalizedName,
          occurrence,
        );

        return {
          ...api,
          name: baseName,
          displayName: baseName,
          duplicateNumber: occurrence,
        };
      });

      const duplicateTotals =
        new Map<string, number>();

      keys.forEach((api) => {
        const normalizedName =
          api.name.trim().toLowerCase();
        duplicateTotals.set(
          normalizedName,
          (duplicateTotals.get(normalizedName) || 0) + 1,
        );
      });

      const displayKeys = keys.map((api) => {
        const normalizedName =
          api.name.trim().toLowerCase();
        const total =
          duplicateTotals.get(normalizedName) || 1;

        return {
          ...api,
          name:
            total > 1
              ? `${api.displayName} ${api.duplicateNumber}`
              : api.displayName,
        };
      });


      if (!displayKeys.length) {
        throw new Error(
          "No supported API keys were found in the uploaded file.",
        );
      }

      if (!displayKeys.length) {
        throw new Error(
          "No supported API keys were found in the uploaded file.",
        );
      }

      setApiKeys(displayKeys);

    } catch (err) {
      console.error(
        "API key file processing failed:",
        err,
      );

      setError(
        err instanceof Error
          ? err.message
          : "Unable to read the API keys file.",
      );

      setApiFile(null);

    } finally {
      setIsUploading(false);
    }
  }



  function handleDownloadApiTemplate() {
    const templateBlob = new Blob([], {
      type: "application/octet-stream",
    });

    const downloadUrl =
      URL.createObjectURL(templateBlob);

    const link =
      document.createElement("a");

    link.href = downloadUrl;
    link.download = "API key Template.txt";
    document.body.appendChild(link);
    link.click();
    link.remove();

    URL.revokeObjectURL(downloadUrl);
  }


  async function handleContinue() {
    if (!apiKeys.length) {
      setError("Upload an API key file before continuing.");
      return;
    }

    setError("");
    setIsSaving(true);

    try {
      const response =
        await fetch(
          `${API_BASE_URL}/api/api-keys/select`,
          {
            method: "POST",
            headers: {
              "Content-Type":
                "application/json",
            },
            body: JSON.stringify({
              // Start the Image Generator with all uploaded AI keys selected.
              // The user can then deselect any key(s) from the header.
              selected_ids: apiKeys.map((api) => api.id),
            }),
            credentials: "include",
          },
        );

      const data =
        await response
          .json()
          .catch(() => null);

      if (!response.ok) {
        throw new Error(
          String(
            data?.detail ||
              "Unable to activate the selected APIs.",
          ),
        );
      }

      const selectedServices =
        apiKeys.map((api) => ({
          id: api.id,
          name: api.name,
          keyName: api.keyName,
        }));

      // Keep all uploaded AI keys selected initially. The Image Generator
      // header still allows the user to select/deselect multiple keys.
      onComplete(
        apiKeys.map((api) => api.id),
        selectedServices,
      );

    } catch (err) {
      console.error(
        "API selection failed:",
        err,
      );

      setError(
        err instanceof Error
          ? err.message
          : "Unable to activate the selected APIs.",
      );

    } finally {
      setIsSaving(false);
    }
  }


  return (
    <div className="api-setup-shell">

      <div className="api-setup-card">

        <div className="api-setup-header">

          <div className="api-setup-mark">
            ✦
          </div>

          <div>

            <div className="api-setup-kicker">
              INITIAL SETUP
            </div>

            <h1>
              Connect your API keys
            </h1>

            <p>
              Upload your API configuration
              file from your desktop. The file
              is processed by the backend and is
              not saved in the project.
            </p>

          </div>

        </div>


        <div className="api-setup-flow">

          <div className="api-setup-step active">

            <span>
              01
            </span>

            <div>
              <strong>
                Upload API Keys File
              </strong>

              <small>
                Choose the API configuration
                file from your desktop.
              </small>
            </div>

          </div>


          <div className="api-setup-connector" />


          <div
            className={`api-setup-step ${
              apiKeys.length
                ? "active"
                : ""
            }`}
          >

            <span>
              02
            </span>

            <div>

              <strong>
                Review APIs
              </strong>

              <small>
                API keys can be selected from the workspace header.
              </small>

            </div>

          </div>


          <div className="api-setup-connector" />


          <div
            className={`api-setup-step ${
              apiKeys.length
                ? "active"
                : ""
            }`}
          >

            <span>
              03
            </span>

            <div>

              <strong>
                Open Workspace
              </strong>

              <small>
                Open the Image Generator.
              </small>

            </div>

          </div>

        </div>


        <div className="api-setup-body">

          <div className="api-upload-panel">

            <input
              ref={apiFileInputRef}
              type="file"
              hidden
              accept=".env,.txt,.json"
              onChange={(event) => {
                const file =
                  event.target.files?.[0];

                if (file) {
                  void handleApiFile(file);
                }

                event.target.value = "";
              }}
            />


            <button
              type="button"
              className="api-upload-button"
              onClick={() =>
                apiFileInputRef.current?.click()
              }
              disabled={isUploading}
            >

              <span className="api-upload-icon">
                ↑
              </span>

              <span>

                <strong>
                  {isUploading
                    ? "Reading API file..."
                    : "Upload API Keys File"}
                </strong>

                <small>
                  .env, .txt or .json
                </small>

              </span>

            </button>


            <button
              type="button"
              className="api-template-download-button"
              onClick={handleDownloadApiTemplate}
              style={{
                width: "100%",
                marginTop: "10px",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                gap: "10px",
                padding: "12px 16px",
                borderRadius: "12px",
                border: "1px solid rgba(255, 255, 255, 0.12)",
                background: "rgba(255, 255, 255, 0.04)",
                color: "inherit",
                cursor: "pointer",
                font: "inherit",
              }}
            >
              <span aria-hidden="true">
                ↓
              </span>

              <span>
                Download API Key Template
              </span>
            </button>


            {apiFile &&
              !isUploading && (
                <div className="api-uploaded-file">

                  <span className="api-file-check">
                    ✓
                  </span>

                  <div>

                    <strong>
                      {apiFile.name}
                    </strong>

                    <small>
                      API configuration loaded
                    </small>

                  </div>

                </div>
              )}

          </div>


          <div className="api-selection-panel">

            <div className="api-selection-heading">

              <div>

                <span>
                  AVAILABLE APIs
                </span>

                <strong>
                  Available API services
                </strong>

              </div>

              {apiKeys.length > 0 && (
                <small>
                  {apiKeys.length} available
                </small>
              )}

            </div>


            {isUploading ? (

              <div className="api-setup-empty">

                <div className="api-setup-loader" />

                <strong>
                  Reading API configuration
                </strong>

                <span>
                  Detecting available API services.
                </span>

              </div>

            ) : apiKeys.length === 0 ? (

              <div className="api-setup-empty">

                <div className="api-setup-empty-icon">
                  ◇
                </div>

                <strong>
                  Upload a file to continue
                </strong>

                <span>
                  API values stay hidden. Only API
                  service names will be displayed.
                </span>

              </div>

            ) : (

              <div className="api-key-list">

                {apiKeys.map(
                  (api) => (

                    <div
                      key={api.id}
                      className="api-key-option"
                      style={{
                        cursor: "default",
                        transition:
                          "border-color 0.2s ease, background 0.2s ease, box-shadow 0.2s ease",
                      }}
                    >

                      <span
                        className="api-key-service-icon"
                        style={{
                          width: "32px",
                          height: "32px",
                          display: "inline-flex",
                          alignItems: "center",
                          justifyContent: "center",
                          background: "rgba(255,255,255,0.06)",
                          borderRadius: "9px",
                          flexShrink: 0,
                        }}
                      >
                        {getApiServiceIcon(api.name, api.keyName, 24)}
                      </span>

                      <span className="api-key-service-text">

                        <strong>
                          {api.name}
                        </strong>

                        <small>
                          API credential detected
                        </small>

                      </span>

                      <span className="api-key-state">
                        Available
                      </span>

                    </div>

                  ),
                )}

              </div>

            )}

          </div>

        </div>


        {error && (
          <div className="api-setup-error">
            {error}
          </div>
        )}


        <div className="api-setup-footer">

          <div className="api-security-note">

            <span>
              ✓
            </span>

            <div>

              <strong>
                API values are never displayed
              </strong>

              <small>
                Only API service names appear
                in the selection list.
              </small>

            </div>

          </div>


          <div className="api-setup-footer-actions">
            <button
              type="button"
              className="api-back-home-button"
              onClick={onBackToHome}
            >
              <span>←</span>
              Home
            </button>

            <button
              type="button"
              className="api-continue-button"
            disabled={
              !apiKeys.length ||
              isSaving
            }
            onClick={
              handleContinue
            }
          >

            {isSaving
              ? "Loading..."
              : "Continue to Image Generator"}

            <span>
              →
            </span>

          </button>
          </div>

        </div>

      </div>

    </div>
  );
}


interface ImageGeneratorProps {
  selectedApiKeys: string[];
  selectedApiServices: SelectedApiService[];
  availableApiServices: SelectedApiService[];
  onApiSelectionChange: (selectedIds: string[]) => void;
  onBackToApiSetup: () => void;
  onBackToHome: () => void;}


function getApiServiceIcon(
  serviceName: string,
  keyName = "",
  size = 24,
) {
  const normalize = (value: string) =>
    value
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, " ")
      .replace(/\s+/g, " ")
      .trim();

  const service = normalize(serviceName);
  const key = normalize(keyName);

  /*
   * Provider icon registry
   * ------------------------------------------------------------
   *
   * The key name is checked before the display name. This is
   * important because different environment-variable names can
   * represent the same provider.
   *
   * Provider-specific assets are used instead of rendering the
   * first character of the provider name. Therefore the UI shows
   * only the real provider icon + provider name.
   *
   * Gemini uses Google's official Gemini sparkle asset.
   * Claude uses Claude's product favicon.
   * OpenRouter uses its current official app icon.
   * Other providers use their own official site favicon.
   */
  const brandIcons: Array<{
    matches: string[];
    iconUrl: string;
    alt: string;
  }> = [
    // Google / Gemini
    {
      matches: [
        "gemini api",
        "gemini",
        "google gemini",
        "gemini api key",
        "gemini api key",
        "google api key",
        "google ai api",
        "google ai api key",
        "generative ai api",
      ],
      iconUrl:
        "https://www.gstatic.com/lamda/images/gemini_sparkle_v002_d4735304ff6292a690345.svg",
      alt: "Google Gemini",
    },
    {
      matches: [
        "google drive api",
        "google drive",
        "drive api",
        "gdrive api",
      ],
      iconUrl:
        "https://drive.google.com/favicon.ico",
      alt: "Google Drive",
    },
    {
      matches: [
        "google cloud api",
        "google cloud",
        "gcp api",
        "google vertex api",
        "google vertex",
      ],
      iconUrl:
        "https://cloud.google.com/favicon.ico",
      alt: "Google Cloud",
    },
    {
      matches: [
        "google api",
        "google",
      ],
      iconUrl:
        "https://www.google.com/favicon.ico",
      alt: "Google",
    },

    // AI providers — exact product icons
    {
      matches: [
        "openrouter api",
        "openrouter",
        "open router api",
        "open router",
        "openrouter api key",
      ],
      iconUrl:
        "https://openrouter.ai/apple-touch-icon.png",
      alt: "OpenRouter",
    },
    {
      matches: [
        "claude api",
        "claude",
        "claude ai",
        "anthropic claude",
        "anthropic api",
        "anthropic",
        "anthropic api key",
      ],
      iconUrl:
        // "https://claude.ai/favicon.ico",
        "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/svg/claude-ai.svg",
      alt: "Claude",
    },
    {
      matches: [
        "openai api",
        "openai",
        "chatgpt api",
        "chatgpt",
        "openai api key",
      ],
      iconUrl:
        "https://openai.com/favicon.ico",
      alt: "OpenAI",
    },
    {
      matches: [
        "groq api",
        "groq",
        "groq api key",
      ],
      iconUrl:
        "https://groq.com/favicon.ico",
      alt: "Groq",
    },
    {
      matches: [
        "mistral api",
        "mistral",
        "mistralai",
        "mistral ai",
        "mistral ai api",
      ],
      iconUrl:
        "https://mistral.ai/favicon.ico",
      alt: "Mistral AI",
    },
    {
      matches: [
        "cohere api",
        "cohere",
        "cohere api key",
      ],
      iconUrl:
        "https://cohere.com/favicon.ico",
      alt: "Cohere",
    },
    {
      matches: [
        "hugging face api",
        "huggingface api",
        "hugging face",
        "huggingface",
      ],
      iconUrl:
        "https://huggingface.co/favicon.ico",
      alt: "Hugging Face",
    },
    {
      matches: [
        "perplexity api",
        "perplexity",
        "perplexity api key",
      ],
      iconUrl:
        "https://www.perplexity.ai/favicon.ico",
      alt: "Perplexity",
    },
    {
      matches: [
        "deepseek api",
        "deepseek",
        "deepseek api key",
      ],
      iconUrl:
        "https://www.deepseek.com/favicon.ico",
      alt: "DeepSeek",
    },
    {
      matches: [
        "xai api",
        "xai",
        "x ai api",
        "grok api",
        "grok",
        "x ai",
      ],
      iconUrl:
        "https://x.ai/favicon.ico",
      alt: "xAI",
    },
    {
      matches: [
        "qwen api",
        "qwen",
        "alibaba qwen",
      ],
      iconUrl:
        "https://qwen.ai/favicon.ico",
      alt: "Qwen",
    },
    {
      matches: [
        "fireworks api",
        "fireworks",
        "fireworks ai",
      ],
      iconUrl:
        "https://fireworks.ai/favicon.ico",
      alt: "Fireworks AI",
    },
    {
      matches: [
        "together ai api",
        "together api",
        "together ai",
        "together",
      ],
      iconUrl:
        "https://www.together.ai/favicon.ico",
      alt: "Together AI",
    },
    {
      matches: [
        "replicate api",
        "replicate",
      ],
      iconUrl:
        "https://replicate.com/favicon.ico",
      alt: "Replicate",
    },
    {
      matches: [
        "stability ai api",
        "stability ai",
        "stabilityai",
        "stability",
      ],
      iconUrl:
        "https://stability.ai/favicon.ico",
      alt: "Stability AI",
    },
    {
      matches: [
        "elevenlabs api",
        "eleven labs api",
        "elevenlabs",
        "eleven labs",
      ],
      iconUrl:
        "https://elevenlabs.io/favicon.ico",
      alt: "ElevenLabs",
    },
    {
      matches: [
        "assemblyai api",
        "assembly ai api",
        "assemblyai",
        "assembly ai",
      ],
      iconUrl:
        "https://www.assemblyai.com/favicon.ico",
      alt: "AssemblyAI",
    },

    // Microsoft / AWS / Meta
    {
      matches: [
        "azure api",
        "microsoft azure api",
        "azure",
        "microsoft azure",
        "azure openai api",
      ],
      iconUrl:
        "https://azure.microsoft.com/favicon.ico",
      alt: "Microsoft Azure",
    },
    {
      matches: [
        "microsoft api",
        "microsoft",
        "ms graph api",
        "microsoft graph api",
        "graph api",
      ],
      iconUrl:
        "https://www.microsoft.com/favicon.ico",
      alt: "Microsoft",
    },
    {
      matches: [
        "aws api",
        "amazon web services api",
        "amazon web services",
        "amazon api",
        "aws",
      ],
      iconUrl:
        "https://aws.amazon.com/favicon.ico",
      alt: "Amazon Web Services",
    },
    {
      matches: [
        "meta api",
        "meta",
        "facebook api",
        "facebook",
      ],
      iconUrl:
        "https://www.meta.com/favicon.ico",
      alt: "Meta",
    },

    // Developer / data / infrastructure
    {
      matches: ["pinecone api", "pinecone"],
      iconUrl:
        "https://www.pinecone.io/favicon.ico",
      alt: "Pinecone",
    },
    {
      matches: ["github api", "github"],
      iconUrl:
        "https://github.com/favicon.ico",
      alt: "GitHub",
    },
    {
      matches: ["gitlab api", "gitlab"],
      iconUrl:
        "https://gitlab.com/favicon.ico",
      alt: "GitLab",
    },

    // Communication / productivity / design
    {
      matches: ["youtube api", "youtube"],
      iconUrl:
        "https://www.youtube.com/favicon.ico",
      alt: "YouTube",
    },
    {
      matches: ["stripe api", "stripe"],
      iconUrl:
        "https://stripe.com/favicon.ico",
      alt: "Stripe",
    },
    {
      matches: ["twilio api", "twilio"],
      iconUrl:
        "https://www.twilio.com/favicon.ico",
      alt: "Twilio",
    },
    {
      matches: ["sendgrid api", "sendgrid"],
      iconUrl:
        "https://sendgrid.com/favicon.ico",
      alt: "SendGrid",
    },
    {
      matches: ["slack api", "slack"],
      iconUrl:
        "https://slack.com/favicon.ico",
      alt: "Slack",
    },
    {
      matches: ["discord api", "discord"],
      iconUrl:
        "https://discord.com/favicon.ico",
      alt: "Discord",
    },
    {
      matches: ["notion api", "notion"],
      iconUrl:
        "https://www.notion.so/favicon.ico",
      alt: "Notion",
    },
    {
      matches: ["canva api", "canva"],
      iconUrl:
        "https://www.canva.com/favicon.ico",
      alt: "Canva",
    },
    {
      matches: ["figma api", "figma"],
      iconUrl:
        "https://www.figma.com/favicon.ico",
      alt: "Figma",
    },
  ];

  const matchesEntry = (entry: (typeof brandIcons)[number]) =>
    entry.matches.some((match) => {
      const normalizedMatch = normalize(match);

      return (
        key === normalizedMatch ||
        service === normalizedMatch ||
        key.includes(normalizedMatch) ||
        service.includes(normalizedMatch)
      );
    });

  const brand = brandIcons.find(matchesEntry);

  if (brand) {
    return (
      <img
        src={brand.iconUrl}
        alt={`${brand.alt} icon`}
        title={brand.alt}
        aria-hidden="true"
        width={size}
        height={size}
        style={{
          width: `${size}px`,
          height: `${size}px`,
          display: "block",
          objectFit: "contain",
          flexShrink: 0,
          borderRadius: "4px",
        }}
        onError={(event) => {
          /*
           * Never render A, C, O, or any other provider letter.
           * If a remote provider asset is temporarily unavailable,
           * show a neutral icon until the asset loads again.
           */
          const image = event.currentTarget;
          image.style.display = "none";

          const fallback =
            document.createElement("span");

          fallback.innerHTML = `
            <svg
              width="${size}"
              height="${size}"
              viewBox="0 0 24 24"
              fill="none"
              xmlns="http://www.w3.org/2000/svg"
              aria-hidden="true"
            >
              <circle
                cx="12"
                cy="12"
                r="9"
                stroke="currentColor"
                stroke-width="2"
              />
              <path
                d="M8 12h8M12 8v8"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
              />
            </svg>
          `;

          fallback.style.width = `${size}px`;
          fallback.style.height = `${size}px`;
          fallback.style.display = "inline-flex";
          fallback.style.alignItems = "center";
          fallback.style.justifyContent = "center";
          fallback.style.color = "#7C6CFF";
          fallback.style.flexShrink = "0";

          image.parentElement?.appendChild(fallback);
        }}
      />
    );
  }

  /*
   * Unknown/custom provider:
   * never use the provider's first letter. Use a neutral API
   * symbol instead so the UI always remains icon + name.
   */
  return (
    <span
      title={`${serviceName} icon`}
      aria-label={`${serviceName} icon`}
      style={{
        width: `${size}px`,
        height: `${size}px`,
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        color: "#7C6CFF",
        flexShrink: 0,
      }}
    >
      <svg
        width={size}
        height={size}
        viewBox="0 0 24 24"
        fill="none"
        xmlns="http://www.w3.org/2000/svg"
        aria-hidden="true"
      >
        <circle
          cx="12"
          cy="12"
          r="9"
          stroke="currentColor"
          strokeWidth="2"
        />
        <path
          d="M8 12h8M12 8v8"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
        />
      </svg>
    </span>
  );
}

function ImageGenerator({
  selectedApiKeys,
  selectedApiServices,
  availableApiServices,
  onApiSelectionChange,
  onBackToApiSetup,
  onBackToHome,
}: ImageGeneratorProps) {


  const [inputFiles, setInputFiles] =
    useState<InputFile[]>([]);

  const [selectedInputIds, setSelectedInputIds] =
    useState<string[]>([]);

  // Keep the first selected reference for the existing preview area.
  const selectedInputId = selectedInputIds[0] || "";

  const [reference, setReference] =
    useState<ReferenceData | null>(null);

  const [showReferenceModal, setShowReferenceModal] =
    useState(false);

  const [showUrlInput, setShowUrlInput] =
    useState(false);

  const [externalUrl, setExternalUrl] =
    useState("");

  const [templatePrompt, setTemplatePrompt] =
    useState("");

  const [promptMode, setPromptMode] =
    useState<"manual" | "ai">(
      "manual",
    );

  const [isGeneratingPrompt, setIsGeneratingPrompt] =
    useState(false);

  const [templateResult, setTemplateResult] =
    useState<GenerateTemplateResponse | null>(
      null,
    );

  const [templateApiProvider, setTemplateApiProvider] =
    useState("");

  const [templateApiModel, setTemplateApiModel] =
    useState("");

  const [promptApiProvider, setPromptApiProvider] =
    useState("");

  const [promptApiModel, setPromptApiModel] =
    useState("");

  const [generatedImageUrl, setGeneratedImageUrl] =
    useState("");

  const [generatedImageFilename, setGeneratedImageFilename] =
    useState("");

  const [generatedImageDescription, setGeneratedImageDescription] =
    useState("");

  type SocialDescriptionItem = {
    text: string;
    character_count: number;
    character_limit: number;
  };

  const [socialDescriptions, setSocialDescriptions] =
    useState<Record<string, SocialDescriptionItem>>({});
  const [socialDescriptionFilename, setSocialDescriptionFilename] =
    useState("");
  const [socialDescriptionPreviewContent, setSocialDescriptionPreviewContent] =
    useState("");
  const [isGeneratingSocialDescriptions, setIsGeneratingSocialDescriptions] =
    useState(false);
  const [socialDescriptionError, setSocialDescriptionError] =
    useState("");
  const [socialDescriptionSaved, setSocialDescriptionSaved] =
    useState(false);
  const [socialDescriptionSaveMessage, setSocialDescriptionSaveMessage] =
    useState("");
  const [isSocialDescriptionPreviewOpen, setIsSocialDescriptionPreviewOpen] =
    useState(false);

  const [generatedImageModel, setGeneratedImageModel] =
    useState("");

  const [generatedImageProvider, setGeneratedImageProvider] =
    useState("");

  const [isSavingGeneratedImage, setIsSavingGeneratedImage] =
    useState(false);

  const [generatedImageSaved, setGeneratedImageSaved] =
    useState(false);

  const [generatedImageSaveMessage, setGeneratedImageSaveMessage] =
    useState("");

  const [canvaEditUrl, setCanvaEditUrl] = useState("");
  const [canvaDesignId, setCanvaDesignId] = useState("");
  const [isCreatingCanvaDesign, setIsCreatingCanvaDesign] = useState(false);
  const [canvaMessage, setCanvaMessage] = useState("");

  const [driveOutputFolders, setDriveOutputFolders] = useState<DriveOutputFolder[]>([]);
  const [selectedOutputFolderId, setSelectedOutputFolderId] = useState("");
  const [isOutputFolderPickerOpen, setIsOutputFolderPickerOpen] = useState(false);

  const [generatedTextChanges, setGeneratedTextChanges] =
    useState<Record<string, string>>({});

  const [isGeneratingImage, setIsGeneratingImage] =
    useState(false);

  const [isLoadingInputs, setIsLoadingInputs] =
    useState(false);

  const [isTaggingImages, setIsTaggingImages] =
    useState(false);

  const [isGeneratingTemplate, setIsGeneratingTemplate] =
    useState(false);

  const [previewUrls, setPreviewUrls] =
    useState<Record<string, string>>({});

  const [error, setError] =
    useState("");

  const [urlError, setUrlError] =
    useState("");


  const [
    isReferenceListAtTop,
    setIsReferenceListAtTop,
  ] = useState(true);


  const [
    isReferenceListAtBottom,
    setIsReferenceListAtBottom,
  ] = useState(false);


  const uploadInputRef =
    useRef<HTMLInputElement | null>(
      null,
    );

  const referenceListRef =
    useRef<HTMLDivElement | null>(
      null,
    );


  /*
   * ------------------------------------------------------------
   * Load reference images from Google Drive and manual uploads
   * ------------------------------------------------------------
   */

  async function loadDriveOutputFolders(): Promise<DriveOutputFolder[]> {
    try {
      const response = await fetch(
        `${API_BASE_URL}/api/drive/folders`,
        { credentials: "include" },
      );

      const data = await response.json().catch(() => null);

      if (!response.ok) {
        throw new Error(
          String(
            data?.detail ||
              "Unable to load Google Drive folders.",
          ),
        );
      }

      const folders: DriveOutputFolder[] =
        Array.isArray(data?.folders)
          ? data.folders
          : [];

      setDriveOutputFolders(folders);

      setSelectedOutputFolderId((current) =>
        current &&
        folders.some((folder) => folder.id === current)
          ? current
          : String(folders[0]?.id || ""),
      );

      return folders;
    } catch (error) {
      console.error(
        "Loading Google Drive output folders failed:",
        error,
      );

      // Preserve an already-loaded list if a refresh fails.
      return driveOutputFolders;
    }
  }


  async function loadInputFiles() {
    setIsLoadingInputs(true);
    setError("");

    try {
      const response =
        await fetch(
          `${API_BASE_URL}/api/inputs`,
          {
            credentials: "include",
          },
        );

      if (!response.ok) {
        const errorData =
          await response
            .json()
            .catch(() => null);

        throw new Error(
          String(
            errorData?.detail ||
              "Unable to load reference images.",
          ),
        );
      }


      const data =
        await response.json();


      const files: InputFile[] =
        Array.isArray(data)
          ? data
          : Array.isArray(
              data?.files,
            )
            ? data.files
            : [];


      const images =
        files.filter(
          (
            file: InputFile,
          ) =>
            file.type ===
              "image" ||
            file.type ===
              "gif",
        );


      setInputFiles(
        images,
      );


      /*
       * Load the actual image bytes through
       * the backend and create browser blob URLs.
       *
       * This makes Drive previews independent
       * of browser caching and Google Drive's
       * response headers.
       */

      void preloadReferencePreviews(
        images,
      );

    } catch (err) {

      console.error(
        "Loading input files failed:",
        err,
      );


      setError(
        err instanceof Error
          ? err.message
          : "Unable to load reference images.",
      );


      setInputFiles([]);

    } finally {

      setIsLoadingInputs(
        false,
      );
    }
  }


  /*
   * ------------------------------------------------------------
   * FIX:
   * Automatically load Google Drive references
   * when Image Generator opens.
   * ------------------------------------------------------------
   */

  useEffect(() => {
    void loadInputFiles();
    void loadDriveOutputFolders();

    // Load Google Drive references once when
    // the workspace opens.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);


  async function preloadReferencePreviews(
    files: InputFile[],
  ) {

    const entries =
      await Promise.all(
        files.map(
          async (
            file,
          ) => {

            try {

              const response =
                await fetch(
                  resolveApiUrl(
                    file.url,
                  ),
                  {
                    cache:
                      "no-store",
                    credentials:
                      "include",
                  },
                );


              if (!response.ok) {
                throw new Error(
                  `Preview request failed (${response.status})`,
                );
              }


              const blob =
                await response.blob();


              if (
                !blob.size ||
                !blob.type.startsWith(
                  "image/",
                )
              ) {
                throw new Error(
                  "The backend did not return a valid image.",
                );
              }


              const objectUrl =
                URL.createObjectURL(
                  blob,
                );


              return [
                file.id,
                objectUrl,
              ] as const;

            } catch (
              previewError
            ) {

              console.error(
                "Reference preview failed:",
                file.name,
                previewError,
              );


              return null;
            }
          },
        ),
      );


    setPreviewUrls(
      (current) => {

        const next = {
          ...current,
        };


        entries.forEach(
          (entry) => {

            if (!entry) {
              return;
            }


            const [
              id,
              url,
            ] = entry;


            if (
              current[id] &&
              current[id] !==
                url
            ) {

              URL.revokeObjectURL(
                current[id],
              );
            }


            next[id] =
              url;
          },
        );


        return next;
      },
    );
  }


  useEffect(() => {

  ;

  return () => {

      Object.values(
        previewUrls,
      ).forEach(
        (url) => {

          URL.revokeObjectURL(
            url as string,
          );

        },
      );

    };

  }, [previewUrls]);


  /*
   * ------------------------------------------------------------
   * Generate AI tags for existing images
   * ------------------------------------------------------------
   */

  async function generateInputImageTags(
    filesToTag: InputFile[] =
      inputFiles,
  ) {

    if (!filesToTag.length) {
      return;
    }


    setIsTaggingImages(true);
    setError("");


    try {

      // The backend keeps API selection in memory. After a backend restart,
      // the browser can still have the selected IDs in its session state,
      // so synchronize them before starting any AI operation. This also
      // prevents a race between restoring the UI selection and /tag-all.
      if (selectedApiKeys.length === 0) {
        throw new Error(
          "No API key is selected. Select at least one API key before using the AI pipeline.",
        );
      }

      const selectionResponse = await fetch(
        `${API_BASE_URL}/api/api-keys/select`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ selected_ids: selectedApiKeys }),
          credentials: "include",
        },
      );

      const selectionData = await selectionResponse.json().catch(() => null);
      if (!selectionResponse.ok) {
        throw new Error(
          String(
            selectionData?.detail ||
              "Unable to synchronize the selected API keys.",
          ),
        );
      }

      const response =
        await fetch(
          `${API_BASE_URL}/api/inputs/tag-all`,
          {
            method: "POST",
            credentials: "include",
          },
        );


      const data =
        await response
          .json()
          .catch(() => null);


      if (!response.ok) {

        throw new Error(
          String(
            data?.detail ||
              "Unable to generate image tags.",
          ),
        );
      }


      const tagMapById:
        Record<
          string,
          string
        > = {};


      const tagMapByFilename:
        Record<
          string,
          string
        > = {};


      const errorMapById:
        Record<
          string,
          string
        > = {};


      if (
        Array.isArray(
          data?.results,
        )
      ) {

        data.results.forEach(
          (
            result: {
              id?: string;
              filename?: string;
              tag?: string;
            },
          ) => {

            if (
              result.tag &&
              result.id
            ) {

              tagMapById[
                result.id
              ] =
                result.tag;
            }


            if (
              result.tag &&
              result.filename
            ) {

              tagMapByFilename[
                result.filename
              ] =
                result.tag;
            }

          },
        );

      }


      if (
        Array.isArray(
          data?.errors,
        )
      ) {

        data.errors.forEach(
          (
            result: {
              id?: string;
              filename?: string;
              error?: string;
            },
          ) => {

            const message =
              String(
                result.error ||
                  "Gemini could not generate a tag.",
              );


            if (result.id) {

              errorMapById[
                result.id
              ] =
                message;
            }


            if (
              result.filename
            ) {

              errorMapById[
                `filename:${result.filename}`
              ] =
                message;
            }

          },
        );

      }


      setInputFiles(
        (currentFiles) =>
          currentFiles.map(
            (file) => ({
              ...file,

              tag:
                tagMapById[
                  file.id
                ] ||
                tagMapByFilename[
                  file.name
                ] ||
                file.tag,

              tagError:
                errorMapById[
                  file.id
                ] ||
                errorMapById[
                  `filename:${file.name}`
                ],
            }),
          ),
      );


      setReference(
        (currentReference) => {

          if (!currentReference) {
            return currentReference;
          }


          const updatedTag =
            (
              selectedInputId
                ? tagMapById[
                    selectedInputId
                  ]
                : undefined
            ) ||
            tagMapByFilename[
              currentReference.name
            ];


          if (!updatedTag) {
            return currentReference;
          }


          return {
            ...currentReference,
            tag: updatedTag,
          };
        },
      );


      if (
        Array.isArray(
          data?.errors,
        ) &&
        data.errors.length >
          0
      ) {

        console.error(
          "Gemini tagging errors:",
          data.errors,
        );


        const firstError =
          data.errors[0];


        setError(
          `Image tagging issue: ${String(
            firstError?.error ||
              "Gemini could not tag one or more images.",
          )}`,
        );
      }

    } catch (err) {

      console.error(
        "Image tagging failed:",
        err,
      );


      setError(
        err instanceof Error
          ? err.message
          : "Unable to generate image tags.",
      );

    } finally {

      setIsTaggingImages(
        false,
      );
    }
  }


  function getDriveReferenceNumber(file: InputFile): number | null {
    if (file.source !== "google-drive") {
      return null;
    }

    const driveFiles = inputFiles.filter(
      (item) => item.source === "google-drive",
    );

    const index = driveFiles.findIndex(
      (item) => item.id === file.id,
    );

    return index >= 0 ? index + 1 : null;
  }


  /*
   * ------------------------------------------------------------
   * Automatically tag loaded images
   * ------------------------------------------------------------
   */

  useEffect(() => {
    // Do not call the AI tagging endpoint until at least one AI API key
    // has been selected. API selection is the source of truth for the
    // complete AI pipeline.
    if (
      inputFiles.length > 0 &&
      selectedApiKeys.length > 0
    ) {
      void generateInputImageTags(inputFiles);
    }

    // generateInputImageTags is intentionally omitted because it is recreated
    // on render. The effect is driven by the input count and selected-key count.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [inputFiles.length, selectedApiKeys.length]);


  /*
   * ------------------------------------------------------------
   * Reference selection
   * ------------------------------------------------------------
   */

  function handleReferenceSelection(
    file: InputFile,
  ) {
    const wasSelected = selectedInputIds.includes(file.id);
    const nextSelectedIds = wasSelected
      ? selectedInputIds.filter((id) => id !== file.id)
      : [...selectedInputIds, file.id];

    setSelectedInputIds(nextSelectedIds);

    const nextPrimaryId = nextSelectedIds[0] || "";
    const primaryFile = inputFiles.find((item) => item.id === nextPrimaryId);

    if (!primaryFile) {
      setReference(null);
      setTemplatePrompt("");
      setGeneratedImageUrl("");
      setGeneratedImageFilename("");
      setGeneratedImageModel("");
      setError("");
      setTemplateResult(null);
      return;
    }

    const extension = primaryFile.name.includes(".")
      ? `.${primaryFile.name.split(".").pop()?.toLowerCase()}`
      : "";

    const selectedReference: ReferenceData = {
      type: getReferenceType(extension, primaryFile.mimeType),
      name: primaryFile.name,
      url: previewUrls[primaryFile.id] || resolveApiUrl(primaryFile.url),
      size: primaryFile.size,
      mimeType: primaryFile.mimeType,
      source: primaryFile.source === "google-drive"
        ? "google-drive"
        : primaryFile.source === "manual-upload"
          ? "upload"
          : "input-folder",
      sourceId: primaryFile.source === "google-drive"
        ? primaryFile.id.replace(/^drive:/, "")
        : primaryFile.name,
      tag: primaryFile.tag,
    };

    setReference(selectedReference);
    setPromptMode("manual");
    setGeneratedImageUrl("");
    setGeneratedImageFilename("");
    setGeneratedImageDescription("");
    setSocialDescriptions({});
    setSocialDescriptionFilename("");
    setSocialDescriptionPreviewContent("");
    setSocialDescriptionSaved(false);
    setSocialDescriptionSaveMessage("");
    setIsSocialDescriptionPreviewOpen(false);
    setGeneratedImageModel("");
    setSocialDescriptions({});
    setSocialDescriptionError("");
    setSocialDescriptionSaved(false);
    setSocialDescriptionSaveMessage("");
    setIsSocialDescriptionPreviewOpen(false);
    setSocialDescriptions({});
    setSocialDescriptionError("");
    setSocialDescriptionSaved(false);
    setSocialDescriptionSaveMessage("");
    setIsSocialDescriptionPreviewOpen(false);
    setGeneratedImageProvider("");
    setGeneratedImageSaved(false);
    setGeneratedImageSaveMessage("");
    setError("");

    // Template generation is disabled. Selected references are passed
    // directly to image generation and addressed by their numbers.
    setTemplateResult(null);
    setTemplateApiProvider("");
    setTemplateApiModel("");
  }


  /*
   * ------------------------------------------------------------
   * Reference list scroll state
   * ------------------------------------------------------------
   */

  function updateReferenceScrollState() {

    const element =
      referenceListRef.current;


    if (!element) {
      return;
    }


    const atTop =
      element.scrollTop <=
      2;


    const atBottom =
      element.scrollTop +
        element.clientHeight >=
      element.scrollHeight -
        2;


    setIsReferenceListAtTop(
      atTop,
    );

    setIsReferenceListAtBottom(
      atBottom,
    );
  }


  function handleReferenceListScroll(
    event: UIEvent<HTMLDivElement>,
  ) {

    const element =
      event.currentTarget;


    const atTop =
      element.scrollTop <=
      2;


    const atBottom =
      element.scrollTop +
        element.clientHeight >=
      element.scrollHeight -
        2;


    setIsReferenceListAtTop(
      atTop,
    );

    setIsReferenceListAtBottom(
      atBottom,
    );
  }


  function scrollReferenceList(
    direction:
      | "up"
      | "down",
  ) {

    if (
      !referenceListRef.current
    ) {
      return;
    }


    referenceListRef.current.scrollBy(
      {
        top:
          direction ===
          "down"
            ? 180
            : -180,

        behavior:
          "smooth",
      },
    );


    window.setTimeout(
      updateReferenceScrollState,
      250,
    );
  }


  /*
   * ------------------------------------------------------------
   * Reference upload
   * ------------------------------------------------------------
   */

  function handleUploadClick() {
    uploadInputRef.current?.click();
  }


  async function handleUploadedFile(
    file: File,
    appendToSelection = false,
    currentSelectedIds: string[] = [],
  ): Promise<string | null> {

    const extension =
      file.name.includes(".")
        ? `.${file.name
            .split(".")
            .pop()
            ?.toLowerCase()}`
        : "";


    const type =
      getReferenceType(
        extension,
        file.type,
      );


    /*
     * Images and GIFs are stored as
     * manual uploads.
     *
     * Google Drive remains the primary
     * reference source.
     */

    if (
      type === "image" ||
      type === "gif"
    ) {

      setError("");
      setIsTaggingImages(
        true,
      );


      try {

        const formData =
          new FormData();


        formData.append(
          "file",
          file,
        );


        const response =
          await fetch(
            `${API_BASE_URL}/api/inputs/upload`,
            {
              method:
                "POST",

              body:
                formData,
            credentials: "include",
          },
          );


        const data =
          await response.json();


        if (!response.ok) {

          throw new Error(
            data?.detail ||
              "Unable to upload and tag image.",
          );
        }


        const nextSelectedIds = appendToSelection
          ? [...currentSelectedIds, data.id]
          : [data.id];

        setSelectedInputIds(nextSelectedIds);


        setTemplateResult(
          null,
        );


        const uploadedReference:
          ReferenceData = {

          type,

          name:
            data.name,

          url:
            `${API_BASE_URL}${data.url}`,

          size:
            data.size,

          mimeType:
            data.mimeType ||
            data.mime_type ||
            file.type,

          source:
            "upload",

          /*
           * The upload endpoint stores the image in manual_uploads.
           * The backend template endpoint expects the stored filename.
           */
          sourceId:
            data.name,

          tag:
            data.tag,
        };


        // Keep the first uploaded image as the primary preview/reference,
        // while all uploaded images remain selected for multi-reference generation.
        if (!appendToSelection || currentSelectedIds.length === 0) {
          setReference(
            uploadedReference,
          );
        }


        // Template generation is temporarily disabled.


        await loadInputFiles();


        setShowReferenceModal(
          false,
        );

        setShowUrlInput(
          false,
        );

        return String(data.id);

      } catch (err) {

        console.error(
          "Image upload and Gemini tagging failed:",
          err,
        );


        setError(
          err instanceof Error
            ? err.message
            : "Unable to upload and tag image.",
        );

        return null;

      } finally {

        setIsTaggingImages(
          false,
        );
      }


    }


    /*
     * Keep PDF/video uploads
     * as local references.
     */

    const objectUrl =
      URL.createObjectURL(
        file,
      );


    setSelectedInputIds([]);

    setTemplateResult(
      null,
    );

    setError("");


    setReference({
      type,

      name:
        file.name,

      url:
        objectUrl,

      size:
        file.size,

      mimeType:
        file.type,

      source:
        "upload",
    });


    setShowReferenceModal(
      false,
    );

    setShowUrlInput(
      false,
    );

    return null;
  }


  async function handleFileInputChange(
    event: ChangeEvent<HTMLInputElement>,
  ) {
    const files = Array.from(event.target.files || []) as File[];

    if (files.length === 0) {
      event.target.value = "";
      return;
    }

    try {
      // Upload every selected image and keep all successful uploads selected.
      // This preserves the existing single-upload flow while allowing the user
      // to choose multiple reference images in one action.
      let selectedIds = [...selectedInputIds];

      for (const file of files) {
        const uploadedId = await handleUploadedFile(
          file,
          selectedIds.length > 0,
          selectedIds,
        );

        if (uploadedId && !selectedIds.includes(uploadedId)) {
          selectedIds = [...selectedIds, uploadedId];
          setSelectedInputIds(selectedIds);
        }
      }
    } finally {
      event.target.value = "";
    }
  }


  /*
   * ------------------------------------------------------------
   * Drag and drop
   * ------------------------------------------------------------
   */

  function handleDrop(
    event: DragEvent<HTMLDivElement>,
  ) {

    event.preventDefault();


    const file =
      event.dataTransfer
        .files?.[0];


    if (file) {

      void handleUploadedFile(
        file,
      );
    }
  }


  /*
   * ------------------------------------------------------------
   * External URL
   * ------------------------------------------------------------
   */

  function handleExternalUrl() {

    const url =
      externalUrl.trim();


    if (!url) {

      setUrlError(
        "Please enter a URL.",
      );

      return;
    }


    try {

      new URL(url);

    } catch {

      setUrlError(
        "Please enter a valid URL.",
      );

      return;
    }


    setUrlError("");

    setSelectedInputIds([]);

    setTemplateResult(
      null,
    );

    setError("");


    let externalReference:
      ReferenceData;


    if (
      isYouTubeUrl(
        url,
      )
    ) {

      externalReference = {

        type:
          "youtube",

        name:
          "YouTube Reference",

        url,

        source:
          "external-url",

        sourceId:
          url,
      };

    } else if (
      isImageUrl(
        url,
      )
    ) {

      externalReference = {

        type:
          "image-link",

        name:
          "External Image",

        url,

        source:
          "external-url",

        sourceId:
          url,
      };

    } else {

      externalReference = {

        type:
          "image-link",

        name:
          "External Reference",

        url,

        source:
          "external-url",

        sourceId:
          url,
      };
    }


    setReference(
      externalReference,
    );


    // Template generation is temporarily disabled.


    setExternalUrl(
      "",
    );

    setShowUrlInput(
      false,
    );

    setShowReferenceModal(
      false,
    );
  }


  /*
   * ------------------------------------------------------------
   * Remove reference
   * ------------------------------------------------------------
   */

  function handleRemoveReference() {

    if (
      reference?.source ===
        "upload" &&
      reference.url.startsWith(
        "blob:",
      )
    ) {

      URL.revokeObjectURL(
        reference.url,
      );
    }


    setReference(
      null,
    );

    setSelectedInputIds([]);

    setTemplatePrompt(
      "",
    );

    setPromptMode(
      "manual",
    );

    setTemplateResult(
      null,
    );

    setGeneratedImageUrl("");
    setGeneratedImageFilename("");
    setGeneratedImageDescription("");
    setSocialDescriptions({});
    setSocialDescriptionFilename("");
    setSocialDescriptionPreviewContent("");
    setSocialDescriptionSaved(false);
    setSocialDescriptionSaveMessage("");
    setIsSocialDescriptionPreviewOpen(false);
    setGeneratedImageModel("");

    setError("");
  }


  /*
   * ------------------------------------------------------------
   * Automatic template generation
   * ------------------------------------------------------------
   */

  async function generateTemplateForReference(
    currentReference: ReferenceData,
  ) {

    if (selectedApiKeys.length === 0) {
      setTemplateResult(null);
      setTemplateApiProvider("");
      setTemplateApiModel("");
      return;
    }

    if (
      currentReference.type ===
        "pdf" ||
      currentReference.type ===
        "video"
    ) {

      setTemplateResult(
        null,
      );


      setError(
        "Template generation currently supports images, GIFs and YouTube references.",
      );


      return;
    }


    setError("");

    setIsGeneratingTemplate(
      true,
    );

    setTemplateResult(
      null,
    );
    setTemplateApiProvider("");
    setTemplateApiModel("");


    try {

      const result =
        await generateTemplate({
          type:
            currentReference.type,

          name:
            currentReference.name,

          /*
           * `url` is only the browser preview URL.
           * `sourceId` is the value the backend uses to locate the
           * actual reference.
           */
          url:
            currentReference.url,

          source:
            currentReference.source as any,

          sourceId:
            currentReference.sourceId,

          mimeType:
            currentReference.mimeType,
        });


      setTemplateResult(
        result,
      );

      const templateMetadata = result as GenerateTemplateResponse & {
        provider?: string;
        model?: string;
      };
      setTemplateApiProvider(String(templateMetadata.provider || ""));
      setTemplateApiModel(String(templateMetadata.model || ""));

    } catch (err) {

      console.error(
        "Automatic template generation failed:",
        err,
      );


      setError(
        err instanceof Error
          ? err.message
          : "Unable to generate template from the reference.",
      );

    } finally {

      setIsGeneratingTemplate(
        false,
      );
    }
  }


  /*
   * ------------------------------------------------------------
   * Template generation temporarily disabled
   * ------------------------------------------------------------
   * Keep the existing template state/functions in place so the feature
   * can be re-enabled later, but do not invoke the template-generation
   * pipeline anywhere in the current flow.
   */


  /*
   * ------------------------------------------------------------
   * Generate AI prompt
   * ------------------------------------------------------------
   */

  function handleGeneratePrompt() {
    setIsGeneratingPrompt(false);
    setPromptMode("manual");
    setPromptApiProvider("");
    setPromptApiModel("");
    setError("AI prompt generation is temporarily disabled. Enter the content prompt manually.");
  }

  /*
   * ------------------------------------------------------------
   * Generate output image
   * ------------------------------------------------------------
   */

  async function handleGenerateImage() {
    if (selectedInputIds.length === 0 && !reference) {
      setError("Please select at least one reference image.");
      return;
    }

    if (!templatePrompt.trim()) {
      setError("Enter a content prompt before generating the output image.");
      return;
    }

    if (reference && (reference.type === "pdf" || reference.type === "video" || reference.type === "youtube")) {
      setError("Image generation currently requires image references.");
      return;
    }

    setError("");
    setPromptMode("manual");
    setGeneratedImageUrl("");
    setGeneratedImageFilename("");
    setGeneratedImageModel("");
    setGeneratedImageProvider("");
    setGeneratedImageSaved(false);
    setGeneratedImageSaveMessage("");
    setIsOutputFolderPickerOpen(false);
    setGeneratedTextChanges({});
    setIsGeneratingImage(true);

    try {
      if (selectedApiKeys.length === 0) {
        throw new Error(
          "No API key is selected. Select at least one API key before generating an image.",
        );
      }

      const apiStatusResponse = await fetch(
        `${API_BASE_URL}/api/api-keys/status`,
        { credentials: "include" },
      );
      const apiStatus = await apiStatusResponse.json().catch(() => null);

      if (!apiStatusResponse.ok || !apiStatus?.configured) {
        throw new Error(
          String(
            apiStatus?.detail ||
              "The API key configuration is no longer available. Please return to API Setup and upload the API key file again.",
          ),
        );
      }

      // The current UI selection is always authoritative. Do not use a cached
      // pipeline key from a previous generation.
      const selectionResponse = await fetch(
        `${API_BASE_URL}/api/api-keys/select`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ selected_ids: selectedApiKeys }),
          credentials: "include",
        },
      );
      const selectionData = await selectionResponse.json().catch(() => null);
      if (!selectionResponse.ok) {
        throw new Error(
          String(selectionData?.detail || "Unable to synchronize the selected API keys."),
        );
      }

      const selectedNames = Array.isArray(selectionData?.selected)
        ? selectionData.selected
        : [];
      if (selectedNames.length === 0) {
        throw new Error(
          "None of the selected API keys are available on the backend.",
        );
      }

      // Build the reference list from the actual selected reference IDs.
      // External URL/manual reference remains supported when no Drive/input
      // item is selected. Google Drive IDs are sent as IDs, never preview URLs.
      const selectedReferences = selectedInputIds
        .map((id) => {
          const file = inputFiles.find((item) => item.id === id);
          if (!file) return null;

          const sourceType =
            file.source === "google-drive"
              ? "google-drive"
              : file.source === "manual-upload"
                ? "upload"
                : "input-folder";

          return {
            number:
              getDriveReferenceNumber(file) ??
              selectedInputIds.indexOf(id) + 1,
            source_type: sourceType,
            source:
              file.source === "google-drive"
                ? file.id.replace(/^drive:/i, "")
                : file.name,
            filename: file.name,
            content_type: file.mimeType || "",
          };
        })
        .filter(Boolean);

      if (selectedReferences.length === 0 && reference) {
        const sourceType =
          reference.source === "google-drive"
            ? "google-drive"
            : reference.source === "upload"
              ? "upload"
              : reference.source === "input-folder"
                ? "input-folder"
                : "external-url";

        const source = reference.sourceId || reference.url;
        if (!source || source.startsWith("blob:")) {
          throw new Error(
            "The selected reference is not available to the backend.",
          );
        }

        selectedReferences.push({
          number: 1,
          source_type: sourceType,
          source,
          filename: reference.name || "reference.png",
          content_type: reference.mimeType || "",
        });
      }

      if (selectedReferences.length === 0) {
        throw new Error("At least one reference image is required.");
      }

      if (selectedReferences.length > 16) {
        throw new Error("A maximum of 16 reference images can be selected.");
      }

      const firstReference = selectedReferences[0];
      const formData = new FormData();
      formData.append("source_type", firstReference.source_type);
      formData.append("source", firstReference.source);
      formData.append("filename", firstReference.filename);
      formData.append("content_type", firstReference.content_type);
      formData.append("references_json", JSON.stringify(selectedReferences));
      formData.append("prompt", templatePrompt.trim());
      formData.append("template_json", "{}");

      const response = await fetch(
        `${API_BASE_URL}/api/images/generate`,
        {
          method: "POST",
          body: formData,
          credentials: "include",
        },
      );

      const data = await response.json().catch(() => null);

      if (!response.ok) {
        throw new Error(
          String(data?.detail || "Unable to generate the output image."),
        );
      }

      if (!data?.image_url) {
        throw new Error("Image generation completed without an output image.");
      }

      setGeneratedImageUrl(resolveApiUrl(data.image_url));
      setGeneratedImageFilename(String(data.filename || ""));
      setGeneratedImageDescription(
        typeof data?.description === "string" && data.description.trim()
          ? data.description.trim()
          : `Generated image based on the selected ${selectedReferences.length} reference image${selectedReferences.length === 1 ? "" : "s"} and prompt: ${templatePrompt.trim()}`,
      );
      setGeneratedImageModel(String(data.model || ""));
      setGeneratedImageProvider(String(data.provider || ""));
      setGeneratedImageSaved(false);
      setGeneratedImageSaveMessage("");
      setCanvaEditUrl("");
      setCanvaDesignId("");
      setCanvaMessage("");
      setGeneratedTextChanges(
        data?.changes && typeof data.changes === "object"
          ? data.changes
          : {},
      );
    } catch (err) {
      console.error("Image generation failed:", err);
      setError(
        err instanceof Error
          ? err.message
          : "Unable to generate the output image.",
      );
    } finally {
      setIsGeneratingImage(false);
    }
  }


  function getReferencePreviewColumns(count: number): number {
    // Keep the reference previews readable. Two columns are used for the
    // first six references so the tiles do not collapse into tiny thumbnails.
    if (count <= 1) return 1;
    if (count <= 6) return 2;
    if (count <= 12) return 3;
    return 4;
  }

  function getReferencePreviewHeight(count: number): number {
    // The preview panel controls the actual tile height so every reference
    // uses the same available grid space. Keep this only as an inline fallback.
    if (count <= 1) return 220;
    return 120;
  }


  /*
   * ------------------------------------------------------------
   * Reference preview
   * ------------------------------------------------------------
   */

  function renderReferencePreview(
    currentReference: ReferenceData,
  ) {

    if (
      currentReference.type ===
      "youtube"
    ) {

      return (
        <div className="media-preview youtube-preview">

          <div className="youtube-icon">
            ▶
          </div>

          <div className="youtube-preview-text">

            <strong>
              YouTube Reference
            </strong>

            <span>
              External video reference
            </span>

          </div>

        </div>
      );
    }


    if (
      currentReference.type ===
      "pdf"
    ) {

      return (
        <div className="media-preview document-preview">

          <div className="document-icon">
            PDF
          </div>

          <div className="document-name">
            {currentReference.name}
          </div>

        </div>
      );
    }


    if (
      currentReference.type ===
      "video"
    ) {

      return (
        <video
          src={
            currentReference.url
          }
          controls
          className="reference-media"
        />
      );
    }


    return (
      <img
        src={
          currentReference.url
        }
        alt={
          currentReference.name
        }
        className="reference-media"
        loading="eager"
        decoding="async"
        onError={(event) => {

          console.error(
            "Reference preview failed:",
            currentReference.url,
          );


          event.currentTarget.style.display =
            "none";
        }}
      />
    );
  }


  /*
   * ------------------------------------------------------------
   * UI
   * ------------------------------------------------------------
   */


const handleOpenGeneratedImageInCanva = async () => {
  if (!generatedImageFilename || isCreatingCanvaDesign) return;

  setIsCreatingCanvaDesign(true);
  setCanvaMessage("");

  // Open the tab immediately from the click event so browser popup blockers
  // do not reject the later Canva navigation after asynchronous API calls.
  const canvaWindow = window.open("about:blank", "_blank");

  try {
    const statusResponse = await fetch(
      `${API_BASE_URL}/api/canva/connect/oauth/status`,
      { credentials: "include" },
    );
    const status = await statusResponse.json().catch(() => null);

    if (!statusResponse.ok) {
      throw new Error(
        String(
          status?.detail ||
            "Unable to check Canva connection.",
        ),
      );
    }

    if (!status?.configured) {
      throw new Error(
        "Canva Connect is not configured. Set CANVA_CLIENT_ID, CANVA_CLIENT_SECRET, and CANVA_CONNECT_REDIRECT_URI in the backend environment.",
      );
    }

    if (!status?.authenticated) {
      const authResponse = await fetch(
        `${API_BASE_URL}/api/canva/connect/oauth/start?filename=${encodeURIComponent(
          generatedImageFilename,
        )}`,
        { credentials: "include" },
      );
      const authData = await authResponse.json().catch(() => null);

      if (!authResponse.ok || !authData?.authorization_url) {
        throw new Error(
          String(
            authData?.detail ||
              "Unable to start Canva authorization.",
          ),
        );
      }

      if (canvaWindow) {
        canvaWindow.location.href = String(
          authData.authorization_url,
        );
      } else {
        window.open(
          String(authData.authorization_url),
          "_blank",
          "noopener,noreferrer",
        );
      }

      setCanvaMessage(
        "Canva authorization opened in a new tab. Approve access. After authorization, the generated image will open in Canva automatically.",
      );
      return;
    }

    const formData = new FormData();
    formData.append(
      "filename",
      generatedImageFilename,
    );
    formData.append(
      "design_type",
      "poster",
    );

    const response = await fetch(
      `${API_BASE_URL}/api/canva/create-from-generated-image`,
      {
        method: "POST",
        body: formData,
        credentials: "include",
      },
    );

    const data = await response
      .json()
      .catch(() => null);

    if (!response.ok) {
      throw new Error(
        String(
          data?.detail ||
            "Unable to create the editable Canva design.",
        ),
      );
    }

    if (!data?.edit_url || !data?.design_id) {
      throw new Error(
        "Canva did not return the design ID and edit URL.",
      );
    }

    setCanvaEditUrl(String(data.edit_url));
    setCanvaDesignId(String(data.design_id));
    setCanvaMessage(
      String(
        data.message ||
          "Editable Canva design created successfully.",
      ),
    );

    if (canvaWindow) {
      canvaWindow.location.href = String(data.edit_url);
    } else {
      window.open(
        String(data.edit_url),
        "_blank",
        "noopener,noreferrer",
      );
    }
  } catch (error) {
    if (canvaWindow && !canvaWindow.closed) {
      try {
        canvaWindow.close();
      } catch {
        // Ignore popup cleanup errors.
      }
    }

    console.error(
      "Canva design creation failed:",
      error,
    );
    setCanvaEditUrl("");
    setCanvaDesignId("");
    setCanvaMessage(
      error instanceof Error
        ? error.message
        : "Unable to create the editable Canva design.",
    );
  } finally {
    setIsCreatingCanvaDesign(false);
  }
};

  async function handleGenerateSocialDescriptions() {
    if (!generatedImageFilename || isGeneratingSocialDescriptions) {
      return;
    }

    setIsGeneratingSocialDescriptions(true);
    setSocialDescriptionError("");
    setSocialDescriptionSaved(false);
    setSocialDescriptionSaveMessage("");

    try {
      const formData = new FormData();
      formData.append("filename", generatedImageFilename);
      formData.append("prompt", templatePrompt.trim());
      formData.append("template_json", "{}");

      const response = await fetch(
        `${API_BASE_URL}/api/social-media/generate`,
        {
          method: "POST",
          body: formData,
          credentials: "include",
        },
      );

      const data = await response.json().catch(() => null);

      if (!response.ok) {
        throw new Error(
          String(
            data?.detail ||
              "Unable to generate the social media descriptions.",
          ),
        );
      }

      setSocialDescriptionFilename(
        String(data?.social_media_filename || ""),
      );
      setSocialDescriptionPreviewContent(
        String(data?.content || "").trim(),
      );

      if (
        data?.descriptions &&
        typeof data.descriptions === "object"
      ) {
        setSocialDescriptions(
          data.descriptions as Record<
            string,
            SocialDescriptionItem
          >,
        );
      } else {
        const rawContent =
          String(data?.content || "").trim();

        const limits: Record<string, number> = {
          "[LINKEDIN]": 3000,
          "[X / TWITTER]": 280,
          "[FACEBOOK]": 10000,
          "[INSTAGRAM]": 2200,
        };

        const headings = Object.keys(limits);

        const parsed: Record<
          string,
          SocialDescriptionItem
        > = {};

        headings.forEach((heading, index) => {
          const startIndex =
            rawContent.indexOf(heading);

          if (startIndex < 0) {
            return;
          }

          const contentStart =
            startIndex + heading.length;

          const nextPositions = headings
            .slice(index + 1)
            .map((nextHeading) =>
              rawContent.indexOf(
                nextHeading,
                contentStart,
              ),
            )
            .filter(
              (position) => position >= 0,
            );

          const endIndex =
            nextPositions.length > 0
              ? Math.min(...nextPositions)
              : rawContent.length;

          const content =
            rawContent
              .slice(
                contentStart,
                endIndex,
              )
              .trim();

          const platform =
            heading
              .replace(/^\[|\]$/g, "")
              .replace(
                "X / TWITTER",
                "X / Twitter",
              )
              .replace(
                "LINKEDIN",
                "LinkedIn",
              )
              .replace(
                "FACEBOOK",
                "Facebook",
              )
              .replace(
                "INSTAGRAM",
                "Instagram",
              );

          if (content) {
            parsed[platform] = {
              text: content,
              character_count:
                content.length,
              character_limit:
                limits[heading],
            };
          }
        });

        if (!Object.keys(parsed).length) {
          throw new Error(
            "The selected API returned no recognizable social media descriptions.",
          );
        }

        setSocialDescriptions(parsed);
      }

      if (!String(data?.social_media_filename || "").trim()) {
        setSocialDescriptionFilename(
          `${generatedImageFilename.replace(/\.[^.]+$/, "")}_description.txt`,
        );
      }

      setIsSocialDescriptionPreviewOpen(false);
    } catch (error) {
      console.error(
        "Social media description generation failed:",
        error,
      );

      setSocialDescriptionError(
        error instanceof Error
          ? error.message
          : "Unable to generate the social media descriptions.",
      );
    } finally {
      setIsGeneratingSocialDescriptions(false);
    }
  }

  async function handleSaveSocialDescriptionsToDrive() {
    if (
      !generatedImageFilename ||
      Object.keys(socialDescriptions).length === 0 ||
      isGeneratingSocialDescriptions ||
      socialDescriptionSaved
    ) {
      return;
    }

    setSocialDescriptionError("");
    setSocialDescriptionSaveMessage("");

    try {
      const formData = new FormData();

      const descriptionFilename =
        socialDescriptionFilename ||
        `${generatedImageFilename.replace(
          /\.[^.]+$/,
          "",
        )}_description.txt`;

      formData.append("filename", descriptionFilename);

      const response = await fetch(
        `${API_BASE_URL}/api/social-media/save-to-drive`,
        {
          method: "POST",
          body: formData,
          credentials: "include",
        },
      );

      const data =
        await response
          .json()
          .catch(() => null);

      if (!response.ok) {
        throw new Error(
          String(
            data?.detail ||
              "Unable to save the description to Google Drive.",
          ),
        );
      }

      setSocialDescriptionSaved(true);
      setSocialDescriptionSaveMessage(
        String(
          data?.message ||
            "Description saved to Google Drive / outputs.",
        ),
      );
    } catch (error) {
      console.error(
        "Saving social media descriptions failed:",
        error,
      );

      setSocialDescriptionError(
        error instanceof Error
          ? error.message
          : "Unable to save the description.",
      );
    }
  }


  const handleSaveGeneratedImageToDrive = async () => {
    if (!generatedImageFilename || isSavingGeneratedImage) return;

    // First click on Save opens the folder picker. The actual upload only
    // happens after the user chooses a folder and confirms.
    if (!isOutputFolderPickerOpen) {
      const folders =
        await loadDriveOutputFolders();

      if (
        folders.length > 0 &&
        !selectedOutputFolderId
      ) {
        setSelectedOutputFolderId(
          String(folders[0].id || ""),
        );
      }

      setGeneratedImageSaveMessage("");
      setIsOutputFolderPickerOpen(true);
      return;
    }

    if (!selectedOutputFolderId) {
      setGeneratedImageSaveMessage(
        "Select a Google Drive output folder before saving.",
      );
      return;
    }

    setIsSavingGeneratedImage(true);
    setGeneratedImageSaveMessage("");
    try {
      const formData = new FormData();
      formData.append("filename", generatedImageFilename);
      if (selectedOutputFolderId) formData.append("folder_id", selectedOutputFolderId);

      const response = await fetch(
        `${API_BASE_URL}/api/images/save-to-drive`,
        { method: "POST", body: formData, credentials: "include" },
      );
      const data = await response.json().catch(() => null);

      if (!response.ok) {
        throw new Error(
          String(
            data?.detail ||
              "Unable to save the generated image to Google Drive.",
          ),
        );
      }

      setGeneratedImageSaved(true);
      setGeneratedImageSaveMessage(
        String(data?.message || "Saved to Google Drive / outputs."),
      );
    } catch (error) {
      setGeneratedImageSaved(false);
      setGeneratedImageSaveMessage(
        error instanceof Error
          ? error.message
          : "Unable to save the generated image.",
      );
    } finally {
      setIsSavingGeneratedImage(false);
    }
  };

  return (
    <div className="app-shell">


      <header className="top-header">

        <div className="brand-area">

          <div className="brand-mark">
            ✦
          </div>

          <div>

            <div className="brand-title">
              Image Generator
            </div>

            <div className="brand-subtitle">
              Reference-driven creative
              workspace
            </div>

          </div>

        </div>


        <div className="header-actions">

          <div
            className="active-api-summary"
            aria-label="Available API services. Click to select or deselect."
            title="Select the API keys that may be used by the pipeline"
            style={{
              display: "flex",
              alignItems: "center",
              gap: "8px",
              flexWrap: "wrap",
              maxWidth: "min(58vw, 700px)",
              justifyContent: "flex-end",
            }}
          >
            {availableApiServices.length > 0 ? (
              availableApiServices.map((service) => {
                const selected = selectedApiKeys.includes(service.id);
                return (
                  <button
                    key={service.id}
                    type="button"
                    className={`active-api-service ${selected ? "selected" : ""}`}
                    aria-pressed={selected}
                    onClick={() => {
                      const next = selected
                        ? selectedApiKeys.filter((id) => id !== service.id)
                        : [...selectedApiKeys, service.id];
                      onApiSelectionChange(next);
                    }}
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: "7px",
                      padding: "6px 10px",
                      borderRadius: "999px",
                      border: selected
                        ? "1px solid rgba(120, 180, 255, 0.95)"
                        : "1px solid rgba(255,255,255,0.12)",
                      background: selected
                        ? "rgba(80, 140, 255, 0.18)"
                        : "rgba(255,255,255,0.055)",
                      boxShadow: selected
                        ? "0 0 0 1px rgba(120, 180, 255, 0.22)"
                        : "none",
                      color: "inherit",
                      whiteSpace: "nowrap",
                      fontSize: "12px",
                      fontWeight: 600,
                      lineHeight: 1,
                      cursor: "pointer",
                    }}
                  >
                    <span aria-hidden="true">
                      {getApiServiceIcon(service.name, service.keyName, 18)}
                    </span>
                    <span>{service.name}</span>
                  </button>
                );
              })
            ) : (
              <span>APIs 0</span>
            )}
          </div>


          <button
            type="button"
            className="header-button"
            onClick={onBackToApiSetup}
          >
            ← API Setup
          </button>

          <button
            type="button"
            className="header-button"
            onClick={onBackToHome}
          >
            ⌂ Home
          </button>

          <button
            type="button"
            className="header-button"
          >
            Documentation
          </button>


          <button
            type="button"
            className="settings-button"
            aria-label="Settings"
          >
            ⚙
          </button>

        </div>

      </header>


      <main className="main-content">


        <section className="hero-section">

          <div>

            <h1>
              Transform references into
              reusable visual content.
            </h1>

            <p>
              Select a reference, define the
              template, and prepare it for
              image generation.
            </p>

          </div>

        </section>


        <section className="workflow-section">


          <div className="workflow-step active">

            <span className="workflow-number">
              01
            </span>

            <span className="workflow-label">
              Reference
            </span>

          </div>


          <div className="workflow-line" />


          <div className="workflow-step">

            <span className="workflow-number">
              02
            </span>

            <span className="workflow-label">
              Template
            </span>

          </div>


          <div className="workflow-line" />


          <div className="workflow-step">

            <span className="workflow-number">
              03
            </span>

            <span className="workflow-label">
              Image Builder
            </span>

          </div>


          <div className="workflow-line" />


          <div className="workflow-step">

            <span className="workflow-number">
              04
            </span>

            <span className="workflow-label">
              Output
            </span>

          </div>

        </section>


        <section className="workspace-grid">


          {/* ====================================================
              REFERENCE
              ==================================================== */}

          <article className="workspace-card">


            <div className="card-header">

              <div>

                <div className="section-kicker">
                  REFERENCE
                </div>

                <h2>
                  Reference Image
                </h2>

              </div>


              {reference && (
                <span className="status-pill">
                  Selected
                </span>
              )}

            </div>


            {!reference ? (

              <>

                <div className="reference-file-selector">

                  <div className="selector-header">

                    <div>

                      <strong>
                        Select reference images
                      </strong>

                      <span>
                        Choose one or more images. Each Google Drive image has a number you can use in the prompt (for example: “Use 1 and 3”).
                      </span>

                    </div>

                    {isTaggingImages && (
                      <span className="tagging-status">
                        AI tagging...
                      </span>
                    )}

                  </div>

                  {isLoadingInputs ? (

                    <div className="reference-loading">
                      Loading images...
                    </div>

                  ) : inputFiles.length === 0 ? (

                    <div className="reference-empty-list">
                      No Google Drive references
                      found. You can still add a
                      manual upload below.
                    </div>

                  ) : (

                    <>

                      <button
                        type="button"
                        className={`reference-scroll-button reference-scroll-up ${
                          isReferenceListAtTop
                            ? "scroll-indicator-top"
                            : ""
                        }`}
                        onClick={() =>
                          scrollReferenceList("up")
                        }
                        aria-label="Scroll reference images up"
                      >
                        ▲
                      </button>

                      <div
                        ref={referenceListRef}
                        className={`reference-file-list ${
                          isReferenceListAtTop ||
                          isReferenceListAtBottom
                            ? "scrollbar-red"
                            : "scrollbar-green"
                        }`}
                        onScroll={handleReferenceListScroll}
                      >

                        {inputFiles.map((file) => (

                          <label
                            key={file.id}
                            className={`reference-file-item ${
                              selectedInputIds.includes(file.id)
                                ? "selected"
                                : ""
                            }`}
                          >

                            <input
                              type="checkbox"
                              checked={selectedInputIds.includes(file.id)}
                              onChange={() =>
                                handleReferenceSelection(file)
                              }
                              aria-label={`Select ${file.name} as reference`}
                            />

                            {getDriveReferenceNumber(file) !== null && (
                              <span
                                className={`reference-image-number reference-preview-number-badge ${
                                  selectedInputIds.includes(file.id)
                                    ? "selected"
                                    : ""
                                }`}
                                title={`Reference ${getDriveReferenceNumber(file)}`}
                                style={{
                                  minWidth: "32px",
                                  height: "32px",
                                  padding: "0 8px",
                                  borderRadius: "10px",
                                  display: "inline-flex",
                                  alignItems: "center",
                                  justifyContent: "center",
                                  flexShrink: 0,
                                  fontWeight: 800,
                                  fontSize: "14px",
                                  border: selectedInputIds.includes(file.id)
                                    ? "2px solid currentColor"
                                    : "1px solid rgba(255,255,255,0.25)",
                                  background: selectedInputIds.includes(file.id)
                                    ? "rgba(99, 91, 255, 0.14)"
                                    : "#ffffff",
                                  color: selectedInputIds.includes(file.id)
                                    ? "#4f46e5"
                                    : "#5f6b7a",
                                  boxSizing: "border-box",
                                }}
                              >
                                {getDriveReferenceNumber(file)}
                              </span>
                            )}

                            <div className="reference-list-thumbnail">

                              <img
                                src={
                                  previewUrls[file.id] ||
                                  resolveApiUrl(file.url)
                                }
                                alt={file.name}
                                loading="eager"
                                decoding="async"
                                onError={(event) => {
                                  console.error(
                                    "Reference thumbnail failed:",
                                    file.name,
                                    file.url,
                                  );
                                  event.currentTarget.style.display =
                                    "none";
                                }}
                              />

                            </div>

                            <div className="reference-list-file-info">

                              <div className="reference-list-file-name">
                                {file.name}
                              </div>

                              <div className="reference-list-file-tag">
                                {file.tag ||
                                  (isTaggingImages
                                    ? "Analyzing image..."
                                    : file.tagError
                                      ? "Tagging failed"
                                      : "Tag unavailable")}
                              </div>

                              {file.tagError && !file.tag && (
                                <div className="reference-list-file-error">
                                  {file.tagError}
                                </div>
                              )}

                              <div className="reference-list-file-meta">
                                {file.sizeFormatted}
                              </div>

                            </div>

                          </label>

                        ))}

                      </div>

                      {selectedInputIds.length > 0 && (
                        <div className="selected-reference-previews" style={{ marginTop: "14px", padding: "12px", borderRadius: "14px", border: "1px solid rgba(255,255,255,0.10)", background: "rgba(255,255,255,0.025)" }}>
                          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "10px" }}>
                            <strong>Selected reference previews</strong>
                            <span style={{ fontSize: "12px", opacity: 0.7 }}>{selectedInputIds.length} selected</span>
                          </div>
                          <div
                            className="reference-selected-preview-grid"
                            style={{
                              display: "grid",
                              gridTemplateColumns: `repeat(${getReferencePreviewColumns(selectedInputIds.length)}, minmax(0, 1fr))`,
                              gap: "10px",
                              alignItems: "start",
                            }}
                          >
                            {selectedInputIds.map((id) => {
                              const selectedFile = inputFiles.find((item) => item.id === id);
                              if (!selectedFile) return null;
                              const number = getDriveReferenceNumber(selectedFile);
                              return (
                                <div
                                  key={selectedFile.id}
                                  className="selected-reference-preview-card"
                                  style={{
                                    position: "relative",
                                    minWidth: 0,
                                    overflow: "hidden",
                                    borderRadius: "12px",
                                    border: "1px solid rgba(99,91,255,0.28)",
                                    background: "#ffffff",
                                  }}
                                >
                                  <div style={{ position: "relative", width: "100%", height: `${getReferencePreviewHeight(selectedInputIds.length)}px`, overflow: "hidden", borderRadius: "10px", background: "#f5f7fb", display: "flex", alignItems: "center", justifyContent: "center" }}>
                                    <img src={previewUrls[selectedFile.id] || resolveApiUrl(selectedFile.url)} alt={`Reference ${number ?? ""}: ${selectedFile.name}`} loading="eager" decoding="async" style={{ width: "100%", height: "100%", objectFit: "contain", display: "block" }} />
                                    {number !== null && <span style={{ position: "absolute", top: "6px", left: "6px", minWidth: "27px", height: "27px", padding: "0 7px", borderRadius: "8px", display: "inline-flex", alignItems: "center", justifyContent: "center", fontWeight: 800, fontSize: "12px", background: "rgba(255,255,255,0.96)", color: "#4f46e5", border: "2px solid #635bff", boxShadow: "0 3px 10px rgba(79,70,229,0.18)" }}>{number}</span>}
                                  </div>
                                  <div title={selectedFile.name} style={{ marginTop: "5px", fontSize: "11px", lineHeight: 1.2, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                                    {number !== null ? `${number} · ` : ""}{selectedFile.name}
                                  </div>
                                </div>
                              );
                            })}
                          </div>
                        </div>
                      )}

                      <button
                        type="button"
                        className={`reference-scroll-button reference-scroll-down ${
                          isReferenceListAtBottom
                            ? "scroll-indicator-bottom"
                            : ""
                        }`}
                        onClick={() =>
                          scrollReferenceList("down")
                        }
                        aria-label="Scroll reference images down"
                      >
                        ▼
                      </button>

                    </>

                  )}

                </div>

                <div className="reference-divider">
                  <span>or</span>
                </div>

                <div
                  className="reference-drop-zone"
                  onDragOver={(event) => event.preventDefault()}
                  onDrop={handleDrop}
                >

                  <div className="reference-drop-title">
                    Add another reference
                  </div>

                  <div className="reference-drop-text">
                    Upload media or provide a
                    YouTube or image link.
                  </div>

                  <button
                    type="button"
                    className="primary-button"
                    onClick={() => setShowReferenceModal(true)}
                  >
                    Add Reference
                  </button>

                </div>

              </>

            ) : (

              <div className="reference-selected-layout">

                <div className="reference-selected-list-panel">

                  <div className="reference-file-selector reference-file-selector-compact">

                    <div className="selector-header">

                      <div>

                        <strong>
                          Numbered reference library
                        </strong>

                        <span>
                          Select multiple reference images. Use their highlighted numbers in the prompt (for example: “Use 1 for layout and 3 for the logo”).
                        </span>

                      </div>

                      <span className="reference-count-pill">
                        {inputFiles.filter((file) => file.source === "google-drive").length} Drive images · {selectedInputIds.length} selected
                      </span>

                    </div>

                    {isTaggingImages && (
                      <span className="tagging-status compact-tagging-status">
                        AI tagging...
                      </span>
                    )}

                    {isLoadingInputs ? (

                      <div className="reference-loading">
                        Loading images...
                      </div>

                    ) : inputFiles.length === 0 ? (

                      <div className="reference-empty-list">
                        No Google Drive references found.
                      </div>

                    ) : (

                      <>

                        <button
                          type="button"
                          className={`reference-scroll-button reference-scroll-up ${
                            isReferenceListAtTop
                              ? "scroll-indicator-top"
                              : ""
                          }`}
                          onClick={() =>
                            scrollReferenceList("up")
                          }
                          aria-label="Scroll reference images up"
                        >
                          ▲
                        </button>

                        <div
                          ref={referenceListRef}
                          className={`reference-file-list reference-file-list-compact ${
                            isReferenceListAtTop ||
                            isReferenceListAtBottom
                              ? "scrollbar-red"
                              : "scrollbar-green"
                          }`}
                          onScroll={handleReferenceListScroll}
                        >

                          {inputFiles.map((file) => (

                            <label
                              key={file.id}
                              className={`reference-file-item ${
                                selectedInputIds.includes(file.id)
                                  ? "selected"
                                  : ""
                              }`}
                            >

                              <input
                                type="checkbox"
                                checked={selectedInputIds.includes(file.id)}
                                onChange={() =>
                                  handleReferenceSelection(file)
                                }
                                aria-label={`Select ${file.name} as reference`}
                              />

                              {getDriveReferenceNumber(file) !== null && (
                              <span
                                className={`reference-image-number ${
                                  selectedInputIds.includes(file.id)
                                    ? "selected"
                                    : ""
                                }`}
                                title={`Reference ${getDriveReferenceNumber(file)}`}
                                style={{
                                  minWidth: "32px",
                                  height: "32px",
                                  padding: "0 8px",
                                  borderRadius: "10px",
                                  display: "inline-flex",
                                  alignItems: "center",
                                  justifyContent: "center",
                                  flexShrink: 0,
                                  fontWeight: 800,
                                  fontSize: "14px",
                                  border: selectedInputIds.includes(file.id)
                                    ? "2px solid currentColor"
                                    : "1px solid rgba(255,255,255,0.25)",
                                  background: selectedInputIds.includes(file.id)
                                    ? "rgba(99, 91, 255, 0.14)"
                                    : "#ffffff",
                                  color: selectedInputIds.includes(file.id)
                                    ? "#4f46e5"
                                    : "#5f6b7a",
                                  boxSizing: "border-box",
                                }}
                              >
                                {getDriveReferenceNumber(file)}
                              </span>
                            )}

                            <div className="reference-list-thumbnail">

                                <img
                                  src={
                                    previewUrls[file.id] ||
                                    resolveApiUrl(file.url)
                                  }
                                  alt={file.name}
                                  loading="eager"
                                  decoding="async"
                                  onError={(event) => {
                                    console.error(
                                      "Reference thumbnail failed:",
                                      file.name,
                                      file.url,
                                    );
                                    event.currentTarget.style.display =
                                      "none";
                                  }}
                                />

                              </div>

                              <div className="reference-list-file-info">

                                <div className="reference-list-file-name">
                                  {file.name}
                                </div>

                                <div className="reference-list-file-tag">
                                  {file.tag ||
                                    (isTaggingImages
                                      ? "Analyzing image..."
                                      : file.tagError
                                        ? "Tagging failed"
                                        : "Tag unavailable")}
                                </div>

                                <div className="reference-list-file-meta">
                                  {file.sizeFormatted}
                                </div>

                              </div>

                            </label>

                          ))}

                        </div>

                        <button
                          type="button"
                          className={`reference-scroll-button reference-scroll-down ${
                            isReferenceListAtBottom
                              ? "scroll-indicator-bottom"
                              : ""
                          }`}
                          onClick={() =>
                            scrollReferenceList("down")
                          }
                          aria-label="Scroll reference images down"
                        >
                          ▼
                        </button>

                      </>

                    )}

                  </div>

                  <button
                    type="button"
                    className="change-reference-button compact-change-reference"
                    onClick={() => setShowReferenceModal(true)}
                  >
                    + Add / Upload Reference
                  </button>

                </div>

                <div className="reference-selected-preview-panel">

                  <div
                    className="selected-reference-preview"
                    style={{
                      width: "100%",
                    }}
                  >
                    <div
                      className="reference-preview-panel-heading"
                      style={{
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "space-between",
                        gap: "10px",
                        marginBottom: "10px",
                      }}
                    >
                      <strong>
                        Selected reference previews
                      </strong>
                      <span style={{ fontSize: "11px", opacity: 0.7 }}>
                        {selectedInputIds.length} selected
                      </span>
                    </div>

                    {selectedInputIds.length > 0 ? (
                      <div
                        className="reference-selected-preview-grid"
                        style={{
                          width: "100%",
                          display: "grid",
                          gridTemplateColumns: `repeat(${getReferencePreviewColumns(selectedInputIds.length)}, minmax(0, 1fr))`,
                          gap: "10px",
                          alignItems: "start",
                        }}
                      >
                        {selectedInputIds.map((id) => {
                          const selectedFile = inputFiles.find(
                            (item) => item.id === id,
                          );
                          if (!selectedFile) return null;

                          const number = getDriveReferenceNumber(selectedFile);
                          const previewUrl =
                            previewUrls[selectedFile.id] ||
                            resolveApiUrl(selectedFile.url);

                          return (
                            <div
                              key={selectedFile.id}
                              className="selected-reference-preview-card"
                              style={{
                                position: "relative",
                                minWidth: 0,
                                borderRadius: "12px",
                                border: "1px solid rgba(120, 100, 255, 0.35)",
                                background: "rgba(255,255,255,0.035)",
                                padding: "8px",
                                overflow: "hidden",
                              }}
                            >
                              <div
                                className="reference-preview-number-badge"
                                style={{
                                  position: "absolute",
                                  top: "7px",
                                  left: "7px",
                                  zIndex: 3,
                                  minWidth: "30px",
                                  height: "30px",
                                  padding: "0 8px",
                                  borderRadius: "9px",
                                  display: "inline-flex",
                                  alignItems: "center",
                                  justifyContent: "center",
                                  background: "#ffffff",
                                  color: "#4f46e5",
                                  fontWeight: 800,
                                  fontSize: "13px",
                                  boxShadow: "0 3px 12px rgba(0,0,0,0.25)",
                                }}
                              >
                                {number ?? "•"}
                              </div>

                              <div
                                style={{
                                  width: "100%",
                                  height: `${getReferencePreviewHeight(selectedInputIds.length)}px`,
                                  borderRadius: "9px",
                                  overflow: "hidden",
                                  background: "rgba(255,255,255,0.04)",
                                  display: "flex",
                                  alignItems: "center",
                                  justifyContent: "center",
                                }}
                              >
                                <img
                                  src={previewUrl}
                                  alt={`Reference ${number ?? ""}: ${selectedFile.name}`}
                                  loading="eager"
                                  decoding="async"
                                  style={{
                                    width: "100%",
                                    height: "100%",
                                    objectFit: "contain",
                                    display: "block",
                                  }}
                                  onError={(event) => {
                                    console.error(
                                      "Selected reference preview failed:",
                                      selectedFile.name,
                                      selectedFile.url,
                                    );
                                    event.currentTarget.style.display = "none";
                                  }}
                                />
                              </div>

                              <div
                                style={{
                                  marginTop: "7px",
                                  fontSize: "11px",
                                  fontWeight: 700,
                                  overflow: "hidden",
                                  textOverflow: "ellipsis",
                                  whiteSpace: "nowrap",
                                }}
                                title={selectedFile.name}
                              >
                                {number != null ? `${number}. ` : ""}
                                {selectedFile.name}
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    ) : (
                      <div className="reference-empty-list">
                        Select one or more Google Drive images to preview them here.
                      </div>
                    )}
                  </div>

                  <div className="selected-reference-info">
                    <div>
                      <strong>
                        {selectedInputIds.length > 0
                          ? `${selectedInputIds.length} reference${selectedInputIds.length === 1 ? "" : "s"} selected`
                          : "No references selected"}
                      </strong>
                      <span>
                        Use the highlighted image numbers directly in your prompt.
                      </span>
                    </div>

                    {selectedInputIds.length > 0 && (
                      <button
                        type="button"
                        className="secondary-button"
                        onClick={() => {
                          setSelectedInputIds([]);
                          setReference(null);
                        }}
                      >
                        Clear Selection
                      </button>
                    )}
                  </div>

                </div>

              </div>

            )}

            {error && (
              <div className="error-message">
                {error}
              </div>
            )}

          </article>


          {/* ====================================================
              TEMPLATE
              ==================================================== */}

          <article className="workspace-card">


            <div className="card-header">

              <div>

                <div className="section-kicker">
                  TEMPLATE
                </div>

                <h2>
                  Template Builder
                </h2>

              </div>


              {templateResult && (
                <span className="status-pill success">
                  Generated
                </span>
              )}

            </div>


            <div className="template-workspace-grid">


              <div className="template-reference-area">


                <div className="template-panel-heading">

                  <div className="template-panel-index">
                    01
                  </div>


                  <div>

                    <div className="template-preview-label">
                      REFERENCE
                    </div>

                    <h3>
                      Source Visual
                    </h3>

                    <p>
                      All selected references are shown here.
                      Their highlighted numbers can be used in the prompt.
                    </p>

                  </div>

                </div>


                <div className="template-reference-frame">


                  {selectedInputIds.length > 0 ? (

                    <div
                      className="selected-reference-preview-grid"
                      style={{
                        width: "100%",
                        height: "100%",
                        minHeight: "240px",
                        display: "grid",
                        gridTemplateColumns: `repeat(${getReferencePreviewColumns(selectedInputIds.length)}, minmax(0, 1fr))`,
                        gap: "10px",
                        padding: "10px",
                        overflowY: "auto",
                        alignContent: "start",
                      }}
                    >
                      {selectedInputIds.map((id) => {
                        const selectedFile = inputFiles.find(
                          (item) => item.id === id,
                        );
                        if (!selectedFile) return null;

                        const number =
                          getDriveReferenceNumber(selectedFile);
                        const previewUrl =
                          previewUrls[selectedFile.id] ||
                          resolveApiUrl(selectedFile.url);

                        const selectedReference: ReferenceData = {
                          type: getReferenceType(
                            selectedFile.name.includes(".")
                              ? `.${selectedFile.name.split(".").pop()?.toLowerCase()}`
                              : "",
                            selectedFile.mimeType,
                          ),
                          name: selectedFile.name,
                          url: previewUrl,
                          size: selectedFile.size,
                          mimeType: selectedFile.mimeType,
                          source:
                            selectedFile.source === "google-drive"
                              ? "google-drive"
                              : selectedFile.source === "manual-upload"
                                ? "upload"
                                : "input-folder",
                          sourceId:
                            selectedFile.source === "google-drive"
                              ? selectedFile.id.replace(/^drive:/, "")
                              : selectedFile.name,
                          tag: selectedFile.tag,
                        };

                        return (
                          <div
                            key={selectedFile.id}
                            className="selected-reference-preview-card"
                            style={{
                              position: "relative",
                              minWidth: 0,
                              borderRadius: "12px",
                              border: "2px solid currentColor",
                              overflow: "hidden",
                              background: "rgba(255,255,255,0.035)",
                              minHeight: "150px",
                            }}
                          >
                            <div
                              className="reference-preview-number-badge"
                              style={{
                                position: "absolute",
                                top: "7px",
                                left: "7px",
                                zIndex: 3,
                                minWidth: "28px",
                                height: "28px",
                                padding: "0 7px",
                                borderRadius: "9px",
                                display: "inline-flex",
                                alignItems: "center",
                                justifyContent: "center",
                                fontWeight: 800,
                                fontSize: "13px",
                                background: "rgba(255,255,255,0.96)",
                                color: "#4f46e5",
                                border: "2px solid #635bff",
                                boxShadow: "0 2px 8px rgba(0,0,0,0.25)",
                              }}
                            >
                              {number ?? selectedInputIds.indexOf(id) + 1}
                            </div>

                            <div
                              style={{
                                width: "100%",
                                height: `${getReferencePreviewHeight(selectedInputIds.length)}px`,
                                display: "flex",
                                alignItems: "center",
                                justifyContent: "center",
                                overflow: "hidden",
                              }}
                            >
                              {renderReferencePreview(
                                selectedReference,
                              )}
                            </div>

                            <div
                              title={selectedFile.name}
                              style={{
                                padding: "7px 9px",
                                fontSize: "11px",
                                lineHeight: 1.2,
                                overflow: "hidden",
                                textOverflow: "ellipsis",
                                whiteSpace: "nowrap",
                                borderTop: "1px solid rgba(255,255,255,0.08)",
                              }}
                            >
                              {number ?? selectedInputIds.indexOf(id) + 1}. {selectedFile.name}
                            </div>
                          </div>
                        );
                      })}
                    </div>

                  ) : reference ? (

                    <div className="fixed-reference-preview">

                      {
                        renderReferencePreview(
                          reference,
                        )
                      }

                    </div>

                  ) : (

                    <div className="template-no-reference">

                      <div className="template-empty-icon">
                        ✦
                      </div>

                      <strong>
                        Select a reference image
                      </strong>

                      <span>
                        The selected reference will
                        appear here.
                      </span>

                    </div>

                  )}

                </div>


                {reference && (

                  <div className="template-reference-meta">


                    <div className="template-reference-file">

                      <span className="template-file-dot" />


                      <div>

                        <strong>
                          {
                            reference.name
                          }
                        </strong>

                        <span>

                          {
                            reference.source ===
                            "external-url"
                              ? "External reference"
                              : reference.source ===
                                  "upload"
                                ? "Uploaded reference"
                                : reference.source ===
                                    "google-drive"
                                  ? "From Google Drive"
                                  : "Manual upload"
                          }

                        </span>

                      </div>

                    </div>


                    <span className="template-reference-type">

                      {
                        reference.type ===
                        "youtube"
                          ? "YouTube"
                          : reference.type.toUpperCase()
                      }

                    </span>

                  </div>

                )}

              </div>


              <div className="generated-template-panel">


                <div className="generated-template-panel-header">


                  <div className="template-panel-heading">

                    <div className="template-panel-index active">
                      02
                    </div>


                    <div>

                      <div className="template-preview-label">
                        GENERATED TEMPLATE
                      </div>


                      <h3>

                        {
                          isGeneratingTemplate
                            ? "Template generation disabled"
                            : "Template generation disabled"
                        }

                      </h3>


                      <p>

                        Template generation is temporarily disabled. Image generation uses the selected reference directly.


                      </p>

                    </div>

                  </div>


                  {isGeneratingTemplate && (
                    <span className="template-generating-pill">
                      Generating
                    </span>
                  )}


                  {templateResult &&
                    !isGeneratingTemplate && (
                      <span className="status-pill success">
                        Ready
                      </span>
                    )}

                </div>


                {isGeneratingTemplate ? (

                  <div className="template-generation-state">

                    <div className="template-loader" />

                    <strong>
                      Analyzing reference structure
                    </strong>

                    <span>
                      Reading canvas, layout,
                      regions and visual style
                      from the reference.
                    </span>

                  </div>

                ) : templateResult ? (

                  <div className="generated-template-content">


                    <div className="template-structure-section">


                      <div className="template-structure-heading">

                        <div>

                          <span className="template-section-number">
                            01
                          </span>

                          <strong>
                            Template Overview
                          </strong>

                        </div>

                        <span>
                          Canvas &amp; layout
                        </span>

                      </div>


                      <div className="template-overview-grid">


                        <div className="template-overview-card">

                          <span>
                            Canvas
                          </span>

                          <strong>

                            {
                              templateResult.template.canvas.width
                            }

                            {" × "}

                            {
                              templateResult.template.canvas.height
                            }

                          </strong>

                        </div>


                        <div className="template-overview-card">

                          <span>
                            Orientation
                          </span>

                          <strong>
                            {
                              templateResult.template.canvas.orientation
                            }
                          </strong>

                        </div>


                        <div className="template-overview-card">

                          <span>
                            Layout
                          </span>

                          <strong>
                            {
                              templateResult.template.layout.type
                            }
                          </strong>

                        </div>


                        <div className="template-overview-card">

                          <span>
                            Alignment
                          </span>

                          <strong>
                            {
                              templateResult.template.layout.alignment
                            }
                          </strong>

                        </div>

                      </div>

                    </div>


                    <div className="template-structure-section">


                      <div className="template-structure-heading">

                        <div>

                          <span className="template-section-number">
                            02
                          </span>

                          <strong>
                            Layout Structure
                          </strong>

                        </div>

                        <span>
                          Content regions
                        </span>

                      </div>


                      <div className="template-region-flow">

                        {
                          (Array.isArray(templateResult.template.regions) ? templateResult.template.regions : []).map(
                            (
                              region,
                              index,
                            ) => (

                              <div
                                key={`${region.order}-${region.name}`}
                                className="template-region-node"
                              >

                                <span className="template-region-order">

                                  {
                                    String(
                                      index + 1,
                                    ).padStart(
                                      2,
                                      "0",
                                    )
                                  }

                                </span>


                                <span className="template-region-name">

                                  {
                                    region.name.replaceAll(
                                      "_",
                                      " ",
                                    )
                                  }

                                </span>

                              </div>

                            ),
                          )
                        }

                      </div>

                    </div>


                    <div className="template-structure-section">


                      <div className="template-structure-heading">

                        <div>

                          <span className="template-section-number">
                            03
                          </span>

                          <strong>
                            Visual System
                          </strong>

                        </div>

                        <span>
                          Reference colors
                        </span>

                      </div>


                      <div className="template-color-list structured">

                        {
                          (Array.isArray(templateResult.template.style?.dominant_colors) ? templateResult.template.style.dominant_colors : []).map(
                            (
                              color,
                            ) => (

                              <div
                                key={
                                  color.hex
                                }
                                className="template-color-card"
                              >

                                <span
                                  className="template-color-swatch large"
                                  style={{
                                    backgroundColor:
                                      color.hex,
                                  }}
                                />


                                <div>

                                  <strong>
                                    {
                                      color.hex
                                    }
                                  </strong>

                                  <span>
                                    Reference color
                                  </span>

                                </div>

                              </div>

                            ),
                          )
                        }

                      </div>

                    </div>


                    <div className="template-output-note">


                      <div className="template-output-note-icon">
                        ✓
                      </div>


                      <div>

                        <strong>
                          Ready for Canva
                        </strong>

                        <span>
                          Template structure is generated
                          independently. The human-written
                          or AI-generated prompt can be
                          combined with this template in the
                          next Image Builder step.
                        </span>

                      </div>

                    </div>

                  </div>

                ) : (

                  <div className="template-generation-state empty">

                    <div className="template-empty-icon">
                      ✦
                    </div>

                    <strong>
                      Template generation is temporarily disabled
                    </strong>

                    <span>
                      The selected reference will be used directly for image generation.
                    </span>

                  </div>

                )}

              </div>

            </div>


            {false && templateResult && (
              <div className="generated-output-meta" style={{ marginTop: "16px" }}>
                <span>API Provider</span>
                <strong>{templateApiProvider || "Selected API"}</strong>
                <span>Model</span>
                <strong>{templateApiModel || "Provider model"}</strong>
              </div>
            )}


            {false && templateResult && (
              <div className="template-editable-section">

                <div className="template-structure-heading">
                  <div>
                    <span className="template-section-number">
                      04
                    </span>
                    <strong>
                      Editable Text Groups
                    </strong>
                  </div>

                  <span>
                    Existing text regions that can be replaced
                  </span>
                </div>

                {templateResult.template.text_groups &&
                templateResult.template.text_groups.length > 0 ? (
                  <div className="editable-text-list">
                    {(Array.isArray(templateResult.template.text_groups) ? templateResult.template.text_groups : []).map((group) => (
                      <div
                        key={group.id}
                        className="editable-text-item"
                      >
                        <div
                          className="editable-text-swatch"
                          style={{
                            backgroundColor:
                              group.lines?.[0]?.color ||
                              "#FFFFFF",
                          }}
                        />
                        <div className="editable-text-copy">
                          <strong>{group.text}</strong>
                          <span>
                            {group.role} · {group.line_count} line{group.line_count === 1 ? "" : "s"}
                          </span>
                        </div>
                      </div>
                    ))}
                  </div>
                ) : templateResult.template.text_elements &&
                  templateResult.template.text_elements.length > 0 ? (
                  <div className="editable-text-list">
                    {(Array.isArray(templateResult.template.text_elements) ? templateResult.template.text_elements : []).map((element) => (
                      <div
                        key={element.id}
                        className="editable-text-item"
                      >
                        <div
                          className="editable-text-swatch"
                          style={{ backgroundColor: element.color }}
                        />
                        <div className="editable-text-copy">
                          <strong>{element.text}</strong>
                          <span>
                            {element.id} · {element.width} × {element.height}px
                          </span>
                        </div>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="editable-text-empty">
                    No editable text regions were detected. Make sure Gemini is configured and regenerate the template.
                  </div>
                )}

              </div>
            )}


            <div className="template-prompt-area">


              <div className="prompt-mode-header">


                <label className="input-label">
                  Content Prompt
                </label>


                <div className="prompt-mode-options">


                  <label
                    className={`prompt-mode-option ${
                      promptMode ===
                      "manual"
                        ? "active"
                        : ""
                    }`}
                  >

                    <input
                      type="radio"
                      name="prompt-mode"
                      value="manual"
                      checked={
                        promptMode ===
                        "manual"
                      }
                      onChange={() => {

                        setPromptMode(
                          "manual",
                        );

                        setError("");

                      }}
                    />

                    <span>
                      Enter Prompt
                    </span>

                  </label>


                  <label
                    className={`prompt-mode-option ${
                      promptMode ===
                      "ai"
                        ? "active"
                        : ""
                    }`}
                  >

                    <input
                      type="radio"
                      name="prompt-mode"
                      value="ai"
                      checked={
                        promptMode ===
                        "ai"
                      }
                      disabled
                      onChange={() => {
                        setPromptMode("manual");
                        setError("AI prompt generation is temporarily disabled.");
                      }}
                    />

                    <span>
                      Generate with AI (Disabled)
                    </span>

                  </label>

                </div>

              </div>


              <div className="ai-prompt-panel">

                <div className="ai-prompt-panel-text">

                  <strong>
                    AI Prompt Generation Disabled
                  </strong>

                  <span>
                    AI will not generate the prompt for now. Enter your content-change prompt manually below.
                  </span>

                </div>

                <button
                  type="button"
                  className="ai-prompt-button"
                  disabled
                >
                  AI Prompt Disabled
                </button>

              </div>


              {promptApiProvider && (
                <div className="generated-output-meta" style={{ marginBottom: "12px" }}>
                  <span>API Provider</span>
                  <strong>{promptApiProvider}</strong>
                  <span>Model</span>
                  <strong>{promptApiModel || "Provider model"}</strong>
                </div>
              )}


              <textarea
                id="template-prompt"
                className="template-textarea"
                value={
                  templatePrompt
                }
                onChange={(
                  event,
                ) =>
                  setTemplatePrompt(
                    event.target
                      .value,
                  )
                }
                placeholder="Describe which poster text or content should change while keeping the reference design..."
                rows={6}
              />


              <div className="template-helper">
                Use the highlighted Google Drive reference numbers directly in your prompt. Example: “Use 1 for the layout, 3 for the logo, and 5 for the color style.”
              </div>

            </div>

          </article>

        </section>


        {/* ======================================================
            IMAGE BUILDER
            ====================================================== */}

        <section className="full-width-card">


          <div className="card-header">

            <div>

              <div className="section-kicker">
                IMAGE BUILDER
              </div>

              <h2>
                Generate Image
              </h2>

            </div>


            <span className="status-pill success">
              Reference Design + Content Changes
            </span>

          </div>


          <div className="image-builder-panel">

            <div className="image-builder-summary">

              <div className="image-builder-input-card">
                <span className="image-builder-number">
                  01
                </span>

                <div>
                  <strong>
                    Reference Image
                  </strong>

                  <span>
                    {selectedInputIds.length > 0
                      ? `${selectedInputIds.length} reference image${selectedInputIds.length === 1 ? "" : "s"} selected`
                      : "Not selected"}
                  </span>
                </div>

              </div>


              <div className="image-builder-plus">
                +
              </div>


              <div className="image-builder-input-card">
                <span className="image-builder-number">
                  02
                </span>

                <div>
                  <strong>
                    Template Generation
                  </strong>

                  <span>
                    Temporarily disabled
                  </span>
                </div>

              </div>


              <div className="image-builder-plus">
                +
              </div>


              <div className="image-builder-input-card">
                <span className="image-builder-number">
                  03
                </span>

                <div>
                  <strong>
                    Content Prompt
                  </strong>

                  <span>
                    {templatePrompt.trim()
                      ? "Prompt ready"
                      : "Enter a prompt below"}
                  </span>
                </div>

              </div>

            </div>


            <button
              type="button"
              className="generate-button"
              disabled={
                selectedInputIds.length === 0 ||
                !reference ||
                !templatePrompt.trim() ||
                isGeneratingImage
              }
              onClick={
                handleGenerateImage
              }
            >
              {isGeneratingImage
                ? "Generating Image..."
                : "Generate Image"}
            </button>


            {isGeneratingImage && (

              <div className="generation-progress">

                <div className="generation-spinner" />

                <div>
                  <strong>
                    Generating your image
                  </strong>

                  <span>
                    The selected image-capable API is applying your manual
                    content prompt to the original reference image.
                  </span>
                </div>

              </div>

            )}

          </div>

        </section>


        {/* ======================================================
            OUTPUT
            ====================================================== */}

        <section className="full-width-card">


          <div className="card-header">

            <div>

              <div className="section-kicker">
                OUTPUT
              </div>

              <h2>
                Generated Output
              </h2>

            </div>


            <span className="output-path">
              Google Drive / outputs
            </span>

          </div>


          {generatedImageUrl ? (

            <div className="generated-output-card">

              <div className="generated-output-preview">

                <img
                  src={
                    generatedImageUrl
                  }
                  alt="Generated output"
                />

              </div>


              <div className="generated-output-details">

                <span className="generated-output-status">
                  Generation complete
                </span>

                <h3>
                  Generated Image
                </h3>

                {generatedImageDescription && (
                  <div
                    className="generated-output-description"
                    style={{
                      marginTop: "12px",
                      padding: "12px 14px",
                      borderRadius: "12px",
                      border: "1px solid rgba(255,255,255,0.10)",
                      background: "rgba(255,255,255,0.035)",
                    }}
                  >
                    <span
                      style={{
                        display: "block",
                        fontSize: "12px",
                        fontWeight: 700,
                        opacity: 0.7,
                        marginBottom: "5px",
                      }}
                    >
                      Description
                    </span>
                    <p
                      style={{
                        margin: 0,
                        lineHeight: 1.5,
                      }}
                    >
                      {generatedImageDescription}
                    </p>
                  </div>
                )}

                <p>
                  The selected reference images are synthesized into one
                  coherent output. Their requested visual features are
                  combined rather than rendered as a collage.
                </p>


                {Object.keys(generatedTextChanges).length > 0 && (
                  <div className="generated-change-list">
                    <span className="generated-change-heading">
                      Applied content changes
                    </span>
                    {Object.entries(generatedTextChanges).map(([id, value]) => (
                      <div key={id} className="generated-change-item">
                        <span>{id}</span>
                        <strong>{value}</strong>
                      </div>
                    ))}
                  </div>
                )}

                {generatedImageFilename && (

                  <div className="generated-output-meta">

                    <span>
                      File
                    </span>

                    <strong>
                      {generatedImageFilename}
                    </strong>

                  </div>

                )}


                {generatedImageProvider && (
                  <div className="generated-output-meta">
                    <span>API Provider</span>
                    <strong>{generatedImageProvider}</strong>
                  </div>
                )}


                {generatedImageModel && (

                  <div className="generated-output-meta">

                    <span>
                      Model
                    </span>

                    <strong>
                      {generatedImageModel}
                    </strong>

                  </div>

                )}

                
{generatedImageUrl && (
  <div
    style={{
      display: "flex",
      gap: "10px",
      flexWrap: "wrap",
      marginTop: "16px",
    }}
  >
    <button
      type="button"
      className="secondary-button"
      onClick={handleOpenGeneratedImageInCanva}
      disabled={isCreatingCanvaDesign}
      style={{
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        gap: "8px",
      }}
    >
      {isCreatingCanvaDesign
        ? "Opening Canva..."
        : "🎨 EDIT IN CANVA TEST"}
    </button>
  </div>
)}
<button
  type="button"
  onClick={handleOpenGeneratedImageInCanva}
  disabled={!generatedImageFilename || isCreatingCanvaDesign}
  style={{
    display: "inline-flex",
    alignItems: "center",
    justifyContent: "center",
    gap: "8px",
    minHeight: "42px",
    padding: "10px 18px",
    borderRadius: "10px",
    border: "1px solid rgba(123, 92, 255, 0.45)",
    background: isCreatingCanvaDesign
      ? "rgba(123, 92, 255, 0.25)"
      : "linear-gradient(135deg, #7b4dff, #4f8cff)",
    color: "#fff",
    fontWeight: 800,
    cursor: "pointer",
  }}
>
  🎨 {isCreatingCanvaDesign ? "Opening Canva..." : "Edit in Canva"}
</button>

{canvaMessage && (
  <div
    style={{
      marginTop: "10px",
      padding: "10px 12px",
      borderRadius: "10px",
      border: "1px solid rgba(120, 100, 255, 0.25)",
      background: "rgba(120, 100, 255, 0.07)",
    }}
  >
    <p style={{ margin: 0 }}>{canvaMessage}</p>
    {canvaEditUrl && (
      <a
        href={canvaEditUrl}
        target="_blank"
        rel="noreferrer"
        style={{
          display: "inline-block",
          marginTop: "7px",
          fontWeight: 700,
        }}
      >
        Reopen editable Canva design
      </a>
    )}
    {canvaDesignId && (
      <small
        style={{
          display: "block",
          marginTop: "5px",
          opacity: 0.65,
        }}
      >
        Canva design: {canvaDesignId}
      </small>
    )}
  </div>
)}

{/* ======================================================
    POST-IMAGE DESCRIPTION — kept separate from
    template/prompt generation.
    ====================================================== */}
<div
  style={{
    marginTop: "18px",
    padding: "14px",
    borderRadius: "12px",
    border: "1px solid rgba(120, 100, 255, 0.22)",
    background: "rgba(120, 100, 255, 0.045)",
  }}
>
  <div style={{ marginBottom: "10px" }}>
    <span
      style={{
        fontSize: "11px",
        fontWeight: 800,
        letterSpacing: "0.08em",
        opacity: 0.7,
      }}
    >
      NEXT STEP
    </span>

    <h3 style={{ margin: "4px 0 5px" }}>
      Social Media Description
    </h3>

    <p
      style={{
        margin: 0,
        fontSize: "12px",
        opacity: 0.72,
      }}
    >
      Generate platform-specific descriptions from the final generated image.
    </p>
  </div>

  <div
    style={{
      display: "flex",
      gap: "8px",
      flexWrap: "wrap",
    }}
  >
    <button
      type="button"
      className="primary-button"
      onClick={handleGenerateSocialDescriptions}
      disabled={
        !generatedImageFilename ||
        isGeneratingSocialDescriptions
      }
    >
      {isGeneratingSocialDescriptions
        ? "Generating Description..."
        : "Generate Description"}
    </button>

    <button
      type="button"
      className="secondary-button"
      onClick={() =>
        setIsSocialDescriptionPreviewOpen(true)
      }
      disabled={
        Object.keys(socialDescriptions).length === 0
      }
    >
      Preview
    </button>

    <button
      type="button"
      className="secondary-button"
      onClick={handleSaveSocialDescriptionsToDrive}
      disabled={
        Object.keys(socialDescriptions).length === 0 ||
        socialDescriptionSaved ||
        isGeneratingSocialDescriptions
      }
    >
      {socialDescriptionSaved
        ? "Saved to Google Drive / outputs"
        : "Save Description"}
    </button>
  </div>

  {socialDescriptionError && (
    <p
      style={{
        margin: "9px 0 0",
        color: "#d84a6a",
        fontSize: "12px",
      }}
    >
      {socialDescriptionError}
    </p>
  )}

  {socialDescriptionSaveMessage && (
    <p
      style={{
        margin: "9px 0 0",
        fontSize: "12px",
      }}
    >
      {socialDescriptionSaveMessage}
    </p>
  )}

  {Object.keys(socialDescriptions).length > 0 && (
    <div
      style={{
        display: "grid",
        gap: "7px",
        marginTop: "12px",
      }}
    >
      {(
        Object.entries(
          socialDescriptions,
        ) as Array<
          [string, SocialDescriptionItem]
        >
      ).map(([platform, item]) => (
        <div
          key={platform}
          style={{
            padding: "9px 10px",
            borderRadius: "9px",
            background:
              "rgba(255,255,255,0.045)",
            border:
              "1px solid rgba(255,255,255,0.08)",
          }}
        >
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              gap: "8px",
              fontSize: "11px",
            }}
          >
            <strong>{platform}</strong>
            <span style={{ opacity: 0.65 }}>
              {item.character_count} /{" "}
              {item.character_limit}
            </span>
          </div>

          <p
            style={{
              margin: "6px 0 0",
              fontSize: "12px",
              lineHeight: 1.45,
              whiteSpace: "pre-wrap",
            }}
          >
            {item.text}
          </p>
        </div>
      ))}
    </div>
  )}
</div>

                {isOutputFolderPickerOpen && (
                  <div
                    style={{
                      marginTop: "16px",
                      padding: "14px",
                      borderRadius: "12px",
                      border: "1px solid rgba(120, 100, 255, 0.35)",
                      background: "rgba(255,255,255,0.035)",
                    }}
                  >
                    <label
                      htmlFor="output-drive-folder"
                      style={{
                        display: "block",
                        marginBottom: "8px",
                        fontWeight: 700,
                      }}
                    >
                      Select Google Drive output folder
                    </label>

                    {driveOutputFolders.length > 0 ? (
                      <>
                        <select
                          id="output-drive-folder"
                          value={selectedOutputFolderId}
                          onChange={(event) => {
                            setSelectedOutputFolderId(event.target.value);
                            setGeneratedImageSaved(false);
                            setGeneratedImageSaveMessage("");
                          }}
                          disabled={isSavingGeneratedImage}
                          style={{
                            width: "100%",
                            padding: "11px 12px",
                            borderRadius: "10px",
                            border: "1px solid rgba(255,255,255,0.14)",
                            background: "rgba(255,255,255,0.05)",
                            color: "inherit",
                            font: "inherit",
                          }}
                        >
                          <option value="">
                            Select a configured Google Drive folder
                          </option>
                          {driveOutputFolders.map((folder, index) => (
                            <option key={folder.id} value={folder.id}>
                              {folder.name || `Google Drive ${index + 1}`}
                            </option>
                          ))}
                        </select>

                        <span
                          style={{
                            display: "block",
                            marginTop: "7px",
                            fontSize: "12px",
                            opacity: 0.7,
                          }}
                        >
                          The generated image will be saved inside an{" "}
                          <strong>outputs</strong> subfolder of the selected
                          folder. These folders come from the uploaded API key
                          configuration.
                        </span>

                        <div
                          style={{
                            display: "flex",
                            gap: "8px",
                            marginTop: "12px",
                            flexWrap: "wrap",
                          }}
                        >
                          <button
                            type="button"
                            className="primary-button"
                            onClick={handleSaveGeneratedImageToDrive}
                            disabled={
                              isSavingGeneratedImage ||
                              generatedImageSaved ||
                              !selectedOutputFolderId
                            }
                          >
                            {isSavingGeneratedImage
                              ? "Saving..."
                              : generatedImageSaved
                                ? "Saved to Google Drive / outputs"
                                : "Confirm Save"}
                          </button>

                          {!isSavingGeneratedImage && !generatedImageSaved && (
                            <button
                              type="button"
                              className="secondary-button"
                              onClick={() => {
                                setIsOutputFolderPickerOpen(false);
                                setGeneratedImageSaveMessage("");
                              }}
                            >
                              Cancel
                            </button>
                          )}
                        </div>
                      </>
                    ) : (
                      <div>
                        <p style={{ margin: 0, opacity: 0.75 }}>
                          No output Google Drive folders were configured in the
                          uploaded API key file.
                        </p>
                        <button
                          type="button"
                          className="secondary-button"
                          onClick={() => setIsOutputFolderPickerOpen(false)}
                          style={{ marginTop: "10px" }}
                        >
                          Close
                        </button>
                      </div>
                    )}
                  </div>
                )}

                <button
                  type="button"
                  className="primary-button"
                  onClick={handleSaveGeneratedImageToDrive}
                  disabled={
                    isSavingGeneratedImage ||
                    generatedImageSaved
                  }
                  style={{ marginTop: 16 }}
                >
                  {isSavingGeneratedImage
                    ? "Saving to Google Drive..."
                    : generatedImageSaved
                      ? "Saved to Google Drive / outputs"
                      : "Save"}
                </button>

                {generatedImageSaveMessage && (
                  <p style={{ marginTop: 8 }}>
                    {generatedImageSaveMessage}
                  </p>
                )}

              </div>

            </div>

          ) : (

            <div className="output-placeholder">

              <div className="placeholder-icon">
                □
              </div>


              <div>

                <h3>
                  No generated outputs yet
                </h3>

                <p>
                  Enter a content prompt and
                  generate an image using the
                  selected reference and template.
                </p>

              </div>

            </div>

          )}

        </section>

      </main>


      {isSocialDescriptionPreviewOpen && (
        <div
          className="modal-backdrop"
          onMouseDown={() =>
            setIsSocialDescriptionPreviewOpen(false)
          }
          style={{
            position: "fixed",
            inset: 0,
            zIndex: 1000,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            padding: "24px",
            background: "rgba(0,0,0,0.72)",
          }}
        >
          <div
            onMouseDown={(event) =>
              event.stopPropagation()
            }
            style={{
              width: "min(900px, 100%)",
              maxHeight: "85vh",
              overflowY: "auto",
              borderRadius: "16px",
              border:
                "1px solid rgba(255,255,255,0.12)",
              background: "#15151a",
              padding: "20px",
              boxShadow:
                "0 24px 80px rgba(0,0,0,0.45)",
            }}
          >
            <div
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                gap: "12px",
                marginBottom: "16px",
              }}
            >
              <div>
                <div
                  className="section-kicker"
                  style={{
                    fontSize: "11px",
                    fontWeight: 800,
                    opacity: 0.7,
                  }}
                >
                  PREVIEW
                </div>
                <h2 style={{ margin: "4px 0 0" }}>
                  Social Media Descriptions
                </h2>
              </div>

              <button
                type="button"
                className="secondary-button"
                onClick={() =>
                  setIsSocialDescriptionPreviewOpen(
                    false,
                  )
                }
              >
                Close
              </button>
            </div>

            {socialDescriptionPreviewContent && (
              <article
                style={{
                  padding: "14px",
                  borderRadius: "12px",
                  border: "1px solid rgba(255,255,255,0.10)",
                  background: "rgba(255,255,255,0.035)",
                }}
              >
                <strong>Generated Description File</strong>
                {socialDescriptionFilename && (
                  <div style={{ marginTop: "5px", fontSize: "12px", opacity: 0.65 }}>
                    {socialDescriptionFilename}
                  </div>
                )}
                <p
                  style={{
                    margin: "10px 0 0",
                    whiteSpace: "pre-wrap",
                    lineHeight: 1.5,
                  }}
                >
                  {socialDescriptionPreviewContent}
                </p>
              </article>
            )}

            <div
              style={{
                display: "grid",
                gap: "12px",
              }}
            >
              {(
                Object.entries(
                  socialDescriptions,
                ) as Array<
                  [string, SocialDescriptionItem]
                >
              ).map(([platform, item]) => (
                <article
                  key={platform}
                  style={{
                    padding: "14px",
                    borderRadius: "12px",
                    border:
                      "1px solid rgba(255,255,255,0.10)",
                    background:
                      "rgba(255,255,255,0.035)",
                  }}
                >
                  <div
                    style={{
                      display: "flex",
                      justifyContent:
                        "space-between",
                      gap: "8px",
                    }}
                  >
                    <strong>{platform}</strong>
                    <span
                      style={{
                        opacity: 0.65,
                        fontSize: "12px",
                      }}
                    >
                      {item.character_count} /{" "}
                      {item.character_limit}
                    </span>
                  </div>

                  <p
                    style={{
                      margin: "8px 0 0",
                      whiteSpace: "pre-wrap",
                      lineHeight: 1.5,
                    }}
                  >
                    {item.text}
                  </p>
                </article>
              ))}
            </div>

            <div
              style={{
                display: "flex",
                justifyContent: "flex-end",
                marginTop: "16px",
              }}
            >
              <button
                type="button"
                className="primary-button"
                onClick={
                  handleSaveSocialDescriptionsToDrive
                }
                disabled={
                  socialDescriptionSaved ||
                  Object.keys(
                    socialDescriptions,
                  ).length === 0
                }
              >
                {socialDescriptionSaved
                  ? "Saved to Google Drive / outputs"
                  : "Save Description"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ========================================================
          REFERENCE MODAL
          ======================================================== */}

      {showReferenceModal && (

        <div
          className="modal-backdrop"
          onMouseDown={() =>
            setShowReferenceModal(
              false,
            )
          }
        >

          <div
            className="reference-modal"
            onMouseDown={(
              event,
            ) =>
              event.stopPropagation()
            }
          >


            <div className="modal-header">

              <div>

                <div className="section-kicker">
                  REFERENCE
                </div>

                <h2>
                  Add Reference
                </h2>

              </div>


              <button
                type="button"
                className="modal-close"
                onClick={() =>
                  setShowReferenceModal(
                    false,
                  )
                }
              >
                ×
              </button>

            </div>


            <div className="modal-options">


              <button
                type="button"
                className="modal-option"
                onClick={
                  handleUploadClick
                }
              >

                <span className="modal-option-icon">
                  ↑
                </span>


                <span>

                  <strong>
                    Upload Media
                  </strong>

                  <small>
                    PNG, JPG, WEBP, GIF,
                    PDF, MP4 and more
                  </small>

                </span>

              </button>


              <button
                type="button"
                className="modal-option"
                onClick={() =>
                  setShowUrlInput(
                    !showUrlInput,
                  )
                }
              >

                <span className="modal-option-icon">
                  ↗
                </span>


                <span>

                  <strong>
                    Use Link
                  </strong>

                  <small>
                    YouTube or image URL
                  </small>

                </span>

              </button>

            </div>


            {showUrlInput && (

              <div className="url-input-area">


                <input
                  type="url"
                  value={
                    externalUrl
                  }
                  onChange={(
                    event,
                  ) => {

                    setExternalUrl(
                      event.target
                        .value,
                    );

                    setUrlError("");

                  }}
                  placeholder="https://..."
                  className="url-input"
                />


                <button
                  type="button"
                  className="primary-button"
                  onClick={
                    handleExternalUrl
                  }
                >
                  Add Link
                </button>


                {urlError && (
                  <div className="error-message">
                    {urlError}
                  </div>
                )}

              </div>

            )}


            <input
              ref={
                uploadInputRef
              }
              type="file"
              hidden
              accept="
                image/png,
                image/jpeg,
                image/webp,
                image/gif,
                application/pdf,
                video/mp4,
                video/webm,
                video/quicktime
              "
              multiple
              onChange={
                handleFileInputChange
              }
            />

          </div>

        </div>

      )}

    </div>
  );
}


function ProjectManager() {
  return null;
}


/*
 * ------------------------------------------------------------
 * Application session persistence
 * ------------------------------------------------------------
 *
 * Keep navigation state for the current browser session.
 * Only non-secret state is stored here:
 *   - whether API setup has been completed
 *   - selected API service IDs
 *   - selected API service names (for display only)
 *
 * Actual API credentials are never stored in browser
 * sessionStorage.
 */
const APP_SESSION_KEY =
  "image-generator-ui-session";


interface AppSessionState {
  apiSetupComplete: boolean;
  selectedApiKeys: string[];
  selectedApiServices: SelectedApiService[];
  availableApiServices: SelectedApiService[];
}


function readAppSession(): AppSessionState {
  const defaultSession: AppSessionState = {
    apiSetupComplete: false,
    selectedApiKeys: [],
    selectedApiServices: [],
    availableApiServices: [],
  };

  try {
    const stored =
      window.sessionStorage.getItem(
        APP_SESSION_KEY,
      );

    if (!stored) {
      return defaultSession;
    }

    const parsed =
      JSON.parse(stored) as Partial<AppSessionState>;

    return {
      apiSetupComplete:
        parsed.apiSetupComplete === true,
      selectedApiKeys:
        Array.isArray(parsed.selectedApiKeys)
          ? parsed.selectedApiKeys.filter(
              (value): value is string =>
                typeof value === "string",
            )
          : [],
      availableApiServices:
        Array.isArray(parsed.availableApiServices)
          ? parsed.availableApiServices
              .filter(
                (service): service is SelectedApiService =>
                  Boolean(service) &&
                  typeof service === "object" &&
                  typeof (service as SelectedApiService).id === "string" &&
                  typeof (service as SelectedApiService).name === "string",
              )
              .map((service) => ({
                id: service.id,
                name: service.name,
                keyName: service.keyName,
              }))
          : [],
      selectedApiServices:
        Array.isArray(parsed.selectedApiServices)
          ? parsed.selectedApiServices
              .filter(
                (service): service is SelectedApiService =>
                  Boolean(service) &&
                  typeof service === "object" &&
                  typeof (service as SelectedApiService).id === "string" &&
                  typeof (service as SelectedApiService).name === "string",
              )
              .map((service) => ({
                id: service.id,
                name: service.name,
              }))
          : [],
    };
  } catch (error) {
    console.warn(
      "Unable to restore application session:",
      error,
    );

    return defaultSession;
  }
}


function writeAppSession(
  state: AppSessionState,
) {
  try {
    window.sessionStorage.setItem(
      APP_SESSION_KEY,
      JSON.stringify({
        apiSetupComplete:
          state.apiSetupComplete,
        selectedApiKeys:
          state.selectedApiKeys,
        selectedApiServices:
          state.selectedApiServices,
        availableApiServices:
          state.availableApiServices,
      }),
    );
  } catch (error) {
    console.warn(
      "Unable to save application session:",
      error,
    );
  }
}



/* ========================= HOME PAGE ========================= */

function HomePage({
  onOpenImageGenerator,
  onOpenContentGenerator,
}: {
  onOpenImageGenerator: () => void;
  onOpenContentGenerator: () => void;
}) {
  const openImageGenerator = onOpenImageGenerator;
  const openContentGenerator = onOpenContentGenerator;

  return (
    <main className="home-page">
      <div className="home-orb home-orb-purple" />
      <div className="home-orb home-orb-pink" />
      <div className="home-orb home-orb-cyan" />

      <header className="home-header">
        <button
          type="button"
          className="home-brand"
          onClick={() => window.scrollTo({ top: 0, behavior: "smooth" })}
        >
          <span className="home-brand-mark">✦</span>
          <span className="home-brand-text">
            <strong>Creative AI</strong>
            <small>Studio</small>
          </span>
        </button>

        <div className="home-ai-badge">
          <span className="home-status-dot" />
          AI CREATIVE WORKSPACE
        </div>
      </header>

      <section className="home-hero">
        <div className="home-hero-copy">
          <div className="home-eyebrow">
            <span>✦</span>
            CREATE • DESIGN • GENERATE
          </div>

          <h1>
            Bring your ideas
            <span> to life with AI.</span>
          </h1>

          <p>
            A single creative workspace for generating powerful visuals and
            engaging content. Choose a workspace and start creating.
          </p>

          <div className="home-steps">
            <div className="home-step">
              <strong>01</strong>
              <span>Choose</span>
            </div>
            <div className="home-step-line" />
            <div className="home-step">
              <strong>02</strong>
              <span>Create</span>
            </div>
            <div className="home-step-line" />
            <div className="home-step">
              <strong>03</strong>
              <span>Refine</span>
            </div>
          </div>
        </div>

        <div className="home-visual" aria-hidden="true">
          <div className="home-ring home-ring-large" />
          <div className="home-ring home-ring-small" />
          <div className="home-visual-core">
            <span className="home-visual-spark">✦</span>
            <strong>AI</strong>
            <small>CREATE</small>
          </div>
          <span className="home-float home-float-one">✦</span>
          <span className="home-float home-float-two">◆</span>
          <span className="home-float home-float-three">●</span>
          <span className="home-float home-float-four">✧</span>
        </div>
      </section>

      <section className="home-workspaces">
        <div className="home-section-heading">
          <div>
            <span>YOUR WORKSPACES</span>
            <h2>What do you want to create?</h2>
          </div>
          <p>Pick a creative tool and turn your idea into something impressive.</p>
        </div>

        <div className="home-workspace-grid">
          <button
            type="button"
            className="home-workspace-card home-image-card"
            onClick={openImageGenerator}
          >
            <div className="home-card-glow" />

            <div className="home-card-top">
              <span className="home-card-icon home-image-icon">
                <span className="home-image-frame">
                  <span className="home-image-sun" />
                  <span className="home-image-mountain" />
                </span>
              </span>
              <span className="home-card-arrow">↗</span>
            </div>

            <div className="home-card-body">
              <span className="home-card-kicker">VISUAL CREATION</span>
              <h3>Image Generator</h3>
              <p>
                Create images from references, templates and prompts using
                your selected AI services.
              </p>
            </div>

            <div className="home-card-tags">
              <span>References</span>
              <span>Templates</span>
              <span>AI Images</span>
            </div>
          </button>

          <button
            type="button"
            className="home-workspace-card home-content-card"
            onClick={openContentGenerator}
          >
            <div className="home-card-glow" />

            <div className="home-card-top">
              <span className="home-card-icon home-content-icon">
                <span />
                <span />
                <span />
                <span />
              </span>
              <span className="home-card-arrow">↗</span>
            </div>

            <div className="home-card-body">
              <span className="home-card-kicker">CONTENT CREATION</span>
              <h3>Content Generator</h3>
              <p>
                Create structured and engaging content for training, marketing,
                presentations, documentation and more.
              </p>
            </div>

            <div className="home-card-tags">
              <span>Ideas</span>
              <span>Writing</span>
              <span>Content</span>
            </div>
          </button>
        </div>
      </section>

      <section className="home-bottom-banner">
        <span>✦</span>
        <div>
          <strong>One workspace. Endless possibilities.</strong>
          <small>Choose a tool above and start creating.</small>
        </div>
      </section>

      <footer className="home-footer">
        <span>Creative AI Studio</span>
        <span>AI-powered creation workspace</span>
      </footer>
    </main>
  );
}


function ContentGeneratorHome() {
  return (
    <main className="content-generator-placeholder">
      <div className="content-generator-placeholder-card">
        <div className="content-generator-placeholder-icon">✦</div>
        <span>CONTENT GENERATOR</span>
        <h1>Content creation workspace</h1>
        <p>
          This is the dedicated Content Generator area. The Home Page is now
          ready to launch it independently from the Image Generator.
        </p>
        <button
          type="button"
          onClick={() => {
            window.location.href = "/";
          }}
        >
          ← Back to Home
        </button>
      </div>
    </main>
  );
}


function App() {

  const normalizePath = (path: string) =>
    path.replace(/\/+$/, "") || "/";

  const [currentPath, setCurrentPath] = useState(() =>
    normalizePath(window.location.pathname),
  );

  useEffect(() => {
    const handlePopState = () => {
      setCurrentPath(normalizePath(window.location.pathname));
    };

    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  const [initialAppSession] = useState<AppSessionState>(
    () => readAppSession(),
  );

  const [apiSetupComplete, setApiSetupComplete] = useState(
    initialAppSession.apiSetupComplete,
  );

  const [selectedApiKeys, setSelectedApiKeys] = useState<string[]>(
    initialAppSession.selectedApiKeys,
  );

  const [selectedApiServices, setSelectedApiServices] =
    useState<SelectedApiService[]>(initialAppSession.selectedApiServices);

  const [availableApiServices, setAvailableApiServices] =
    useState<SelectedApiService[]>(initialAppSession.availableApiServices);

  // Keep Home, API Setup and Image Generator mounted so navigation between
  // them does not destroy the current Image Generator work in progress.
  const navigateTo = (path: string) => {
    const normalized = normalizePath(path);
    if (normalizePath(window.location.pathname) !== normalized) {
      window.history.pushState({}, "", normalized);
    }
    setCurrentPath(normalized);
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  const showHome = currentPath === "/";
  const showApiSetup =
    currentPath === "/api-setup" ||
    (currentPath === "/image-generator" && !apiSetupComplete);
  const showImageGenerator =
    currentPath === "/image-generator" && apiSetupComplete;

  // Rehydrate the backend's in-memory API selection after a backend restart.
  // The browser session can retain selectedApiKeys even though FastAPI has
  // reset its process state. Synchronizing here makes all AI endpoints
  // (tagging, templates, prompts and image generation) use the same selection.
  useEffect(() => {
    if (!apiSetupComplete || selectedApiKeys.length === 0) return;

    let cancelled = false;

    void (async () => {
      try {
        const statusResponse = await fetch(
          `${API_BASE_URL}/api/api-keys/status`,
          { credentials: "include" },
        );
        const statusData = await statusResponse.json().catch(() => null);

        if (!statusResponse.ok) {
          throw new Error(
            String(statusData?.detail || "Unable to read API key status."),
          );
        }

        if (!statusData?.configured) {
          if (!cancelled) {
            setSelectedApiKeys([]);
            setSelectedApiServices([]);
            setAvailableApiServices([]);
            setApiSetupComplete(false);
          }
          return;
        }

        const response = await fetch(`${API_BASE_URL}/api/api-keys/select`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ selected_ids: selectedApiKeys }),
          credentials: "include",
        });
        const data = await response.json().catch(() => null);

        if (!response.ok) {
          throw new Error(
            String(data?.detail || "Unable to restore API selection."),
          );
        }
      } catch (error) {
        if (!cancelled) {
          console.error("API selection restore failed:", error);
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [apiSetupComplete, selectedApiKeys.join("|")]);

  useEffect(() => {
    writeAppSession({
      apiSetupComplete,
      selectedApiKeys,
      selectedApiServices,
      availableApiServices,
    });
  }, [
    apiSetupComplete,
    selectedApiKeys,
    selectedApiServices,
    availableApiServices,
  ]);

  if (currentPath === "/content-generator") {
    return <ContentGeneratorHome />;
  }

  if (
    currentPath === "/project-manager" ||
    currentPath === "/projectmanager"
  ) {
    return <ProjectManager />;
  }

  // Unknown paths return to the home workspace.
  const safeHome =
    currentPath !== "/" &&
    currentPath !== "/api-setup" &&
    currentPath !== "/image-generator";

  return (
    <>
      <div
        style={{ display: showHome || safeHome ? "block" : "none" }}
        aria-hidden={!(showHome || safeHome)}
      >
        <HomePage
          onOpenImageGenerator={() => navigateTo("/api-setup")}
          onOpenContentGenerator={() => navigateTo("/content-generator")}
        />
      </div>

      <div
        style={{ display: showApiSetup ? "block" : "none" }}
        aria-hidden={!showApiSetup}
      >
        <ApiKeySetup
          onBackToHome={() => navigateTo("/")}
          onComplete={(selectedKeys, selectedServices) => {
            // Keep any API selections that are still present after returning
            // from the Image Generator. If the uploaded API file changed,
            // automatically discard only IDs that no longer exist.
            const availableIds = new Set(
              selectedServices.map((service) => service.id),
            );
            const validSelectedIds = selectedKeys.filter((id) =>
              availableIds.has(id),
            );

            setSelectedApiKeys(validSelectedIds);
            setSelectedApiServices(
              selectedServices.filter((service) =>
                validSelectedIds.includes(service.id),
              ),
            );
            setAvailableApiServices(selectedServices);
            setApiSetupComplete(true);
            navigateTo("/image-generator");
          }}
        />
      </div>

      <div
        style={{ display: showImageGenerator ? "block" : "none" }}
        aria-hidden={!showImageGenerator}
      >
        <ImageGenerator
          selectedApiKeys={selectedApiKeys}
          selectedApiServices={selectedApiServices}
          availableApiServices={availableApiServices}
          onApiSelectionChange={async (nextSelectedIds) => {
            try {
              const response = await fetch(`${API_BASE_URL}/api/api-keys/select`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ selected_ids: nextSelectedIds }),
                credentials: "include",
              });

              const data = await response.json().catch(() => null);
              if (!response.ok) {
                throw new Error(
                  String(data?.detail || "Unable to update API selection."),
                );
              }

              setSelectedApiKeys(nextSelectedIds);
              setSelectedApiServices(
                availableApiServices.filter((service) =>
                  nextSelectedIds.includes(service.id),
                ),
              );
            } catch (error) {
              console.error("API selection update failed:", error);
            }
          }}
          onBackToApiSetup={() => navigateTo("/api-setup")}
          onBackToHome={() => navigateTo("/")}
        />
      </div>
    </>
  );
}

export default App;
