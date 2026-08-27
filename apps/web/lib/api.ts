const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem("access_token");
}
export function setToken(t: string) {
  localStorage.setItem("access_token", t);
}
export function clearToken() {
  localStorage.removeItem("access_token");
  localStorage.removeItem("org_id");
}
export function getOrgId(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem("org_id");
}
export function setOrgId(id: string) {
  localStorage.setItem("org_id", id);
}

export async function apiFetch(path: string, opts: RequestInit = {}) {
  const headers: Record<string, string> = { ...(opts.headers as Record<string,string> || {}) };
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const orgId = getOrgId();
  if (orgId) headers["X-Organization-Id"] = orgId;
  if (!(opts.body instanceof FormData) && opts.body) headers["Content-Type"] = "application/json";
  const res = await fetch(`${API_URL}${path}`, { ...opts, headers });
  if (!res.ok) {
    const data = await res.json().catch(()=>({detail: res.statusText}));
    throw new Error(data.detail || "خطای سرور");
  }
  return res.json();
}
