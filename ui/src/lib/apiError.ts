/** The one error type every `api` call rejects with — a non-2xx status plus
 * the backend's `detail`. Lives outside api.ts so the demo adapter can throw
 * the same class without importing the module that selects it (no cycle). */
export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}
