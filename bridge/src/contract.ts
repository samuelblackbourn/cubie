/**
 * The office's companion payload, as this bridge understands it.
 *
 * These types mirror `contract/companion-status.json`, which is checked against
 * Virtual-Office's `companionStatus.ts` by `scripts/check_companion_contract.py`.
 * If that guard fails, these types are the thing to fix — they are downstream of
 * the contract, not a second source of truth.
 */

export type Mood = 'calm' | 'busy' | 'attention';
export type FounderTaskStatus = 'blocked' | 'review';

export interface PendingApproval {
  id: string;
  oneLine: string;
}

export interface FounderTask {
  id: string;
  title: string;
  status: FounderTaskStatus;
}

export interface OfficeState {
  /**
   * Desk presence. **Absent when unknown**, `false` only when something looked
   * and found nobody. Collapsing the two would make a dead sensor
   * indistinguishable from an empty room — the office is careful about this and
   * so is the bridge. Nothing reports it until firmware ladder Rung 4.
   */
  present?: boolean;
  pendingApprovals: PendingApproval[];
  pendingTotal: number;
  agentsRunning: number;
  needsYou: boolean;
  founderTasks: FounderTask[];
  founderTasksTotal: number;
  mood: Mood;
  ts: number;
}

const MOODS: readonly string[] = ['calm', 'busy', 'attention'];

/**
 * Narrow an unknown body to `OfficeState`, or return null.
 *
 * Deliberately tolerant about *extra* fields and strict about the ones we use:
 * the office may grow the payload (it already has, twice), and a bridge that
 * refused to run because of a field it does not read would be a totem that
 * stops reflecting the office for no reason. But a field we *do* read arriving
 * with the wrong type is drift, and drift should surface as "office
 * unreachable" — the confused posture — rather than as a wrong face.
 */
export function parseOfficeState(body: unknown): OfficeState | null {
  if (typeof body !== 'object' || body === null) return null;
  const b = body as Record<string, unknown>;

  const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);

  const pendingTotal = num(b.pendingTotal);
  const agentsRunning = num(b.agentsRunning);
  const founderTasksTotal = num(b.founderTasksTotal);
  const ts = num(b.ts);
  if (pendingTotal === null || agentsRunning === null || founderTasksTotal === null || ts === null) {
    return null;
  }
  if (typeof b.needsYou !== 'boolean') return null;
  if (typeof b.mood !== 'string' || !MOODS.includes(b.mood)) return null;
  if (!Array.isArray(b.pendingApprovals) || !Array.isArray(b.founderTasks)) return null;

  const state: OfficeState = {
    pendingApprovals: b.pendingApprovals as PendingApproval[],
    pendingTotal,
    agentsRunning,
    needsYou: b.needsYou,
    founderTasks: b.founderTasks as FounderTask[],
    founderTasksTotal,
    mood: b.mood as Mood,
    ts,
  };
  // Only set `present` when the office actually sent a boolean: `exactOptional`
  // means `{present: undefined}` and `{}` are different types, and the second
  // is the one that means "unknown".
  if (typeof b.present === 'boolean') state.present = b.present;
  return state;
}
