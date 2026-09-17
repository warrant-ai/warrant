/**
 * Asynchronous, batched record emission with a bounded buffer and disk spill.
 *
 * The caller only enqueues. A timer-driven pump delivers batches to a sink. If the sink
 * fails, batches are spilled to JSONL files and retried with backoff once it recovers; if
 * the buffer is full, the caller spills directly. A sink that throws `PermanentSinkError`
 * for a batch (for example a schema violation) has that batch moved to a dead-letter
 * directory instead of retried. Same files and layout as the Python SDK.
 */

import { closeSync, fsyncSync, mkdirSync, openSync, readdirSync, readFileSync, renameSync, unlinkSync, writeSync } from "node:fs";
import { join } from "node:path";

/** A transient delivery failure; the batch will be retried. */
export class SinkError extends Error {
  constructor(message, options) {
    super(message, options);
    this.name = "SinkError";
  }
}

/** A delivery failure that will not succeed on retry; the batch is dead-lettered. */
export class PermanentSinkError extends SinkError {
  constructor(message, options) {
    super(message, options);
    this.name = "PermanentSinkError";
  }
}

export class Emitter {
  constructor(sink, spillDir, { maxQueue = 10_000, batchSize = 200, flushIntervalMs = 200, backoffInitialMs = 500, backoffMaxMs = 30_000, logger = console } = {}) {
    if (!sink || typeof sink.write !== "function") throw new TypeError("sink must have a write(records) method");
    if (!(maxQueue >= 1) || !(batchSize >= 1) || !(flushIntervalMs > 0)) {
      throw new RangeError("maxQueue and batchSize must be >= 1 and flushIntervalMs > 0");
    }
    this._sink = sink;
    this._spillDir = spillDir;
    this._deadDir = join(spillDir, "dead");
    this._maxQueue = maxQueue;
    this._batchSize = batchSize;
    this._flushIntervalMs = flushIntervalMs;
    this._backoffInitialMs = backoffInitialMs;
    this._backoffMaxMs = backoffMaxMs;
    this._backoffMs = backoffInitialMs;
    this._nextRetry = 0;
    this._healthy = true;
    this._log = logger;
    this._queue = [];
    this._pending = 0;
    this._waiters = [];
    this._timer = null;
    this._running = false;
    this._closed = false;
    this._spillSeq = 0;
    this._counters = { submitted: 0, delivered: 0, spilled: 0, recovered: 0, failed_batches: 0, dead: 0 };
    mkdirSync(this._deadDir, { recursive: true });
    const backlog = this._spillFiles().length;
    if (backlog) {
      this._log.info(`warrant emitter started with ${backlog} spill file(s) to recover`);
      this._schedule(0);
    }
  }

  submit(record) {
    if (this._closed) throw new Error("warrant emitter is closed");
    this._counters.submitted += 1;
    if (this._queue.length >= this._maxQueue) {
      this._spill([record], "buffer full");
      return;
    }
    this._queue.push(record);
    this._pending += 1;
    this._schedule(this._queue.length >= this._batchSize ? 0 : this._flushIntervalMs);
  }

  /** Resolve once every submitted record is delivered or safely spilled. Resolves false on timeout. */
  flush(timeoutMs = 5000) {
    if (this._pending === 0) return Promise.resolve(true);
    this._schedule(0);
    return new Promise((resolve) => {
      const waiter = { resolve, timer: null };
      waiter.timer = setTimeout(() => {
        this._waiters = this._waiters.filter((w) => w !== waiter);
        resolve(false);
      }, timeoutMs);
      this._waiters.push(waiter);
    });
  }

  async close(timeoutMs = 5000) {
    if (this._closed) return;
    const flushed = await this.flush(timeoutMs);
    this._closed = true;
    if (this._timer) clearTimeout(this._timer);
    this._timer = null;
    if (!flushed && this._queue.length) {
      const rest = this._queue.splice(0);
      this._spill(rest, "closing before delivery");
      this._settle(rest.length);
    }
  }

  stats() {
    return { ...this._counters, pending: this._pending, queued: this._queue.length };
  }

  // -- pump -----------------------------------------------------------------

