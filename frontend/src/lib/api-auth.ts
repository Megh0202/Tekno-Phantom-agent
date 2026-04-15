"use client";

type BuildApiHeadersOptions = {
  json?: boolean;
  adminToken?: string;
};

type ApiFetchOptions = RequestInit & BuildApiHeadersOptions;

export function resolveApiBaseUrl(): string {
  const configured = process.env.NEXT_PUBLIC_API_BASE_URL?.trim();
  if (typeof window === "undefined") {
    return configured ? configured.replace(/\/$/, "") : "";
  }

  const browserHost = window.location.hostname.trim();
  if (!configured) {
    return window.location.origin.replace(/\/$/, "");
  }

  try {
    const url = new URL(configured);
    const loopbackHosts = new Set(["localhost", "127.0.0.1"]);
    const configuredIsLoopback = loopbackHosts.has(url.hostname);
    const browserIsLoopback = loopbackHosts.has(browserHost);
    if (configuredIsLoopback !== browserIsLoopback) {
      url.hostname = browserHost;
      return url.toString().replace(/\/$/, "");
    }
    return configured.replace(/\/$/, "");
  } catch {
    return configured.replace(/\/$/, "");
  }
}

export function resolveLiveViewerUrl(apiBaseUrl: string): string {
  const fallbackOrigin = typeof window !== "undefined" ? window.location.origin : "";
  const base = apiBaseUrl || fallbackOrigin;
  if (!base) {
    return "/viewer/vnc.html?autoconnect=1&resize=remote&reconnect=1";
  }

  try {
    const origin = new URL(base).origin;
    return new URL("/viewer/vnc.html?autoconnect=1&resize=remote&reconnect=1", origin).toString();
  } catch {
    return `${base.replace(/\/$/, "")}/viewer/vnc.html?autoconnect=1&resize=remote&reconnect=1`;
  }
}

function getCookie(name: string): string {
  if (typeof document === "undefined") return "";
  const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = document.cookie.match(new RegExp(`(?:^|; )${escaped}=([^;]*)`));
  return match ? decodeURIComponent(match[1]) : "";
}

export function buildApiHeaders(options?: BuildApiHeadersOptions): HeadersInit {
  const headers: Record<string, string> = {};
  if (options?.json) headers["Content-Type"] = "application/json";

  const adminToken = options?.adminToken?.trim() ?? "";
  if (adminToken) {
    headers["X-Admin-Token"] = adminToken;
  }
  return headers;
}

function buildRequestInit(options?: ApiFetchOptions): RequestInit {
  const { json, adminToken, headers, credentials, ...rest } = options ?? {};
  const mergedHeaders = new Headers(buildApiHeaders({ json, adminToken }));
  if (headers) {
    new Headers(headers).forEach((value, key) => mergedHeaders.set(key, value));
  }
  const csrfToken = getCookie("tekno_phantom_csrf");
  if (csrfToken && !mergedHeaders.has("X-CSRF-Token")) {
    mergedHeaders.set("X-CSRF-Token", csrfToken);
  }
  return {
    ...rest,
    headers: mergedHeaders,
    credentials: credentials ?? "include",
  };
}

export async function apiFetch(input: RequestInfo | URL, options?: ApiFetchOptions): Promise<Response> {
  let response: Response;
  try {
    response = await fetch(input, buildRequestInit(options));
  } catch (error) {
    if (error instanceof TypeError) {
      const url = typeof input === "string" ? input : input.toString();
      let hint = "Failed to reach the backend.";
      if (typeof window !== "undefined") {
        const pageHost = window.location.hostname;
        try {
          const apiHost = new URL(url, window.location.origin).hostname;
          if (
            (pageHost === "127.0.0.1" && apiHost === "localhost") ||
            (pageHost === "localhost" && apiHost === "127.0.0.1")
          ) {
            hint = `Failed to reach the backend. Open frontend and backend on the same host name (${pageHost}) or allow both loopback origins.`;
          }
        } catch {
          // Ignore URL parsing issues and fall back to the generic hint.
        }
      }
      throw new Error(hint);
    }
    throw error;
  }
  const url = typeof input === "string" ? input : input.toString();
  const isAuthRefreshCall = url.includes("/auth/refresh");
  const isAuthLoginOrRegister = url.includes("/auth/login") || url.includes("/auth/register");
  const shouldRetry = response.status === 401 && !isAuthRefreshCall && !isAuthLoginOrRegister;
  if (!shouldRetry) {
    return response;
  }

  const targetUrl = new URL(url, window.location.origin);
  const refreshUrl = new URL("/auth/refresh", targetUrl.origin);
  const refreshResponse = await fetch(refreshUrl, buildRequestInit({ method: "POST" }));
  if (!refreshResponse.ok) {
    return response;
  }
  return fetch(input, buildRequestInit(options));
}

export async function ensureCsrfCookie(apiBaseUrl: string): Promise<void> {
  const csrfToken = getCookie("tekno_phantom_csrf");
  if (csrfToken) {
    return;
  }
  const response = await fetch(`${apiBaseUrl}/auth/csrf`, buildRequestInit({ method: "GET" }));
  if (!response.ok) {
    throw new Error("Failed to initialize CSRF protection");
  }
}
