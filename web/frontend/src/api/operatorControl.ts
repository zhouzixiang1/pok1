// Operator credentials are deliberately process-memory only.  They are never
// written to localStorage/sessionStorage, URLs, logs, or application state
// returned by the backend.  A full page reload clears the value.
let controlToken = "";

export function getOperatorControlToken(): string {
  return controlToken;
}

export function setOperatorControlToken(value: string): void {
  controlToken = value.trim();
}

export function withOperatorControlHeader(
  headers: Record<string, string> = {},
): Record<string, string> {
  const token = getOperatorControlToken();
  return token ? { ...headers, "X-Control-Token": token } : headers;
}
