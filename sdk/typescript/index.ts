export type RenderRequest = {
  url?: string;
  html?: string;
  markdown?: string;
  engine?: "chromium" | "firefox" | "webkit";
  output?: "png" | "jpeg" | "webp" | "avif" | "pdf" | "html" | "markdown" | "metadata" | "webm" | "mp4" | "gif";
  [key: string]: unknown;
};

export class ViperCapture {
  constructor(private baseUrl: string, private apiKey?: string) {}

  async render(request: RenderRequest): Promise<Uint8Array> {
    const response = await fetch(`${this.baseUrl.replace(/\/$/, "")}/v1/render`, {
      method: "POST",
      headers: {"content-type": "application/json", ...(this.apiKey ? {authorization: `Bearer ${this.apiKey}`} : {})},
      body: JSON.stringify(request),
    });
    if (!response.ok) throw new Error(`ViperCapture render failed: ${response.status} ${await response.text()}`);
    return new Uint8Array(await response.arrayBuffer());
  }

  async createJob(request: RenderRequest, requestId?: string): Promise<Record<string, unknown>> {
    const response = await fetch(`${this.baseUrl.replace(/\/$/, "")}/v1/jobs`, {
      method: "POST",
      headers: {"content-type": "application/json", ...(this.apiKey ? {authorization: `Bearer ${this.apiKey}`} : {}), ...(requestId ? {"x-request-id": requestId} : {})},
      body: JSON.stringify(request),
    });
    if (!response.ok) throw new Error(`ViperCapture job failed: ${response.status} ${await response.text()}`);
    return response.json();
  }
}
