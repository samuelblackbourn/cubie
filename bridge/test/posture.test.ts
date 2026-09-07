import { describe, expect, it } from 'vitest';

import type { OfficeState } from '../src/contract.ts';
import { parseOfficeState } from '../src/contract.ts';
import {
  differs,
  MAX_YAW_TRAVEL,
  PITCH_MAX,
  PITCH_MIN,
  posture,
  type PostureReason,
} from '../src/posture.ts';

/** A quiet office. Overridden per test to isolate one signal at a time. */
function office(over: Partial<OfficeState> = {}): OfficeState {
  return {
    pendingApprovals: [],
    pendingTotal: 0,
    agentsRunning: 0,
    needsYou: false,
    founderTasks: [],
    founderTasksTotal: 0,
    mood: 'calm',
    ts: 1787173749556,
    ...over,
  };
}

const ALL_REASONS: PostureReason[] = ['offline', 'attention', 'review', 'busy', 'resting'];

function postureFor(reason: PostureReason) {
  switch (reason) {
    case 'offline':
      return posture(null);
    case 'attention':
      return posture(office({ needsYou: true }));
    case 'review':
      return posture(office({ founderTasksTotal: 2 }));
    case 'busy':
      return posture(office({ agentsRunning: 1 }));
    case 'resting':
      return posture(office());
  }
}

describe('posture selection', () => {
  it('is confused when the office cannot be read', () => {
    // null is every failure at once -- unreachable, 401, malformed. From the
    // desk they are the same fact: Cubie does not know what the office is doing.
    const p = posture(null);
    expect(p.reason).toBe('offline');
    expect(p.led).toEqual([255, 140, 0]);
    expect(p.yaw).not.toBe(0);
  });

  it('wakes and faces front when someone is needed', () => {
    for (const state of [office({ needsYou: true }), office({ pendingTotal: 1 })]) {
      const p = posture(state);
      expect(p.reason).toBe('attention');
      expect(p.yaw).toBe(0);
      expect(p.face).toBe('surprised');
    }
  });

  it('ranks a person being needed above machines being busy', () => {
    // Both signals present. The one that involves a human wins.
    const p = posture(office({ needsYou: true, agentsRunning: 4, mood: 'busy' }));
    expect(p.reason).toBe('attention');
  });

  it('ranks board review above busy, and below attention', () => {
    expect(posture(office({ founderTasksTotal: 1, agentsRunning: 3 })).reason).toBe('review');
    expect(posture(office({ founderTasksTotal: 1, pendingTotal: 1 })).reason).toBe('attention');
  });

  it('is busy on either agents running or the office saying so', () => {
    expect(posture(office({ agentsRunning: 1 })).reason).toBe('busy');
    expect(posture(office({ mood: 'busy' })).reason).toBe('busy');
  });

  it('dozes with the head dipped and lights off when nothing is happening', () => {
    const p = posture(office());
    expect(p.reason).toBe('resting');
    expect(p.led).toEqual([0, 0, 0]);
    // Dozing is carried by posture because the gateway exposes no eye-weight
    // tool -- so the head must actually be lower than any working posture.
    expect(p.pitch).toBeLessThan(posture(office({ agentsRunning: 1 })).pitch);
  });
});

describe('hardware limits', () => {
  it('never emits a pitch the gateway would reject', () => {
    // move_head declares pitch 5..85 and REJECTS outside it rather than
    // clamping -- a pose reset of pitch=0 is refused at the MCP boundary to
    // avoid the servo-bus hang. So an out-of-range value here is not a wobbly
    // head, it is a silently dropped command.
    for (const reason of ALL_REASONS) {
      const p = postureFor(reason);
      expect(p.pitch).toBeGreaterThanOrEqual(PITCH_MIN);
      expect(p.pitch).toBeLessThanOrEqual(PITCH_MAX);
      expect(p.pitch).not.toBe(0);
    }
  });

  it('never asks for a yaw sweep that could hang the servo bus', () => {
    // Upstream's known issues: the bus can hang on large abrupt reversals,
    // their example being +60 -> -60. Any two postures may follow each other.
    for (const a of ALL_REASONS) {
      for (const b of ALL_REASONS) {
        const travel = Math.abs(postureFor(a).yaw - postureFor(b).yaw);
        expect(travel).toBeLessThanOrEqual(MAX_YAW_TRAVEL);
      }
    }
  });

  it('keeps every LED channel inside 0..255', () => {
    for (const reason of ALL_REASONS) {
      for (const channel of postureFor(reason).led) {
        expect(channel).toBeGreaterThanOrEqual(0);
        expect(channel).toBeLessThanOrEqual(255);
        expect(Number.isInteger(channel)).toBe(true);
      }
    }
  });

  it('gives every reason a distinguishable posture', () => {
    // Two states that mean different things must not look identical, or the
    // robot is not communicating anything.
    for (const a of ALL_REASONS) {
      for (const b of ALL_REASONS) {
        if (a === b) continue;
        expect(differs(postureFor(a), postureFor(b))).toBe(true);
      }
    }
  });
});

describe('parsing the office payload', () => {
  const live = {
    pendingApprovals: [],
    pendingTotal: 0,
    agentsRunning: 0,
    needsYou: false,
    founderTasks: [],
    founderTasksTotal: 0,
    mood: 'calm',
    ts: 1787173749556,
  };

  it('accepts the payload the office actually serves', () => {
    expect(parseOfficeState(live)).not.toBeNull();
  });

  it('tolerates fields it does not read', () => {
    // The payload has grown twice already. A bridge that refused to run over a
    // field it never looks at would stop reflecting the office for no reason.
    expect(parseOfficeState({ ...live, somethingNew: { nested: true } })).not.toBeNull();
  });

  it('treats a missing present as unknown, not as absent', () => {
    // `undefined` and `false` mean different things: nothing reporting versus
    // something looked and found nobody. Collapsing them would make a dead
    // sensor indistinguishable from an empty room.
    const parsed = parseOfficeState(live);
    expect(parsed).not.toBeNull();
    expect(Object.hasOwn(parsed!, 'present')).toBe(false);

    const withPresence = parseOfficeState({ ...live, present: false });
    expect(withPresence?.present).toBe(false);
  });

  it('rejects drift in a field it does read, which shows as offline', () => {
    for (const bad of [
      { ...live, mood: 'frantic' },
      { ...live, needsYou: 'yes' },
      { ...live, pendingTotal: '0' },
      { ...live, founderTasks: 'none' },
    ]) {
      expect(parseOfficeState(bad)).toBeNull();
      // And that null must reach the confused posture, not a wrong face.
      expect(posture(parseOfficeState(bad)).reason).toBe('offline');
    }
  });
});
