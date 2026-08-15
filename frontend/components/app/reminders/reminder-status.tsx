'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import {
  BellIcon,
  CalendarDaysIcon,
  CalendarX2Icon,
  CircleAlertIcon,
  Clock3Icon,
  Loader2Icon,
  MessageSquareTextIcon,
  RefreshCwIcon,
  SearchIcon,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { cancelReminder, fetchReminderStatus } from '@/lib/reminder-client';
import type { SafeReminder } from '@/lib/reminders';
import { cn } from '@/lib/shadcn/utils';

interface ReminderStatusLookupProps {
  reminders: SafeReminder[];
}

const STATUS_STYLES: Record<string, string> = {
  pending: 'bg-amber-500/10 text-amber-700 ring-amber-500/25 dark:text-amber-300',
  triggered: 'bg-sky-500/10 text-sky-700 ring-sky-500/25 dark:text-sky-300',
  completed: 'bg-emerald-500/10 text-emerald-700 ring-emerald-500/25 dark:text-emerald-300',
  failed: 'bg-rose-500/10 text-rose-700 ring-rose-500/25 dark:text-rose-300',
  cancelled: 'bg-slate-500/10 text-slate-700 ring-slate-500/25 dark:text-slate-300',
};

const STATUS_LABELS: Record<string, string> = {
  pending: 'Pending',
  triggered: 'Triggered',
  completed: 'Completed',
  failed: 'Failed',
  cancelled: 'Cancelled',
};

const DATE_TIME_OPTIONS: Intl.DateTimeFormatOptions = {
  year: 'numeric',
  month: 'short',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
};

function formatDateTime(isoTime: string) {
  const date = new Date(isoTime);
  if (Number.isNaN(date.getTime())) {
    return isoTime;
  }
  // Fixed UTC so server and client render identical text during
  // SSR/hydration (backend stores timestamps in UTC; avoids hydration
  // mismatch). After hydration the UI switches to browser-local time.
  return date.toLocaleString(undefined, { timeZone: 'UTC', ...DATE_TIME_OPTIONS });
}

function formatLocalDateTime(isoTime: string) {
  const date = new Date(isoTime);
  if (Number.isNaN(date.getTime())) {
    return isoTime;
  }
  // No timeZone option: Intl renders in the browser's own local timezone.
  return date.toLocaleString(undefined, DATE_TIME_OPTIONS);
}

function useHydrated() {
  const [hydrated, setHydrated] = useState(false);
  useEffect(() => {
    setHydrated(true);
  }, []);
  return hydrated;
}

function MetaItem({
  Icon,
  label,
  value,
}: {
  Icon: typeof CalendarDaysIcon;
  label: string;
  value: React.ReactNode;
}) {
  return (
    <div className="flex min-w-[140px] flex-1 items-center gap-2.5 rounded-2xl border border-emerald-500/10 bg-emerald-500/5 px-3 py-2.5">
      <div className="flex size-7 shrink-0 items-center justify-center rounded-full bg-emerald-500/10 text-emerald-700 dark:text-emerald-300">
        <Icon className="size-3.5" />
      </div>
      <div className="min-w-0">
        <p className="text-muted-foreground text-[10px] font-semibold tracking-wider uppercase">
          {label}
        </p>
        <div className="text-foreground truncate text-xs font-medium">{value}</div>
      </div>
    </div>
  );
}

const TERMINAL_STATUSES = ['completed', 'failed', 'cancelled'];

const AUTO_REFRESH_MS = 10_000;

