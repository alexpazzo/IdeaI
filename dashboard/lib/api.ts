// Fetch server-side verso l'API IdeaI. La chiave vive SOLO lato server:
// nessuna variabile NEXT_PUBLIC_* è usata in tutta la dashboard.

const API_BASE_URL = process.env.IDEAI_API_BASE_URL ?? "http://api:8000";
const API_KEY = process.env.IDEAI_API_KEY ?? "";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

export async function apiGet<T>(path: string, params?: Record<string, unknown>): Promise<T> {
  const url = new URL(`/api/v1${path}`, API_BASE_URL);
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value === undefined || value === null || value === "") continue;
      url.searchParams.set(key, String(value));
    }
  }
  const res = await fetch(url, {
    headers: { Authorization: `Bearer ${API_KEY}` },
    cache: "no-store",
  });
  if (!res.ok) {
    throw new ApiError(res.status, `GET ${path} → ${res.status} ${res.statusText}`);
  }
  return (await res.json()) as T;
}

/** GET che degrada a `fallback` invece di far fallire la pagina. */
export async function apiGetSafe<T>(path: string, fallback: T, params?: Record<string, unknown>): Promise<T> {
  try {
    return await apiGet<T>(path, params);
  } catch {
    return fallback;
  }
}

export { API_BASE_URL };
