import { ReminderStatusLookup } from '@/components/app/reminders/reminder-status';
import { getReminders } from '@/lib/reminders';

export const revalidate = 0;
export const dynamic = 'force-dynamic';

export default async function RemindersPage() {
  // Privacy: getReminders() already projects the mirror to SAFE fields
  // only — reference ID, status, times and the (backend-scrubbed) message.
  // Destinations (phone number / SIP user / SIP URI) and internal claim
  // IDs never leave lib/reminders.ts.
  const reminders = await getReminders();

  return <ReminderStatusLookup reminders={reminders} />;
}
