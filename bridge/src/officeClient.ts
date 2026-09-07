/**
 * Reading the office. One GET, one parse, no retries.
 *
 * Retrying belongs to the loop, not here: the bridge polls on a fixed interval
 * anyway, so a failed read is just this tick's answer being "unknown". A retry
 * inside the fetch would only delay that answer and blur the poll cadence.
 */

import { parseOfficeState, type OfficeState } from './contract.ts';

export interface OfficeClientOptions {
  /** e.g. `http://127.0.0.1:8420` */
  baseUrl: string;
  /** `AGENTHUB_COMPANION_TOKEN`. */
  token: string;
  timeoutMs?: number;
}

export class OfficeClient {
  constructor(private readonly options: OfficeClientOptions) {}

  /**
   * The office's current state, or null if it could not be read.
   *
   * Null covers every failure the same way — unreachable, 401, 503, malformed
   * body — because they all mean the same thing to a robot on a desk: it does
   * not know what the office is doing. `reason` is logged by the caller so the
   * distinction survives for a human without leaking into the posture rules.
   */
  async read(): Promise<{ state: OfficeState | null; reason: string }> {
    const url = `${this.options.baseUrl}/api/companion/status`;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.options.timeoutMs ?? 4000);
    try {
      const res = await fetch(url, {
        headers: { authorization: `Bearer ${this.options.token}` },
        signal: controller.signal,
      });
      if (!res.ok) {
        return { state: null, reason: `http ${res.status}` };
      }
      const state = parseOfficeState(await res.json());
      return state === null
        ? { state: null, reason: 'payload did not match the contract' }
        : { state, reason: 'ok' };
    } catch (err) {
      return { state: null, reason: err instanceof Error ? err.message : String(err) };
    } finally {
      clearTimeout(timer);
    }
  }
}