export function ReminderStatusLookup({ reminders }: ReminderStatusLookupProps) {
  const [query, setQuery] = useState('');
  const [match, setMatch] = useState<SafeReminder | null | undefined>(undefined);
  const [checking, setChecking] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [confirmingCancel, setConfirmingCancel] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);
  const hydrated = useHydrated();

  const terminal = match ? TERMINAL_STATUSES.includes(match.status) : true;

  function findMatch(needle: string): SafeReminder | null {
    const normalized = needle.trim().toUpperCase();
    if (!normalized) {
      return null;
    }
    return reminders.find((record) => record.reference_id.toUpperCase() === normalized) ?? null;
  }

  function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (checking) {
      return;
    }
    setChecking(true);
    window.setTimeout(() => {
      setMatch(findMatch(query));
      setChecking(false);
    }, 400);
  }

  async function handleRefresh() {
    if (!match || refreshing) {
      return;
    }
    setRefreshing(true);
    try {
      const result = await fetchReminderStatus(match.reference_id);
      if (result.found && result.reminder) {
        setMatch(result.reminder);
        setCancelError(null);
      } else {
        setMatch(null);
      }
    } finally {
      setRefreshing(false);
    }
  }

  async function handleConfirmCancel() {
    if (!match || cancelling) {
      return;
    }
    setCancelling(true);
    setCancelError(null);
    try {
      const result = await cancelReminder(match.reference_id);
      if (result.ok) {
        const fresh = await fetchReminderStatus(match.reference_id);
        if (fresh.found && fresh.reminder) {
          setMatch(fresh.reminder);
        } else {
          setMatch({ ...match, status: 'cancelled' });
        }
        setConfirmingCancel(false);
      } else {
        setCancelError(result.error ?? 'The reminder could not be cancelled.');
        const fresh = await fetchReminderStatus(match.reference_id);
        if (fresh.found && fresh.reminder) {
          setMatch(fresh.reminder);
        }
      }
    } finally {
      setCancelling(false);
    }
  }

  // Auto-refresh while the reminder is still live (pending/triggered);
  // stops automatically once the status reaches a terminal state.
  useEffect(() => {
    if (!match || terminal || refreshing) {
      return;
    }
    const timer = window.setInterval(() => {
      void fetchReminderStatus(match.reference_id)
        .then((result) => {
          const fresh = result.reminder;
          if (result.found && fresh) {
            setMatch((current) => (current && current.status === fresh.status ? current : fresh));
          }
        })
        .catch(() => {
          // Transient fetch failures are ignored; the next tick retries.
        });
    }, AUTO_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [match, refreshing, terminal]);

  const showError = match === null;

  const statusBadge = match ? (
    <span
      className={cn(
        'inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold capitalize ring-1',
        STATUS_STYLES[match.status]
      )}
    >
      {STATUS_LABELS[match.status] ?? match.status}
    </span>
  ) : null;

  return (
    <main className="flex h-full min-h-0 w-full justify-center overflow-y-auto bg-[radial-gradient(circle_at_top,_rgba(16,185,129,0.12),_transparent_55%)] px-3 py-6 sm:px-4 lg:px-6">
      <div className="w-full max-w-2xl">
        <div className="mb-5 flex items-center justify-between gap-3">
          <div className="flex items-center gap-2.5">
            <div className="flex h-10 w-10 items-center justify-center rounded-full bg-emerald-500/10 text-emerald-700 ring-1 ring-emerald-500/20 dark:text-emerald-300">
              <BellIcon className="size-5" />
            </div>
            <div>
              <h1 className="text-foreground text-lg font-semibold tracking-tight sm:text-xl">
                Scheduled Reminder
              </h1>
              <p className="text-muted-foreground text-xs leading-5">
                Check the status of your reminder using its reference ID
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

        <div className="bg-background/95 w-full rounded-3xl border border-emerald-500/10 px-5 py-6 shadow-[0_12px_40px_-24px_rgba(15,23,42,0.35)] backdrop-blur sm:px-6">
          <form onSubmit={handleSubmit} className="flex flex-col gap-3 sm:flex-row">
            <label htmlFor="reference-id" className="sr-only">
              Reference ID
            </label>
            <input
              id="reference-id"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="e.g. REM-20260815-001"
              autoComplete="off"
              spellCheck={false}
              aria-invalid={showError}
              aria-describedby={showError ? 'reference-error' : undefined}
              className="text-foreground placeholder:text-muted-foreground/60 bg-background/60 h-12 min-w-0 flex-1 rounded-full border border-emerald-500/15 px-4 font-mono text-sm transition-colors outline-none focus-visible:border-emerald-500/40 focus-visible:ring-2 focus-visible:ring-emerald-500/20"
            />
            <Button type="submit" size="lg" disabled={checking} className="shrink-0 rounded-full">
              {checking ? (
                <Loader2Icon className="size-4 animate-spin" />
              ) : (
                <SearchIcon className="size-4" />
              )}
              {checking ? 'Checking…' : 'Check Status'}
            </Button>
          </form>

          {match === undefined && (
            <div className="mt-5 flex flex-col items-center gap-1 text-center">
              <p className="text-muted-foreground text-xs leading-5">
                Enter your reference ID to view your reminder status.
              </p>
              <p className="text-muted-foreground/70 text-[11px] leading-5">
                When you ask the agent to remind you of something by phone, it gives you a reference
                ID.
              </p>
            </div>
          )}

          {showError && (
            <div
              id="reference-error"
              role="alert"
              className="mt-5 rounded-2xl border border-rose-500/15 bg-rose-500/5 px-4 py-3.5"
            >
              <p className="flex items-center gap-2 text-sm font-semibold text-rose-700 dark:text-rose-300">
                <CircleAlertIcon className="size-4 shrink-0" />
                Reminder not found.
              </p>
              <p className="text-muted-foreground mt-1 pl-6 text-xs leading-5">
                Check the reference ID the agent gave you and try again.
              </p>
            </div>
          )}

          {match && (
            <div
              role="status"
              aria-live="polite"
              className="mt-6 border-t border-emerald-500/10 pt-5"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                  <p className="text-muted-foreground text-[11px] font-semibold tracking-wider uppercase">
                    Reminder
                  </p>
                  <p className="text-foreground mt-0.5 font-mono text-sm font-bold tracking-tight">
                    {match.reference_id}
                  </p>
                </div>
                {statusBadge}
              </div>

              <div className="mt-4 flex flex-wrap gap-2.5">
                <MetaItem
                  Icon={CalendarDaysIcon}
                  label="Scheduled"
                  value={
                    hydrated
                      ? formatLocalDateTime(match.scheduled_at)
                      : formatDateTime(match.scheduled_at)
                  }
                />
                <MetaItem
                  Icon={Clock3Icon}
                  label="Created"
                  value={
                    hydrated
                      ? formatLocalDateTime(match.created_at)
                      : formatDateTime(match.created_at)
                  }
                />
                {match.message && (
                  <MetaItem Icon={MessageSquareTextIcon} label="Reminder" value={match.message} />
                )}
              </div>

              <div className="mt-5 flex flex-wrap items-center gap-2.5">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={handleRefresh}
                  disabled={refreshing}
                  className="rounded-full"
                >
                  {refreshing ? (
                    <Loader2Icon className="size-4 animate-spin" />
                  ) : (
                    <RefreshCwIcon className="size-4" />
                  )}
                  Refresh Status
                </Button>
                {match.status === 'pending' && !confirmingCancel && (
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => {
                      setConfirmingCancel(true);
                      setCancelError(null);
                    }}
                    className="rounded-full border-rose-500/25 text-rose-700 hover:border-rose-500/40 hover:bg-rose-500/10 hover:text-rose-700 dark:text-rose-300 dark:hover:text-rose-300"
                  >
                    <CalendarX2Icon className="size-4" />
                    Cancel Reminder
                  </Button>
                )}
                {!terminal && (
                  <p className="text-muted-foreground/70 flex items-center gap-1.5 text-[11px]">
                    <span className="inline-block size-1.5 animate-pulse rounded-full bg-emerald-500/80" />
                    Auto-refreshing every 10 seconds
                  </p>
                )}
              </div>

              {confirmingCancel && (
                <div
                  role="alertdialog"
                  aria-label="Cancel reminder confirmation"
                  className="mt-4 rounded-2xl border border-rose-500/15 bg-rose-500/5 px-4 py-3.5"
                >
                  <p className="flex items-center gap-2 text-sm font-semibold text-rose-700 dark:text-rose-300">
                    <CircleAlertIcon className="size-4 shrink-0" />
                    Are you sure you want to cancel this reminder?
                  </p>
                  <p className="text-muted-foreground mt-1 pl-6 text-xs leading-5">
                    The reminder call will not be placed. This cannot be undone.
                  </p>
                  <div className="mt-3 flex flex-wrap items-center gap-2.5 pl-6">
                    <Button
                      type="button"
                      variant="destructive"
                      size="sm"
                      onClick={handleConfirmCancel}
                      disabled={cancelling}
                      className="rounded-full"
                    >
                      {cancelling ? (
                        <Loader2Icon className="size-4 animate-spin" />
                      ) : (
                        <CalendarX2Icon className="size-4" />
                      )}
                      Yes, cancel reminder
                    </Button>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => {
                        setConfirmingCancel(false);
                        setCancelError(null);
                      }}
                      disabled={cancelling}
                      className="rounded-full"
                    >
                      Keep reminder
                    </Button>
                  </div>
                  {cancelError && (
                    <p className="mt-2 pl-6 text-xs text-rose-700 dark:text-rose-300">
                      {cancelError}
                    </p>
                  )}
                </div>
              )}
            </div>
          )}

          <p className="text-muted-foreground mt-5 text-center text-xs leading-5">
            For your privacy, this page shows only your reminder status and reference details. Phone
            numbers and destination details are never displayed.
          </p>
        </div>
      </div>
    </main>
  );
}
