/** arena 前端 API client(同源 fetch;开发期 vite proxy /api -> 50180)。 */

const API_BASE = ""

export async function apiGet<T = any>(path: string): Promise<T> {
  const r = await fetch(API_BASE + path)
  if (!r.ok) throw new Error(`${path}: ${r.status} ${r.statusText}`)
  return r.json()
}

export async function apiJson<T = any>(
  path: string,
  method: "POST" | "PUT" | "DELETE",
  body?: any,
  token?: string,
): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" }
  if (token) headers["x-admin-token"] = token
  const r = await fetch(API_BASE + path, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  if (!r.ok) throw new Error(`${path}: ${r.status} ${r.statusText}`)
  return r.json()
}

/** 管理员 token(localStorage)。login 端点也设 httponly cookie,这里存一份供 X-Admin-Token。 */
export const adminToken = {
  get: () => localStorage.getItem("arena_admin_token"),
  set: (t: string) => localStorage.setItem("arena_admin_token", t),
  clear: () => localStorage.removeItem("arena_admin_token"),
}
