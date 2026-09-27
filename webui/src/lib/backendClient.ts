export type JsonValidator<T> = (payload: unknown) => payload is T

export type BackendRequestErrorKind =
  | 'configuration'
  | 'request'
  | 'http'
  | 'invalid_json'
  | 'invalid_response'

export class BackendRequestError extends Error {
  readonly kind: BackendRequestErrorKind
  readonly method: string
  readonly path: string
  readonly status: number | null
  readonly responseBody: string | null

  constructor(
    kind: BackendRequestErrorKind,
    method: string,
    path: string,
    status: number | null,
    responseBody: string | null,
    message: string,
  ) {
    super(message)
    this.name = 'BackendRequestError'
    this.kind = kind
    this.method = method
    this.path = path
    this.status = status
    this.responseBody = responseBody
  }
}

const assertApiPath = (path: string, method: string): void => {
  if (!/^\/api\/v[23](?:\/|$)/.test(path)) {
    throw new BackendRequestError(
      'configuration',
      method,
      path,
      null,
      null,
      `Backend requests must use a same-origin /api/v2 or /api/v3 path: ${path}`,
    )
  }
}

export const isJsonObject = (payload: unknown): payload is Record<string, unknown> =>
  typeof payload === 'object' && payload !== null && !Array.isArray(payload)

export async function requestJson<T>(
  path: string,
  init: RequestInit,
  validate: JsonValidator<T>,
): Promise<T> {
  const method = init.method?.toUpperCase() ?? ''
  if (!method) {
    throw new BackendRequestError(
      'configuration',
      '(missing)',
      path,
      null,
      null,
      `A request method is required for ${path}`,
    )
  }
  assertApiPath(path, method)

  const headers = new Headers(init.headers)
  if (!headers.has('Accept')) headers.set('Accept', 'application/json')

  let response: Response
  try {
    response = await fetch(path, { ...init, headers, cache: 'no-store' })
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error)
    throw new BackendRequestError(
      'request',
      method,
      path,
      null,
      null,
      `${method} ${path} could not reach the backend: ${detail}`,
    )
  }

  let responseBody: string
  try {
    responseBody = await response.text()
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error)
    throw new BackendRequestError(
      'request',
      method,
      path,
      response.status,
      null,
      `${method} ${path} could not read the HTTP ${response.status} response: ${detail}`,
    )
  }

  if (!response.ok) {
    throw new BackendRequestError(
      'http',
      method,
      path,
      response.status,
      responseBody,
      `${method} ${path} failed with HTTP ${response.status}: ${responseBody || '(empty response body)'}`,
    )
  }

  let payload: unknown
  try {
    payload = JSON.parse(responseBody) as unknown
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error)
    throw new BackendRequestError(
      'invalid_json',
      method,
      path,
      response.status,
      responseBody,
      `${method} ${path} returned invalid JSON: ${detail}`,
    )
  }

  if (!validate(payload)) {
    throw new BackendRequestError(
      'invalid_response',
      method,
      path,
      response.status,
      responseBody,
      `${method} ${path} returned a JSON response outside the expected contract: ${responseBody}`,
    )
  }
  return payload
}

export async function postJson<TBody, TResponse>(
  path: string,
  body: TBody,
  validate: JsonValidator<TResponse>,
): Promise<TResponse> {
  let serializedBody: string | undefined
  try {
    serializedBody = JSON.stringify(body)
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error)
    throw new BackendRequestError(
      'configuration',
      'POST',
      path,
      null,
      null,
      `POST ${path} request body could not be serialized as JSON: ${detail}`,
    )
  }
  if (typeof serializedBody !== 'string') {
    throw new BackendRequestError(
      'configuration',
      'POST',
      path,
      null,
      null,
      `POST ${path} request body must serialize to JSON text.`,
    )
  }

  return requestJson(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: serializedBody,
  }, validate)
}
