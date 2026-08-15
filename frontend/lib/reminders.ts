import { existsSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { safeReminderFrom } from './reminder-client';

/**
 * A scheduled reminder exactly as stored by the backend
 * (`backend/src/reminders.py`) in its JSON mirror. Includes fields that
 * must NEVER be exposed to the UI (destination, claim_id).
 */
export interface ReminderMirrorRecord {
  id: number;
  reference_id: string;
  destination: string;
  message: string;
  scheduled_at: string;
  status: string;
  created_at: string;
  triggered_at?: string | null;
  claim_id?: string | null;
}

/** The reminder statuses supported by the backend store. */
export const REMINDER_STATUSES = [
  'pending',
  'triggered',
  'completed',
  'failed',
  'cancelled',
] as const;

export type ReminderStatus = (typeof REMINDER_STATUSES)[number];

/**
 * The ONLY reminder fields safe to show a caller. Explicitly excludes the
 * destination (phone number / SIP user / SIP URI) and the internal
 * claim_id; conversation content is already scrubbed by the backend
 * store before it is written to the mirror.
 */
export interface SafeReminder {
  reference_id: string;
  status: string;
  scheduled_at: string;
  message: string | null;
  created_at: string;
}

/** Map a mirror row to its safe projection; unknown/absent safe fields are dropped. */
export function toSafeReminder(row: ReminderMirrorRecord): SafeReminder {
  return {
    reference_id: row.reference_id,
    status: row.status,
    scheduled_at: row.scheduled_at,
    message: typeof row.message === 'string' ? row.message : null,
    created_at: row.created_at,
  };
}

/**
 * Look up a reminder by reference ID (case-insensitive, exact match).
 * Returns the safe projection or null when unknown/empty.
 */
export function findReminderByReference(
  records: SafeReminder[],
  referenceId: string | null | undefined
): SafeReminder | null {
  const needle = (referenceId ?? '').trim().toUpperCase();
  if (!needle) {
    return null;
  }
  return records.find((record) => record.reference_id.toUpperCase() === needle) ?? null;
}

const CANDIDATE_PATHS = [
  process.env.REMIN_JSON_PATH,
  // Running from frontend/ (the normal development layout)
  path.resolve(process.cwd(), '..', 'backend', 'data', 'reminders.json'),
  // Running from the repository root
  path.resolve(process.cwd(), 'backend', 'data', 'reminders.json'),
].filter((entry): entry is string => Boolean(entry));

/**
 * Read the reminder mirror (newest first) and return ONLY safe fields.
 * Never throws; empty list on failure. `paths` is overridable for tests.
 */
export async function getReminders(paths: string[] = CANDIDATE_PATHS): Promise<SafeReminder[]> {
  for (const candidate of paths) {
    if (!existsSync(candidate)) {
      continue;
    }
    try {
      const raw = await readFile(candidate, 'utf-8');
      const parsed: unknown = JSON.parse(raw);
      if (Array.isArray(parsed)) {
        return parsed
          .map(safeReminderFrom)
          .filter((record): record is SafeReminder => record !== null);
      }
    } catch {
      // Unreadable or malformed mirror: try the next candidate.
    }
  }
  return [];
}
