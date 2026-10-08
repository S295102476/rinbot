import { useCallback, useEffect, useRef, useState } from "react";

let csrfToken = "";
export function setCsrfToken(token: string) {
  csrfToken = token;
}

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}

export async function api<T = Record<string, unknown>>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body) headers.set("Content-Type", "application/json");
  if (options.method && options.method !== "GET")
    headers.set("X-CSRF-Token", csrfToken);
  const response = await fetch(`/api/admin${path}`, {
    ...options,
    headers,
    credentials: "same-origin",
  });
  const contentType = response.headers.get("content-type") || "";
  const data = contentType.includes("application/json")
    ? await response.json()
    : null;
  if (!response.ok) {
    if (
      response.status === 401 &&
      path !== "/auth/login" &&
      path !== "/auth/me"
    ) {
      window.dispatchEvent(new Event("session-expired"));
    }
    const detail = data?.detail;
    throw new ApiError(
      typeof detail === "string"
        ? detail
        : detail?.message || data?.message || `请求失败 (${response.status})`,
      response.status,
    );
  }
  return data as T;
}

export function write<T = Record<string, unknown>>(
  path: string,
  body?: unknown,
  method = "POST",
) {
  return api<T>(path, {
    method,
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });
}

export function query(values: Record<string, unknown>) {
  const result = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "")
      result.set(key, String(value));
  });
  return result.toString() ? `?${result}` : "";
}

export function useApi<T>(path: string | null) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(!!path);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const generation = useRef(0);
  const reload = useCallback(() => setRevision((value) => value + 1), []);
  useEffect(() => {
    const current = ++generation.current;
    if (!path) {
      setLoading(false);
      setData(null);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError("");
    setData(null);
    api<T>(path, { signal: controller.signal })
      .then((value) => {
        if (current === generation.current) setData(value);
      })
      .catch((cause: Error) => {
        if (cause.name !== "AbortError" && current === generation.current)
          setError(cause.message);
      })
      .finally(() => {
        if (current === generation.current) setLoading(false);
      });
    return () => controller.abort();
  }, [path, revision]);
  return { data, loading, error, reload };
}

export type Row = Record<string, any>;
export type ListResult = { items: Row[]; total: number };
export const number = (value: unknown) =>
  value === undefined || value === null
    ? "—"
    : Number(value).toLocaleString("zh-CN");
export const dateTime = (value: unknown) => {
  if (!value) return "—";
  const date =
    typeof value === "number"
      ? new Date(value * 1000)
      : new Date(String(value));
  return Number.isNaN(date.getTime())
    ? String(value)
    : date.toLocaleString("zh-CN", { hour12: false });
};
export const errorMessage = (cause: unknown) =>
  cause instanceof Error ? cause.message : "操作失败，请重试";
