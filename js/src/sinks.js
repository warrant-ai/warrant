/** Sinks that deliver record batches to a Warrant collector. */

import { gzipSync } from "node:zlib";
import { PermanentSinkError, SinkError } from "./emit.js";

const LOCAL = /^http:\/\/(localhost|127\.|\[::1\])/;

async function errorDetail(response) {
  let text;
  try {
    text = await response.text();
  } catch {
    return response.statusText;
  }
  try {
    const data = JSON.parse(text);
    let detail = String(data.detail ?? data.error ?? text);
    if (Array.isArray(data.problems) && data.problems[0] && typeof data.problems[0] === "object") {
      detail += `; first: ${data.problems[0].error}`;
    }
    return detail.slice(0, 300);
  } catch {
    return text.slice(0, 300);
  }
}

/**
 * POST batches to a Warrant collector. 5xx and connection errors are transient (the
 * emitter spills and retries); 4xx responses other than 408 and 429 are permanent.
 */
export class HttpSink {
  constructor(url, token, { timeoutMs = 10_000, compress = true, userAgent = "warrantai-js", logger = console, fetch: fetchImpl = globalThis.fetch } = {}) {
    if (typeof url !== "string" || !/^https?:\/\//.test(url)) throw new TypeError("collector url must start with http:// or https://");
    this.url = `${url.replace(/\/+$/, "")}/v1/records`;
    this._token = token ?? process.env.WARRANT_TOKEN;
    this._timeoutMs = timeoutMs;
    this._compress = compress;
    this._userAgent = userAgent;
    this._fetch = fetchImpl;
    if (url.startsWith("http://") && !LOCAL.test(url)) {
      logger.warn(`warrant: collector url ${new URL(url).origin} is not https; records will travel in clear`);
    }
  }

  async write(records) {
    let body = Buffer.from(JSON.stringify({ records }), "utf8");
    const headers = { "content-type": "application/json", "user-agent": this._userAgent };
    if (this._compress && body.length > 1024) {
      body = gzipSync(body);
      headers["content-encoding"] = "gzip";
    }
    if (this._token) headers.authorization = `Bearer ${this._token}`;
    // A cleared timer rather than AbortSignal.timeout(): on Node 18 that one keeps the process alive until it fires.
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), this._timeoutMs);
    let response;
    try {
      response = await this._fetch(this.url, { method: "POST", headers, body, signal: abort.signal });
    } catch (err) {
      throw new SinkError(`collector unreachable: ${err?.cause?.code ?? err?.name ?? "error"}`, { cause: err });
    } finally {
      clearTimeout(timer);
    }
    if (response.status === 200 || response.status === 202) return;
    const detail = await errorDetail(response);
    if (response.status >= 400 && response.status < 500 && response.status !== 408 && response.status !== 429) {
      throw new PermanentSinkError(`collector rejected batch (${response.status}): ${detail}`);
    }
    throw new SinkError(`collector error ${response.status}: ${detail}`);
  }
}
