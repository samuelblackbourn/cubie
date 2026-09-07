/**
 * The decision layer: office state in, device intent out. No I/O.
 *
 * Everything the bridge decides happens here, as one pure function over a
 * plain object. That is the same split as the K10's `totem_logic.h`, and for
 * the same reason: posture rules are the part most likely to be wrong, and a
 * pure function is the part cheapest to test. `bridge.ts` does the talking and
 * makes no decisions; this file decides and talks to nothing.
 *
 * ## Hardware limits are encoded here, not hoped for
 *
 * `pitch` is clamped to 5..85 because the gateway's `move_head` declares that
 * range and **rejects** anything outside it rather than clamping — a pose reset
 * of `move_head(yaw=0, pitch=0)` is refused at the MCP boundary, deliberately,
 * to avoid the servo-bus hang tracked upstream. So a bug here is not a wobbly
 * head, it is a silently dropped command.
 *
 * Yaw travel is also bounded: upstream's known-issues list says the servo bus
 * can hang on large abrupt reversals (their example is +60 -> -60). Every
 * posture in this file sits within +/-20 of centre, and a test asserts no two
 * postures are further apart than `MAX_YAW_TRAVEL`.
 */

import type { OfficeState } from './contract.ts';

/** Faces the firmware exposes, in the board's own index order. */
export type Face = 'idle' | 'happy' | 'thinking' | 'sad' | 'surprised' | 'embarrassed';

export interface Posture {
  face: Face;
  /** Degrees, -90..90. Negative is one side, positive the other. */
  yaw: number;
  /** Degrees, 5..85. Higher looks up. */
  pitch: number;
  speed: 'low' | 'mid' | 'high';
  /** 0..255 each. `[0,0,0]` is off. */
  led: readonly [number, number, number];
  /** Why this posture was chosen. Logged, and asserted in tests. */
  reason: PostureReason;
}

export type PostureReason =
  /** The office could not be read at all. */
  | 'offline'
  /** Something is waiting on Sam: an approval, or an explicit needsYou. */
  | 'attention'
  /** Board work is blocked or in review, but nothing is mid-flight. */
  | 'review'
  /** Agents are working. */
  | 'busy'
  /** Nothing is happening. */
  | 'resting';

export const PITCH_MIN = 5;
export const PITCH_MAX = 85;
export const YAW_LIMIT = 90;
/** No single move should cross more than this, per upstream's bus-hang note. */
export const MAX_YAW_TRAVEL = 60;

/** Pitch that reads as "level", chosen to match the firmware's boot pose. */
const PITCH_LEVEL = 45;

// Colours. Amber is the office's own "something is not right" signal, reused
// from the K10's beacon so the fleet says the same thing the same way.
const AMBER: readonly [number, number, number] = [255, 140, 0];
const PA_PINK: readonly [number, number, number] = [255, 102, 153];
const WORKING: readonly [number, number, number] = [0, 90, 130];
const OFF: readonly [number, number, number] = [0, 0, 0];

function clamp(value: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, value));
}

/** Coerce a posture into what the hardware will actually accept. */
export function safe(posture: Posture): Posture {
  return {
    ...posture,
    yaw: Math.round(clamp(posture.yaw, -YAW_LIMIT, YAW_LIMIT)),
    pitch: Math.round(clamp(posture.pitch, PITCH_MIN, PITCH_MAX)),
    led: [
      Math.round(clamp(posture.led[0], 0, 255)),
      Math.round(clamp(posture.led[1], 0, 255)),
      Math.round(clamp(posture.led[2], 0, 255)),
    ] as const,
  };
}

/**
 * Choose a posture for the office as it currently is.
 *
 * `null` means the office could not be read — a fetch failure, a bad status, or
 * a payload that did not parse. That is deliberately the *same* input as a
 * network outage, because from the desk they are the same thing: Cubie does not
 * know what the office is doing and should say so rather than hold a stale pose.
 *
 * Precedence is by urgency, not by field order:
 *
 *   offline > attention > review > busy > resting
 *
 * `attention` outranks `busy` because a person being needed matters more than
 * machines being busy; `review` outranks `busy` for the same reason, but sits
 * below `attention` because a blocked board item is not as loud as an approval
 * waiting on the desk right now.
 */
export function posture(state: OfficeState | null): Posture {
  if (state === null) {
    // Head tilted and off-centre: the design note asks for "visibly confused",
    // and with no roll axis a yaw offset plus a lowered gaze is what reads as a
    // tilt on this body.
    return safe({
      face: 'thinking',
      yaw: 14,
      pitch: 38,
      speed: 'low',
      led: AMBER,
      reason: 'offline',
    });
  }

  if (state.needsYou || state.pendingTotal > 0) {
    // Wakes, faces front, looks up. This is the one posture meant to catch a
    // person's eye from across a desk.
    return safe({
      face: 'surprised',
      yaw: 0,
      pitch: 62,
      speed: 'high',
      led: PA_PINK,
      reason: 'attention',
    });
  }

  if (state.founderTasksTotal > 0) {
    return safe({
      face: 'thinking',
      yaw: 0,
      pitch: 52,
      speed: 'mid',
      led: AMBER,
      reason: 'review',
    });
  }

  if (state.agentsRunning > 0 || state.mood === 'busy') {
    return safe({
      face: 'idle',
      yaw: 0,
      pitch: PITCH_LEVEL,
      speed: 'mid',
      led: WORKING,
      reason: 'busy',
    });
  }

  // Dozing. The design note asks for half-closed eyes, which the gateway does
  // not expose — eye weight is driven by the firmware's own blink machine and
  // there is no tool for it. So dozing is carried by posture instead: head
  // dipped, LEDs dark. Honest about what the surface allows rather than
  // pretending at an expression we cannot set.
  return safe({
    face: 'idle',
    yaw: 0,
    pitch: 24,
    speed: 'low',
    led: OFF,
    reason: 'resting',
  });
}

/** True when two postures differ in anything the device would need told. */
export function differs(a: Posture, b: Posture): boolean {
  return (
    a.face !== b.face ||
    a.yaw !== b.yaw ||
    a.pitch !== b.pitch ||
    a.led[0] !== b.led[0] ||
    a.led[1] !== b.led[1] ||
    a.led[2] !== b.led[2]
  );
}
