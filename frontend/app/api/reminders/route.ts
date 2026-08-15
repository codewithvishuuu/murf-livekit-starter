import { NextResponse } from 'next/server';
import { findReminderByReference, getReminders } from '@/lib/reminders';

export const dynamic = 'force-dynamic';

/**
 * Read-only reminder status lookup (Scheduled Reminder page).
 *
 * GET /api/reminders?ref=REM-YYYYMMDD-NNN
 *
 * Reads the backend's existing JSON mirror (the store's own output — no
 * second database) and returns ONLY safe fields: reference_id, status,
 * scheduled_at, message, created_at. Destinations (phone number / SIP
 * user / SIP URI), claim_id and credentials are stripped inside
 * lib/reminders.ts and can never appear in this response.
 */
export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const referenceId = searchParams.get('ref') ?? '';

  if (!referenceId.trim()) {
    return NextResponse.json({ found: false });
  }

  const reminders = await getReminders();
  const reminder = findReminderByReference(reminders, referenceId);
  if (!reminder) {
    return NextResponse.json({ found: false });
  }
  return NextResponse.json({ found: true, reminder });
}
