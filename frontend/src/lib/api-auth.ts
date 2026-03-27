"use client";

type BuildApiHeadersOptions = {
  json?: boolean;
  adminToken?: string;
};

type ApiFetchOptions = RequestInit & BuildApiHeadersOptions;

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
  const response = await fetch(input, buildRequestInit(options));
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
