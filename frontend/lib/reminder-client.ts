import type { SafeReminder } from './reminders';

/**
 * Client-safe reminder actions. This module must NEVER import node:fs or
 * any server-only module — it is imported by client components.
 */

/**
 * Validate an unknown JSON value and project it to a SafeReminder, or
 * return null when it is not a reminder-shaped object. Used as
 * defense-in-depth on API responses so fields beyond the safe set
 * (destination, claim_id, credentials) can never reach the UI even if a
 * future endpoint returns extra fields. Kept in this client-safe module
 * so it can be imported from both browser and server code.
 */
export function safeReminderFrom(value: unknown): SafeReminder | null {
  if (!value || typeof value !== 'object') {
    return null;
  }
  const row = value as Record<string, unknown>;
  if (typeof row.reference_id !== 'string' || typeof row.status !== 'string') {
    return null;
  }
  return {
    reference_id: row.reference_id,
    status: row.status,
    scheduled_at: typeof row.scheduled_at === 'string' ? row.scheduled_at : '',
    message: typeof row.message === 'string' ? row.message : null,
    created_at: typeof row.created_at === 'string' ? row.created_at : '',
  };
}

export interface ReminderLookupResult {
  found: boolean;
  reminder?: SafeReminder;
}

/** GET the latest status for a reference ID from the existing mirror. */
export async function fetchReminderStatus(referenceId: string): Promise<ReminderLookupResult> {
  const needle = referenceId.trim();
  if (!needle) {
    return { found: false };
  }
  let response: Response;
  try {
    response = await fetch(`/api/reminders?ref=${encodeURIComponent(needle)}`, {
      cache: 'no-store',
    });
  } catch {
    return { found: false };
  }
  if (!response.ok) {
    return { found: false };
  }
  const data: unknown = await response.json().catch(() => null);
  if (!data || typeof data !== 'object') {
    return { found: false };
  }
  const row = data as Record<string, unknown>;
  if (row.found !== true) {
    return { found: false };
  }
  const reminder = safeReminderFrom(row.reminder);
  if (!reminder) {
    return { found: false };
  }
  return { found: true, reminder };
}

export interface CancelReminderResult {
  ok: boolean;
  error?: string;
}

/** POST a cancel request; the backend only cancels still-pending reminders. */
export async function cancelReminder(referenceId: string): Promise<CancelReminderResult> {
  let response: Response;
  try {
    response = await fetch('/api/reminders/cancel', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reference_id: referenceId }),
    });
  } catch {
    return { ok: false, error: 'Could not reach the reminder service.' };
  }
  const data: unknown = await response.json().catch(() => null);
  if (
    response.ok &&
    data &&
    typeof data === 'object' &&
    (data as Record<string, unknown>).ok === true
  ) {
    return { ok: true };
  }
  const error =
    data && typeof data === 'object' && typeof (data as Record<string, unknown>).error === 'string'
      ? String((data as Record<string, unknown>).error)
      : 'The reminder could not be cancelled.';
  return { ok: false, error };
}
