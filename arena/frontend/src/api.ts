/** arena 前端 API client(里程碑 8)。
 *
 * 统一鉴权:
 * - 登录成功后把 token 存 localStorage(arena_user_token),请求带 Authorization: Bearer。
 * - 同时后端设了 httponly cookie(arena_session),这里默认带 credentials:'include',
 *   同源 fetch 会自动带上 cookie —— 双保险(Bearer 优先)。
 * - 401 → 自动清理 token + 跳 /login(由 requireAuth 触发)。
 */

const TOKEN_KEY = 'arena_user_token'
const USER_KEY = 'arena_user'

/** 用户对象(登录后存 localStorage,供 UI 显示)。 */
export interface CurrentUser {
  id: number
  username: string
  email: string
  role: 'user' | 'admin'
  display_name: string
  is_active: number
  email_verified?: number
  created_at?: string
  last_login_at?: string
}

export const userToken = {
  get: (): string | null => localStorage.getItem(TOKEN_KEY),
  set: (t: string) => localStorage.setItem(TOKEN_KEY, t),
  clear: () => localStorage.removeItem(TOKEN_KEY),
}

export const currentUserStore = {
  get: (): CurrentUser | null => {
    const raw = localStorage.getItem(USER_KEY)
    if (!raw) return null
    try {
      return JSON.parse(raw) as CurrentUser
    } catch {
      return null
    }
  },
  set: (u: CurrentUser) => localStorage.setItem(USER_KEY, JSON.stringify(u)),
  clear: () => localStorage.removeItem(USER_KEY),
}

/** 统一 fetch 封装:自动带 Bearer token + credentials。 */
export async function apiFetch<T = any>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const headers = new Headers(options.headers || {})
  const token = userToken.get()
  if (token && !headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${token}`)
  }
  // json body 自动加 Content-Type
  const body = options.body
  if (
    body &&
    typeof body === 'object' &&
    !(body instanceof FormData) &&
    !(body instanceof Blob) &&
    !headers.has('Content-Type')
  ) {
    headers.set('Content-Type', 'application/json')
    ;(options as any).body = JSON.stringify(body)
  }
  const r = await fetch(path, {
    ...options,
    headers,
    credentials: 'include',
  })
  if (r.status === 401) {
    userToken.clear()
    currentUserStore.clear()
    // 跳登录(避免循环:若当前已在登录页则不跳)
    if (!location.hash.startsWith('#/login')) {
      const back = encodeURIComponent(
        location.hash.replace(/^#/, '') || '/',
      )
      location.hash = `#/login?from=${back}`
    }
    throw new UnauthorizedError(path)
  }
  if (!r.ok) {
    // 尝试解析后端 detail
    let detail = `${r.status} ${r.statusText}`
    try {
      const j = await r.clone().json()
      if (j?.detail) detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail)
    } catch {
      /* 非 json 错误体,保留默认 */
    }
    throw new ApiError(path, r.status, detail)
  }
  // 204 / 空体
  if (r.status === 204) return undefined as unknown as T
  const ct = r.headers.get('content-type') || ''
  if (ct.includes('application/json')) return (await r.json()) as T
  return (await r.text()) as unknown as T
}

export class ApiError extends Error {
  status: number
  detail: string
  constructor(path: string, status: number, detail: string) {
    super(`${path}: ${detail}`)
    this.status = status
    this.detail = detail
  }
}

export class UnauthorizedError extends ApiError {
  constructor(path: string) {
    super(path, 401, '未登录或会话过期')
    this.name = 'UnauthorizedError'
  }
}

/** GET */
export function apiGet<T = any>(path: string): Promise<T> {
  return apiFetch<T>(path, { method: 'GET' })
}

/** POST / PUT / PATCH / DELETE(JSON body) */
export function apiJson<T = any>(
  path: string,
  method: 'POST' | 'PUT' | 'PATCH' | 'DELETE',
  body?: any,
): Promise<T> {
  return apiFetch<T>(path, {
    method,
    body: body === undefined ? undefined : body,
  })
}

/** alias(apiJson)。 */
export const apiPost = apiJson

/** multipart 上传(file + fields)。
 *
 * 注意:不要手动设 Content-Type,浏览器自动加 multipart/form-data + boundary。
 */
export function apiUpload<T = any>(
  path: string,
  file: File,
  fields: Record<string, string>,
  method: 'POST' | 'PUT' = 'POST',
): Promise<T> {
  const fd = new FormData()
  fd.append('file', file)
  for (const [k, v] of Object.entries(fields)) fd.append(k, v)
  return apiFetch<T>(path, { method, body: fd })
}

/** 读错误 detail 字符串(给 UI 显示)。 */
export function errMsg(e: unknown, fallback = '操作失败'): string {
  if (e instanceof ApiError) return e.detail || fallback
  if (e instanceof Error) return e.message || fallback
  return fallback
}

/** 是否 401。 */
export function isUnauthorized(e: unknown): boolean {
  return e instanceof UnauthorizedError || (e instanceof ApiError && e.status === 401)
}
