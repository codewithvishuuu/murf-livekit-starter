import Link from 'next/link';
import { HeartPulseIcon, InboxIcon } from 'lucide-react';
import type { EscalationRecord } from '@/lib/escalations';
import { getEscalations } from '@/lib/escalations';
import { cn } from '@/lib/shadcn/utils';

export const revalidate = 0;
export const dynamic = 'force-dynamic';

const URGENCY_STYLES: Record<string, string> = {
  low: 'bg-emerald-500/10 text-emerald-700 ring-emerald-500/25 dark:text-emerald-300',
  medium: 'bg-amber-500/10 text-amber-700 ring-amber-500/25 dark:text-amber-300',
  high: 'bg-orange-500/10 text-orange-700 ring-orange-500/25 dark:text-orange-300',
  emergency: 'bg-rose-500/10 text-rose-700 ring-rose-500/25 dark:text-rose-300',
};

const STATUS_STYLES: Record<string, string> = {
  open: 'bg-amber-500/10 text-amber-700 ring-amber-500/25 dark:text-amber-300',
  in_progress: 'bg-sky-500/10 text-sky-700 ring-sky-500/25 dark:text-sky-300',
  resolved: 'bg-emerald-500/10 text-emerald-700 ring-emerald-500/25 dark:text-emerald-300',
};

function badge(styles: string | undefined, label: string) {
  return (
    <span
      className={cn(
        'inline-flex rounded-full px-2.5 py-0.5 text-xs font-semibold capitalize ring-1',
        styles
      )}
    >
      {label}
    </span>
  );
}

function formatTime(isoTime: string) {
  const date = new Date(isoTime);
  if (Number.isNaN(date.getTime())) {
    return isoTime;
  }
  return date.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function EscalationCard({ record }: { record: EscalationRecord }) {
  return (
    <li className="bg-background/95 w-full rounded-3xl border border-emerald-500/10 px-5 py-4 shadow-[0_12px_40px_-24px_rgba(15,23,42,0.35)] backdrop-blur sm:px-6">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-sm font-bold tracking-tight">{record.reference_id}</span>
        <span className="text-muted-foreground text-xs">{formatTime(record.created_at)}</span>
        <span className="ml-auto flex items-center gap-2">
          {badge(URGENCY_STYLES[record.urgency], record.urgency)}
          {badge(STATUS_STYLES[record.status], record.status.replace('_', ' '))}
        </span>
      </div>

      <p className="text-foreground mt-3 text-sm leading-6 font-medium">{record.summary}</p>

      <div className="text-muted-foreground mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs leading-5">
        {record.language && <span>Language: {record.language}</span>}
        {record.preferred_follow_up && (
          <span>Preferred follow-up: {record.preferred_follow_up}</span>
        )}
      </div>

      {record.agent_checked && (
        <p className="text-muted-foreground mt-2 text-xs leading-5 italic">
          Already explained: {record.agent_checked}
        </p>
      )}
    </li>
  );
}

export default async function AdminPage() {
  const escalations = await getEscalations();

  return (
    <main className="flex h-full min-h-0 w-full justify-center overflow-y-auto bg-[radial-gradient(circle_at_top,_rgba(16,185,129,0.12),_transparent_55%)] px-3 py-6 sm:px-4 lg:px-6">
      <div className="w-full max-w-2xl">
        <div className="mb-5 flex items-center justify-between gap-3">
          <div className="flex items-center gap-2.5">
            <div className="flex h-10 w-10 items-center justify-center rounded-full bg-emerald-500/10 text-emerald-700 ring-1 ring-emerald-500/20 dark:text-emerald-300">
              <HeartPulseIcon className="size-5" />
            </div>
            <div>
              <h1 className="text-foreground text-lg font-semibold tracking-tight sm:text-xl">
                Human Support Queue
              </h1>
              <p className="text-muted-foreground text-xs leading-5">
                Aarogya Sahayak — requests approved by callers for human follow-up
              </p>
            </div>
          </div>
          <Link
            href="/"
            className="text-muted-foreground hover:text-foreground text-xs font-semibold underline underline-offset-4"
          >
            Back to conversation
          </Link>
        </div>

        {escalations.length === 0 ? (
          <div className="bg-background/95 flex w-full flex-col items-center justify-center rounded-3xl border border-emerald-500/10 px-5 py-12 text-center backdrop-blur">
            <div className="flex h-12 w-12 items-center justify-center rounded-full bg-emerald-500/10 text-emerald-700 dark:text-emerald-300">
              <InboxIcon className="size-6" />
            </div>
            <p className="text-foreground mt-4 text-sm font-semibold">No escalation requests yet</p>
            <p className="text-muted-foreground mt-1 max-w-sm text-xs leading-5">
              When a caller approves sharing a short summary, it appears here with a reference ID.
              Refresh this page to see new requests.
            </p>
          </div>
        ) : (
          <>
            <ul className="flex flex-col gap-3">
              {escalations.map((record) => (
                <EscalationCard key={record.reference_id} record={record} />
              ))}
            </ul>
            <p className="text-muted-foreground mx-auto mt-5 max-w-md px-4 text-center text-xs leading-5">
              Each request holds only the short, caller-approved summary — never the full
              conversation or private details.
            </p>
          </>
        )}
      </div>
    </main>
  );
}
