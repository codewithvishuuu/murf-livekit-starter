import { SupportLookup } from '@/components/app/support-lookup';
import { getEscalations } from '@/lib/escalations';

export const revalidate = 0;
export const dynamic = 'force-dynamic';

export default async function SupportPage() {
  const escalations = await getEscalations();

  // Privacy: pass ONLY the minimum a caller needs to identify their request
  // and its status — never summaries, follow-up notes, language, or caller
  // information. The full queue stays on the admin page. urgency and
  // preferred_follow_up are non-sensitive metadata (no conversation
  // content), and resolved_callback_at is surfaced only as a "last updated"
  // timestamp for resolved requests.
  const records = escalations.map(
    ({ reference_id, status, created_at, urgency, preferred_follow_up, resolved_callback_at }) => ({
      reference_id,
      status,
      created_at,
      urgency: urgency ?? null,
      preferred_follow_up: preferred_follow_up ?? null,
      last_updated: resolved_callback_at ?? null,
    })
  );

  return <SupportLookup records={records} />;
}
