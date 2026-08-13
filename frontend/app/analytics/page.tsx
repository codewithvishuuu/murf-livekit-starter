import Link from 'next/link';
import { CheckCircle2Icon, HeartPulseIcon, PhoneCallIcon, XCircleIcon } from 'lucide-react';
import { getCallOutcomes } from '@/lib/analytics';

export const revalidate = 0;
export const dynamic = 'force-dynamic';

function statCardStyle(tone: 'neutral' | 'success' | 'failed') {
  switch (tone) {
    case 'success':
      return 'text-emerald-700 dark:text-emerald-300 ring-emerald-500/25 bg-emerald-500/10';
    case 'failed':
      return 'text-rose-700 dark:text-rose-300 ring-rose-500/25 bg-rose-500/10';
    default:
      return 'text-foreground ring-slate-500/20 bg-slate-400/10';
  }
}

function StatCard({
  label,
  value,
  tone,
  Icon,
}: {
  label: string;
  value: number;
  tone: 'neutral' | 'success' | 'failed';
  Icon: typeof PhoneCallIcon;
}) {
  return (
    <div className="bg-background/95 flex w-full flex-col items-center rounded-3xl border border-emerald-500/10 px-5 py-8 text-center shadow-[0_12px_40px_-24px_rgba(15,23,42,0.35)] backdrop-blur sm:px-6">
      <div
        className={`flex h-12 w-12 items-center justify-center rounded-full ring-1 ${statCardStyle(tone)}`}
      >
        <Icon className="size-6" />
      </div>
      <p className="text-foreground mt-4 font-mono text-4xl font-bold tracking-tight">{value}</p>
      <p className="text-muted-foreground mt-2 text-xs font-semibold tracking-wider uppercase">
        {label}
      </p>
    </div>
  );
}

export default async function AnalyticsPage() {
  const counts = await getCallOutcomes();

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
                Call Analytics
              </h1>
              <p className="text-muted-foreground text-xs leading-5">
                Aarogya Sahayak — outcomes of completed calls
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

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <StatCard label="Total Calls" value={counts.total} tone="neutral" Icon={PhoneCallIcon} />
          <StatCard
            label="Successful Calls"
            value={counts.successful}
            tone="success"
            Icon={CheckCircle2Icon}
          />
          <StatCard label="Failed Calls" value={counts.failed} tone="failed" Icon={XCircleIcon} />
        </div>

        <p className="text-muted-foreground mx-auto mt-5 max-w-md px-4 text-center text-xs leading-5">
          Counts are calculated from real recorded call outcomes. No transcripts or private details
          are stored or shown. Refresh this page after a call ends to see updated numbers.
        </p>
      </div>
    </main>
  );
}
