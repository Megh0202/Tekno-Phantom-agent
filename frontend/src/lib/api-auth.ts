"use client";

type BuildApiHeadersOptions = {
  json?: boolean;
  adminToken?: string;
};

type ApiFetchOptions = RequestInit & BuildApiHeadersOptions;

function buildLocalApiBaseUrl(): string {
  return "/backend-proxy";
}

export function getApiBaseUrl(): string {
  const configuredBaseUrl = process.env.NEXT_PUBLIC_API_BASE_URL?.trim();
  return configuredBaseUrl || buildLocalApiBaseUrl();
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

export async function ensureCsrfCookie(apiBaseUrl: string, adminToken?: string): Promise<void> {
  if (adminToken?.trim()) {
    return;
  }

  const existingCsrfToken = getCookie("tekno_phantom_csrf");
  if (existingCsrfToken) {
    return;
  }

  const response = await fetch(`${apiBaseUrl}/auth/csrf`, buildRequestInit({ adminToken }));
  if (!response.ok) {
    throw new Error(`Failed to initialize CSRF protection: ${response.status} ${response.statusText}`);
  }
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
      let hint = `Failed to reach the backend at ${url}.`;
      if (typeof window !== "undefined") {
        const pageHost = window.location.hostname;
        try {
          const apiHost = new URL(url, window.location.origin).hostname;
          if (
            (pageHost === "127.0.0.1" && apiHost === "localhost") ||
            (pageHost === "localhost" && apiHost === "127.0.0.1")
          ) {
            hint = `Failed to reach the backend at ${url}. Open frontend and backend on the same host name (${pageHost}) or allow both loopback origins.`;
          }
        } catch {
          // Ignore URL parsing issues and fall back to the generic hint.
        }
      }
      const detail = error.message?.trim();
      throw new Error(detail ? `${hint} Browser error: ${detail}` : hint);
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
  const refreshPath = targetUrl.pathname.includes("/backend-proxy/")
    ? "/backend-proxy/auth/refresh"
    : "/auth/refresh";
  const refreshUrl = new URL(refreshPath, targetUrl.origin);
  const refreshResponse = await fetch(refreshUrl, buildRequestInit({ method: "POST" }));
  if (!refreshResponse.ok) {
    return response;
  }
  return fetch(input, buildRequestInit(options));
}
