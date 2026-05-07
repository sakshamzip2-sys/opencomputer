// Typed REST client. Reads the session token from the <meta> tag injected
// by FastAPI's _render_html. Loopback-only by default — `Authorization`
// is attached when the meta tag's value is real.

const getToken = (): string => {
  const meta = document.querySelector<HTMLMetaElement>(
    'meta[name="oc-session-token"]',
  );
  const v = meta?.content ?? "";
  return v.includes("__SESSION_TOKEN__") ? "" : v;
};

const TOKEN = getToken();

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (TOKEN) headers.set("Authorization", `Bearer ${TOKEN}`);
  if (!headers.has("Content-Type") && init?.body) {
    headers.set("Content-Type", "application/json");
  }
  const resp = await fetch(path, { ...init, headers });
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      detail = (await resp.json()).detail ?? detail;
    } catch {
      // not JSON — use statusText
    }
    throw new ApiError(resp.status, detail);
  }
  if (resp.status === 204) return undefined as T;
  return resp.json() as Promise<T>;
}

export interface StatusResponse {
  profile: string;
  wire_url: string;
  version: string;
}
