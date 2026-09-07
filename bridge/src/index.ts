/**
 * Entry point. Reads configuration from the environment and runs the loop.
 *
 * Config comes from the environment rather than a file so the systemd unit can
 * carry it the way every other service on this box does — see
 * `config.example.env` at the repo root.
 */

import { Bridge } from './bridge.ts';
import { DeviceClient } from './deviceClient.ts';
import { OfficeClient } from './officeClient.ts';

function required(name: string): string {
  const value = process.env[name];
  if (!value) {
    // Fail loudly at startup rather than running with a broken half: a bridge
    // that polls an unreachable office forever looks identical to a bridge
    // whose token is missing, and only one of those is fixable by waiting.
    console.error(`${name} is not set`);
    process.exit(1);
  }
  return value;
}

// `OFFICE_HUB` is the name the repo's config.example.env already uses, rather
// than a second one meaning the same thing. Defaults to loopback because the
// bridge has to run on the gateway's host anyway (see below), and that host is
// office-server, which is also the office.
const office = new OfficeClient({
  baseUrl: process.env.OFFICE_HUB ?? 'http://127.0.0.1:8420',
  token: required('AGENTHUB_COMPANION_TOKEN'),
});

// Note this is NOT `STACKCHAN_GATEWAY_HOST:PORT` from config.example.env: that
// pair is 8765, the WebSocket the *device* dials in on. This is 8767, the
// gateway's MCP control surface, bound to loopback on purpose — which is why
// the bridge must be co-located with the gateway.
const device = new DeviceClient({
  url: process.env.CUBIE_GATEWAY_MCP_URL ?? 'http://127.0.0.1:8767/mcp',
  token: required('STACKCHAN_TOKEN'),
});

const bridge = new Bridge({
  office,
  device,
  pollMs: Number(process.env.CUBIE_POLL_MS ?? 5000),
});

for (const signal of ['SIGINT', 'SIGTERM'] as const) {
  process.on(signal, () => {
    bridge.stop();
    process.exit(0);
  });
}

await bridge.run();
