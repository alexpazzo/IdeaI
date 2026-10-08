// Proxy mutevole: unico punto in cui la dashboard invia POST/PATCH/DELETE all'API.
// La chiave IDEAI_API_KEY vive solo qui (server-side); nessuna NEXT_PUBLIC_* esiste.
// Sono ammessi esclusivamente i path sotto indicati: qualunque altro → 403.

import { NextResponse } from "next/server";

const ALLOWED_PATHS: RegExp[] = [
  /^ideas\/\d+\/watch$/,
  /^ideas\/\d+\/reanalyze$/,
  /^ideas\/\d+\/merge$/,
  /^jobs\/\d+\/retry$/,
  /^targets\/\d+$/,
  /^sources\/\d+\/targets$/,
];

const ALLOWED_METHODS = new Set(["POST", "PATCH", "DELETE"]);

const API_BASE_URL = process.env.IDEAI_API_BASE_URL ?? "http://api:8000";
const API_KEY = process.env.IDEAI_API_KEY ?? "";

async function forward(
  request: Request,
  { params }: { params: Promise<{ path: string[] }> },
): Promise<Response> {
  if (!ALLOWED_METHODS.has(request.method)) {
    return NextResponse.json({ detail: "Metodo non ammesso" }, { status: 405 });
  }

  const { path } = await params;
  const target = path.join("/");
  if (!ALLOWED_PATHS.some((re) => re.test(target))) {
    return NextResponse.json({ detail: "Path non ammesso" }, { status: 403 });
  }

  const rawBody = request.method === "DELETE" ? "" : await request.text();
  const headers: Record<string, string> = { Authorization: `Bearer ${API_KEY}` };
  if (rawBody.length > 0) headers["Content-Type"] = "application/json";

  const upstream = await fetch(`${API_BASE_URL}/api/v1/${target}`, {
    method: request.method,
    headers,
    body: rawBody.length > 0 ? rawBody : undefined,
    cache: "no-store",
  });

  if (upstream.status === 204) {
    return new NextResponse(null, { status: 204 });
  }

  const text = await upstream.text();
  return new NextResponse(text, {
    status: upstream.status,
    headers: { "Content-Type": upstream.headers.get("content-type") ?? "application/json" },
  });
}

export const POST = forward;
export const PATCH = forward;
export const DELETE = forward;
