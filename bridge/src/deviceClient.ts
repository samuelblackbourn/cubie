/**
 * Driving Cubie, through the stackchan-mcp gateway.
 *
 * The gateway owns the WebSocket to the robot, the reconnect backoff and the
 * tool surface; this is an MCP client of it. That was the S0 decision and it is
 * why this file is small — the fiddly parts are somebody else's maintained code.
 *
 * The gateway listens on loopback only (`MCP_HTTP_HOST=127.0.0.1`), so the
 * bridge must run on the same host. That is deliberate: the device-facing ports
 * are the ones exposed to the LAN, the control surface is not.
 */

import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';

import type { Posture } from './posture.ts';

export interface DeviceClientOptions {
  /** e.g. `http://127.0.0.1:8767/mcp` */
  url: string;
  /** `STACKCHAN_TOKEN`. Distinct from the office's companion token by design. */
  token: string;
}

export class DeviceClient {
  private client: Client | null = null;

  constructor(private readonly options: DeviceClientOptions) {}

  private async connected(): Promise<Client> {
    if (this.client) return this.client;
    const transport = new StreamableHTTPClientTransport(new URL(this.options.url), {
      requestInit: { headers: { authorization: `Bearer ${this.options.token}` } },
    });
    const client = new Client({ name: 'cubie-bridge', version: '0.1.0' }, { capabilities: {} });
    // The SDK's `Transport` declares `sessionId: string` while its own
    // StreamableHTTP transport supplies `string | undefined`, which
    // `exactOptionalPropertyTypes` rightly objects to. Keeping that flag is
    // worth more than accommodating the mismatch: it is what enforces the
    // `present?` distinction in contract.ts, where "absent" and "false" mean
    // genuinely different things. So the gap is localised here rather than
    // relaxed project-wide — and `ts-expect-error` errors if it becomes
    // unnecessary, so this disappears the moment upstream fixes it.
    // @ts-expect-error -- SDK transport is not exactOptional-clean
    await client.connect(transport);
    this.client = client;
    return client;
  }

  /** Drop the session so the next call reconnects. */
  reset(): void {
    this.client = null;
  }

  /** True when the gateway reports the robot is actually on the other end. */
  async esp32Connected(): Promise<boolean> {
    const client = await this.connected();
    const res = await client.callTool({ name: 'get_status', arguments: {} });
    const text = JSON.stringify(res.content ?? '');
    return text.includes('"esp32_connected": true') || text.includes('"esp32_connected":true');
  }

  /**
   * Apply a posture.
   *
   * Order matters: face first, then head, then LEDs. The face is the fastest to
   * change and the most likely to be noticed, and putting the servo move second
   * means a failed move still leaves the expression correct rather than leaving
   * a stale face on a head that already turned.
   */
  async apply(posture: Posture): Promise<void> {
    const client = await this.connected();
    await client.callTool({ name: 'set_avatar', arguments: { face: posture.face } });
    await client.callTool({
      name: 'move_head',
      arguments: { yaw: posture.yaw, pitch: posture.pitch, speed: posture.speed },
    });
    await client.callTool({
      name: 'set_all_leds',
      arguments: { r: posture.led[0], g: posture.led[1], b: posture.led[2] },
    });
  }
}
