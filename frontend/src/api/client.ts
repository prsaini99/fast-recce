import axios, { type AxiosInstance } from "axios";

// `||` (not `??`) so an empty-string env var also falls back. Empty
// strings in .env files are common and would otherwise produce a broken
// baseURL ("") that resolves all requests against the SPA's own origin.
const BASE_URL = import.meta.env.VITE_API_URL || "/api/v1";

export const http: AxiosInstance = axios.create({
  baseURL: BASE_URL,
  headers: { "Content-Type": "application/json" },
});

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