  _schedule(delayMs) {
    if (this._closed || this._running) return;
    if (this._timer) {
      if (delayMs > 0) return;
      clearTimeout(this._timer);
    }
    this._timer = setTimeout(() => {
      this._timer = null;
      void this._pump();
    }, delayMs);
    this._timer.unref?.();
  }

  async _pump() {
    if (this._running) return;
    this._running = true;
    try {
      while (this._queue.length) {
        await this._deliver(this._queue.splice(0, this._batchSize));
      }
      await this._drainSpill();
    } finally {
      this._running = false;
    }
    if (this._queue.length) {
      this._schedule(0);
    } else if (this._spillFiles().length) {
      this._schedule(Math.max(this._nextRetry - Date.now(), this._flushIntervalMs));
    }
  }

  async _deliver(batch) {
    try {
      await this._sink.write(batch);
      this._counters.delivered += batch.length;
      this._markHealthy();
    } catch (err) {
      if (err instanceof PermanentSinkError) {
        this._deadLetter(batch, err);
      } else {
        this._markUnhealthy(err);
        this._spill(batch, "sink failure");
      }
    } finally {
      this._settle(batch.length);
    }
  }

  async _drainSpill() {
    if (Date.now() < this._nextRetry) return;
    const [name] = this._spillFiles();
    if (!name) return;
    const path = join(this._spillDir, name);
    let records;
    try {
      records = readFileSync(path, "utf8").split("\n").filter((line) => line.trim()).map((line) => JSON.parse(line));
    } catch (err) {
      this._log.error(`warrant spill file ${name} unreadable, moving to dead letters: ${err.message}`);
      renameSync(path, join(this._deadDir, name));
      return;
    }
    try {
      await this._sink.write(records);
    } catch (err) {
      if (err instanceof PermanentSinkError) {
        this._log.error(`warrant spill file ${name} rejected permanently: ${err.message}`);
        renameSync(path, join(this._deadDir, name));
        this._counters.dead += records.length;
      } else {
        this._markUnhealthy(err);
      }
      return;
    }
    unlinkSync(path);
    this._counters.recovered += records.length;
    this._counters.delivered += records.length;
    this._markHealthy();
  }

  // -- helpers --------------------------------------------------------------

  _settle(count) {
    this._pending -= count;
    if (this._pending > 0) return;
    for (const waiter of this._waiters.splice(0)) {
      clearTimeout(waiter.timer);
      waiter.resolve(true);
    }
  }

  _fileName() {
    const stamp = (BigInt(Date.now()) * 1_000_000n).toString().padStart(20, "0");
    return `${stamp}-${String(this._spillSeq++).padStart(6, "0")}.jsonl`;
  }

  _writeJsonl(path, records) {
    const tmp = `${path}.tmp`;
    const fd = openSync(tmp, "w");
    try {
      writeSync(fd, records.map((r) => JSON.stringify(r)).join("\n") + "\n");
      fsyncSync(fd);
    } finally {
      closeSync(fd);
    }
    renameSync(tmp, path);
  }

  _spill(records, reason) {
    mkdirSync(this._spillDir, { recursive: true });
    this._writeJsonl(join(this._spillDir, this._fileName()), records);
    this._counters.spilled += records.length;
    this._log.warn(`warrant spilled ${records.length} record(s) to disk (${reason})`);
  }

  _deadLetter(batch, err) {
    const name = this._fileName();
    this._writeJsonl(join(this._deadDir, name), batch);
    this._counters.dead += batch.length;
    this._log.error(`warrant dead-lettered ${batch.length} record(s) to ${name}: ${err.message}`);
  }

  _markUnhealthy(err) {
    this._counters.failed_batches += 1;
    if (this._healthy) this._log.error(`warrant sink failing, retrying with backoff: ${err?.message ?? err}`);
    this._healthy = false;
    this._nextRetry = Date.now() + this._backoffMs;
    this._backoffMs = Math.min(this._backoffMs * 2, this._backoffMaxMs);
  }

  _markHealthy() {
    if (!this._healthy) this._log.info("warrant sink recovered");
    this._healthy = true;
    this._backoffMs = this._backoffInitialMs;
    this._nextRetry = 0;
  }

  _spillFiles() {
    try {
      return readdirSync(this._spillDir).filter((name) => name.endsWith(".jsonl")).sort();
    } catch (err) {
      if (err.code === "ENOENT") return [];
      throw err;
    }
  }
}
