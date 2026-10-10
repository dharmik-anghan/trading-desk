/**
 * The backend. Overridable (VITE_API_BASE) so a second copy can run beside the desk.
 * In dev it is the page's own origin: Vite proxies /api, which is what lets a
 * phone on a tunnel reach it.
 */
export const API_BASE: string =
  import.meta.env.VITE_API_BASE ?? (import.meta.env.DEV ? "" : "http://127.0.0.1:8000");

/**
 * A failure the desk can explain rather than just show as red text.
 *
 * The backend classifies broker trouble into a stable `code` (see
 * broker/errors.py), so "rate limited" can be presented as a passing
 * condition while "sign-in expired" tells you to go and do something.
 */
export class ApiError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(code: string, message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
  }

  /** Transient: the figures on screen are simply a little behind. */
  get isTransient(): boolean {
    return this.code === "rate_limited";
  }
}

async function toApiError(path: string, response: Response): Promise<ApiError> {
  const text = await response.text();
  try {
    const detail = JSON.parse(text)?.detail;
    if (detail && typeof detail === "object" && typeof detail.code === "string") {
      return new ApiError(detail.code, detail.message ?? detail.code, response.status);
    }
    // A refused order lists every check it failed.
    if (detail && typeof detail === "object" && Array.isArray(detail.reasons)) {
      return new ApiError("request_failed", detail.reasons.join("; "), response.status);
    }
    if (typeof detail === "string") {
      return new ApiError("request_failed", detail, response.status);
    }
  } catch {
    // not JSON - fall through
  }
  const said = text ? ` ${text}` : "";
  return new ApiError("request_failed", `${path} failed: ${response.status}${said}`, response.status);
}

/**
 * Every request the desk makes. One place, so a write fails the same way a read
 * does - an `ApiError`, and "the backend is down" said as that rather than as
 * the browser's "Failed to fetch". A body-less answer (204) resolves to undefined.
 */
export async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    // the backend itself is not answering, which is different from the broker
    throw new ApiError("api_unreachable", "Cannot reach the backend. Is uvicorn running?", 0);
  }
  if (!response.ok) {
    throw await toApiError(path, response);
  }
  const text = await response.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

export const getJson = <T>(path: string): Promise<T> => request<T>("GET", path);

export const postJson = <TRequest, TResponse>(path: string, body: TRequest): Promise<TResponse> =>
  request<TResponse>("POST", path, body);

export const putJson = <TRequest, TResponse>(path: string, body: TRequest): Promise<TResponse> =>
  request<TResponse>("PUT", path, body);

export const del = (path: string): Promise<void> => request<void>("DELETE", path);

/** A failure as the desk should present it: what to say, and how loudly. */
export function describeError(error: Error | null): { text: string; transient: boolean } | null {
  if (!error) return null;
  if (error instanceof ApiError) {
    return { text: error.message, transient: error.isTransient };
  }
  return { text: error.message, transient: false };
}
