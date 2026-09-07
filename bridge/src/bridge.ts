/**
 * The loop. Poll, decide, apply — and nothing else.
 *
 * Every decision lives in `posture.ts`; this file only moves bytes and handles
 * failure. Keeping it that way is what makes the interesting behaviour testable
 * without a robot on the desk.
 */

import type { DeviceClient } from './deviceClient.ts';
import type { OfficeClient } from './officeClient.ts';
import { differs, posture, type Posture } from './posture.ts';

export interface BridgeOptions {
  office: OfficeClient;
  device: DeviceClient;
  /** How often to read the office. The office caps its own cost, not us. */
  pollMs?: number;
  log?: (line: string) => void;
}

export class Bridge {
  private last: Posture | null = null;
  private stopped = false;

  constructor(private readonly options: BridgeOptions) {}

  private log(line: string): void {
    (this.options.log ?? ((l: string) => console.log(l)))(line);
  }

  /**
   * One cycle. Returns the posture that is now showing, or null if the device
   * could not be driven at all.
   *
   * Note what is *not* here: any decision about what the office state means.
   * This reads, calls `posture()`, and applies the answer if it changed.
   */
  async tick(): Promise<Posture | null> {
    const { state, reason } = await this.options.office.read();
    if (state === null) {
      this.log(`office unreadable: ${reason}`);
    }

    const next = posture(state);

    // Re-send when the previous cycle failed, even if the posture is identical:
    // `last` records what we last *successfully* applied, so a failure leaves it
    // untouched and the next tick will retry rather than deciding there is
    // nothing to do.
    if (this.last !== null && !differs(this.last, next)) {
      return this.last;
    }

    try {
      await this.options.device.apply(next);
      if (this.last === null || this.last.reason !== next.reason) {
        this.log(
          `posture ${this.last?.reason ?? 'none'} -> ${next.reason} ` +
            `(face=${next.face} yaw=${next.yaw} pitch=${next.pitch} ` +
            `led=${next.led.join(',')})`,
        );
      }
      this.last = next;
      return next;
    } catch (err) {
      // A gateway that has gone away leaves a dead session behind, so drop it
      // and let the next tick reconnect. Do not update `last`: nothing was
      // applied, and pretending otherwise would strand the wrong posture.
      this.log(`device apply failed: ${err instanceof Error ? err.message : String(err)}`);
      this.options.device.reset();
      return null;
    }
  }

  async run(): Promise<void> {
    const pollMs = this.options.pollMs ?? 5000;
    this.log(`bridge started, polling every ${pollMs} ms`);
    while (!this.stopped) {
      await this.tick();
      await new Promise((resolve) => setTimeout(resolve, pollMs));
    }
  }

  stop(): void {
    this.stopped = true;
  }
}
