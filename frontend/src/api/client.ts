import axios, { type AxiosInstance, type AxiosRequestConfig } from "axios";

/**
 * Single axios instance + JWT interceptor.
 *
 * - Access token is read from localStorage on every request.
 * - On 401, we try /auth/refresh once with the stored refresh token.
 *   If refresh also fails, we clear storage and bounce to /login.
 */

export const ACCESS_TOKEN_KEY = "fastrecce.access_token";
export const REFRESH_TOKEN_KEY = "fastrecce.refresh_token";
export const USER_KEY = "fastrecce.user";

// `||` (not `??`) so an empty-string env var also falls back. Empty
// strings in .env files are common and would otherwise produce a broken
// baseURL ("") that resolves all requests against the SPA's own origin.
const BASE_URL = import.meta.env.VITE_API_URL || "/api/v1";

export const http: AxiosInstance = axios.create({
  baseURL: BASE_URL,
  headers: { "Content-Type": "application/json" },
});

http.interceptors.request.use((config) => {
  const token = localStorage.getItem(ACCESS_TOKEN_KEY);
  if (token) {
    config.headers = config.headers ?? {};
    (config.headers as Record<string, string>).Authorization = `Bearer ${token}`;
  }
  return config;
});

let refreshPromise: Promise<string | null> | null = null;

async function tryRefresh(): Promise<string | null> {
  const refresh = localStorage.getItem(REFRESH_TOKEN_KEY);
  if (!refresh) return null;

  try {
    const { data } = await axios.post(
      `${BASE_URL}/auth/refresh`,
      { refresh_token: refresh },
      { headers: { "Content-Type": "application/json" } }
    );
    localStorage.setItem(ACCESS_TOKEN_KEY, data.access_token);
    return data.access_token as string;
  } catch {
    localStorage.removeItem(ACCESS_TOKEN_KEY);
    localStorage.removeItem(REFRESH_TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
    return null;
  }
}

http.interceptors.response.use(
  (resp) => resp,
  async (error) => {
    const original = error.config as AxiosRequestConfig & { _retry?: boolean };
    const status = error.response?.status;

    if (status === 401 && !original._retry && !original.url?.endsWith("/auth/login")) {
      original._retry = true;
      refreshPromise = refreshPromise ?? tryRefresh();
      const newToken = await refreshPromise;
      refreshPromise = null;

      if (newToken) {
        original.headers = original.headers ?? {};
        (original.headers as Record<string, string>).Authorization = `Bearer ${newToken}`;
        return http(original);
      }

      if (typeof window !== "undefined" && !window.location.pathname.startsWith("/login")) {
        window.location.replace("/login");
      }
    }
    return Promise.reject(error);
  }
);

/** Extract the most helpful message out of a backend error response. */
export function extractErrorMessage(err: unknown): string {
  if (axios.isAxiosError(err)) {
    const payload = err.response?.data;
    if (payload?.errors?.[0]?.message) {
      return payload.errors[0].message as string;
    }
    if (err.response?.statusText) {
      return `${err.response.status} ${err.response.statusText}`;
    }
    return err.message;
  }
  return err instanceof Error ? err.message : "Unknown error";
}
