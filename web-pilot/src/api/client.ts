// Calls to the core API, typed from the generated schema (web-pilot/openapi.json
// -> src/api/schema.d.ts, `npm run gen:api`). Never hand-write a response type.
import type { paths } from "./schema";

type JsonOf<T> = T extends { content: { "application/json": infer B } } ? B : never;

export type GetResponse<P extends keyof paths> = paths[P] extends {
  get: { responses: { 200: infer R } };
}
  ? JsonOf<R>
  : never;

export class SignInRequired extends Error {}

function toSignIn(): never {
  const next = location.pathname + location.search;
  location.replace(`/login?next=${encodeURIComponent(next)}`);
  throw new SignInRequired("sign in required");
}

export async function apiGet<P extends keyof paths>(path: P, query = ""): Promise<GetResponse<P>> {
  const response = await fetch(`${String(path)}${query}`, { credentials: "same-origin" });
  if (response.status === 401) toSignIn();
  if (!response.ok) throw new Error(`${String(path)}: ${response.status}`);
  return (await response.json()) as GetResponse<P>;
}

// A lookup of exactly one record: null when the API says there is none (404),
// an error for anything else that is not a success.
export async function apiLookup<P extends keyof paths>(
  path: P,
  query: string,
): Promise<GetResponse<P> | null> {
  const response = await fetch(`${String(path)}${query}`, { credentials: "same-origin" });
  if (response.status === 401) toSignIn();
  if (response.status === 404) return null;
  if (!response.ok) throw new Error(`${String(path)}: ${response.status}`);
  return (await response.json()) as GetResponse<P>;
}

// A PUT with a JSON body, as `apiPost`: the same header, the same 401.
export async function apiPut(path: string, body: unknown): Promise<Response> {
  const response = await fetch(path, {
    method: "PUT",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Courier-Request": "1" },
    body: JSON.stringify(body),
  });
  if (response.status === 401) toSignIn();
  return response;
}

// State changes carry X-Courier-Request, which a form on another site cannot
// send (api/auth.py). The API refuses a cookie-authenticated change without it.
export async function apiPost(path: string, body?: unknown): Promise<Response> {
  return apiSend("POST", path, body === undefined ? undefined : JSON.stringify(body));
}

// Any change: a JSON body already encoded (an uploaded file is sent as it
// was read, not parsed and re-encoded), or none.
export async function apiSend(
  method: "POST" | "PUT" | "DELETE",
  path: string,
  body?: string,
): Promise<Response> {
  const response = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Courier-Request": "1" },
    body,
  });
  if (response.status === 401) toSignIn();
  return response;
}
