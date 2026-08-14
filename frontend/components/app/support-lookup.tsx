'use client';

import { Fragment, useEffect, useMemo, useState } from 'react';
import Link from 'next/link';
import {
  CalendarDaysIcon,
  CheckCircle2Icon,
  CircleAlertIcon,
  Clock3Icon,
  GaugeIcon,
  LifeBuoyIcon,
  Loader2Icon,
  type LucideIcon,
  PhoneCallIcon,
  SearchIcon,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/shadcn/utils';

export interface SupportRecord {
  reference_id: string;
  status: string;
  created_at: string;
  urgency: string | null;
  preferred_follow_up: string | null;
  last_updated: string | null;
}

interface SupportLookupProps {
  records: SupportRecord[];
}

const STATUS_STYLES: Record<string, string> = {
  open: 'bg-amber-500/10 text-amber-700 ring-amber-500/25 dark:text-amber-300',
  in_progress: 'bg-sky-500/10 text-sky-700 ring-sky-500/25 dark:text-sky-300',
  resolved: 'bg-emerald-500/10 text-emerald-700 ring-emerald-500/25 dark:text-emerald-300',
};

const STATUS_DOTS: Record<string, string> = {
  open: 'bg-amber-500',
  in_progress: 'bg-sky-500',
  resolved: 'bg-emerald-500',
};

const STATUS_LABELS: Record<string, string> = {
  open: 'Open',
  in_progress: 'In progress',
  resolved: 'Resolved',
};

const URGENCY_STYLES: Record<string, string> = {
  low: 'bg-slate-500/10 text-slate-700 ring-slate-500/25 dark:text-slate-300',
  medium: 'bg-amber-500/10 text-amber-700 ring-amber-500/25 dark:text-amber-300',
  high: 'bg-orange-500/10 text-orange-700 ring-orange-500/25 dark:text-orange-300',
  emergency: 'bg-rose-500/10 text-rose-700 ring-rose-500/25 dark:text-rose-300',
};

const URGENCY_LABELS: Record<string, string> = {
  low: 'Low',
  medium: 'Medium',
  high: 'High',
  emergency: 'Emergency',
};

const PROGRESS_STEPS = ['Request Created', 'Human Review', 'Resolved'] as const;

type StepState = 'completed' | 'current-check' | 'current-dot' | 'pending';

const CIRCLE_STYLES: Record<StepState, string> = {
  completed: 'bg-emerald-500 text-white',
  'current-check': 'bg-emerald-500 text-white ring-2 ring-emerald-500/30 animate-pulse',
  'current-dot': 'bg-emerald-500/90 ring-2 ring-emerald-500/30 animate-pulse',
  pending: 'bg-slate-400/10 ring-1 ring-slate-400/30',
};

const LABEL_STYLES: Record<StepState, string> = {
  completed: 'text-foreground',
  'current-check': 'font-semibold text-emerald-700 dark:text-emerald-300',
  'current-dot': 'font-semibold text-emerald-700 dark:text-emerald-300',
  pending: 'text-muted-foreground',
};

function progressStates(status: string): StepState[] {
  if (status === 'resolved') {
    return ['completed', 'completed', 'completed'];
  }
  if (status === 'in_progress') {
    return ['completed', 'current-check', 'pending'];
  }
  return ['completed', 'current-dot', 'pending'];
}

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

function capitalize(value: string) {
  return value.charAt(0).toUpperCase() + value.slice(1);
}

function MetaItem({
  Icon,
  label,
  value,
}: {
  Icon: LucideIcon;
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

export function SupportLookup({ records }: SupportLookupProps) {
  const [query, setQuery] = useState('');
  const [submitted, setSubmitted] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const hydrated = useHydrated();

  const match = useMemo(() => {
    if (submitted === null) {
      return undefined;
    }
    const needle = submitted.trim().toUpperCase();
    if (!needle) {
      return null;
    }
    return records.find((record) => record.reference_id.toUpperCase() === needle) ?? null;
  }, [records, submitted]);

  function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (checking) {
      return;
    }
    setChecking(true);
    window.setTimeout(() => {
      setSubmitted(query);
      setChecking(false);
    }, 400);
  }

  const showError = submitted !== null && match === null;
  const states = match ? progressStates(match.status) : null;

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
              <LifeBuoyIcon className="size-5" />
            </div>
            <div>
              <h1 className="text-foreground text-lg font-semibold tracking-tight sm:text-xl">
                Human Support
              </h1>
              <p className="text-muted-foreground text-xs leading-5">
                Check the status of your support request using its reference ID
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
              placeholder="e.g. ESC-20260813-001"
              autoComplete="off"
              spellCheck={false}
              aria-invalid={showError}
              aria-describedby={showError ? 'reference-error' : undefined}
              className="text-foreground placeholder:text-muted-foreground/60 bg-background/60 h-12 min-w-0 flex-1 rounded-full border border-emerald-500/15 px-4 text-sm transition-colors outline-none focus-visible:border-emerald-500/40 focus-visible:ring-2 focus-visible:ring-emerald-500/20"
            />
            <Button type="submit" size="lg" disabled={checking} className="shrink-0 rounded-full">
              {checking ? (
                <Loader2Icon className="size-4 animate-spin" />
              ) : (
                <SearchIcon className="size-4" />
              )}
              {checking ? 'Checking…' : 'Check status'}
            </Button>
          </form>

          {submitted === null && (
            <div className="mt-5 flex flex-col items-center gap-1 text-center">
              <p className="text-muted-foreground text-xs leading-5">
                Enter your reference ID to view your support request status.
              </p>
              <p className="text-muted-foreground/70 text-[11px] leading-5">
                When the agent creates a human support request for you, it gives you a reference ID.
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
                We couldn’t find a support request with that reference ID.
              </p>
              <p className="text-muted-foreground mt-1 pl-6 text-xs leading-5">
                Check the ID the agent gave you and try again.
              </p>
            </div>
          )}

          {match && states && (
            <div
              role="status"
              aria-live="polite"
              className="mt-6 border-t border-emerald-500/10 pt-5"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                  <p className="text-muted-foreground text-[11px] font-semibold tracking-wider uppercase">
                    Support request
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
                  label="Created"
                  value={
                    hydrated
                      ? formatLocalDateTime(match.created_at)
                      : formatDateTime(match.created_at)
                  }
                />
                {match.last_updated && (
                  <MetaItem
                    Icon={Clock3Icon}
                    label="Last updated"
                    value={
                      hydrated
                        ? formatLocalDateTime(match.last_updated)
                        : formatDateTime(match.last_updated)
                    }
                  />
                )}
                {match.urgency && (
                  <MetaItem
                    Icon={GaugeIcon}
                    label="Urgency"
                    value={
                      <span
                        className={cn(
                          'inline-flex rounded-full px-2 py-0.5 text-[10px] font-semibold capitalize ring-1',
                          URGENCY_STYLES[match.urgency]
                        )}
                      >
                        {URGENCY_LABELS[match.urgency] ?? match.urgency}
                      </span>
                    }
                  />
                )}
                {match.preferred_follow_up && (
                  <MetaItem
                    Icon={PhoneCallIcon}
                    label="Follow-up"
                    value={capitalize(match.preferred_follow_up)}
                  />
                )}
              </div>

              <div className="mt-5">
                <p className="text-muted-foreground text-[11px] font-semibold tracking-wider uppercase">
                  Progress
                </p>
                <ol className="mt-3 flex items-start" aria-label="Support request progress">
                  {PROGRESS_STEPS.map((label, index) => {
                    const state = states[index];
                    const isActive = state === 'current-check' || state === 'current-dot';
                    const isDone = state === 'completed' || state === 'current-check';
                    return (
                      <Fragment key={label}>
                        <li
                          className="flex w-20 flex-col items-center gap-1.5 sm:w-24"
                          aria-current={isActive ? 'step' : undefined}
                        >
                          <div
                            className={cn(
                              'flex size-7 items-center justify-center rounded-full transition-all duration-300',
                              CIRCLE_STYLES[state]
                            )}
                          >
                            {isDone ? (
                              <CheckCircle2Icon className="size-4" />
                            ) : (
                              <span
                                className={cn(
                                  'size-2 rounded-full',
                                  state === 'pending' ? 'bg-slate-400/60' : 'bg-white/90'
                                )}
                              />
                            )}
                          </div>
                          <span
                            className={cn(
                              'text-center text-[11px] leading-tight',
                              LABEL_STYLES[state]
                            )}
                          >
                            {label}
                          </span>
                        </li>
                        {index < PROGRESS_STEPS.length - 1 && (
                          <div
                            aria-hidden="true"
                            className={cn(
                              'mt-3.5 h-0.5 min-w-4 flex-1 rounded-full transition-colors duration-300',
                              isDone ? 'bg-emerald-500/60' : 'bg-slate-400/20'
                            )}
                          />
                        )}
                      </Fragment>
                    );
                  })}
                </ol>
              </div>

              <div className="mt-5 rounded-2xl border border-emerald-500/10 bg-emerald-500/5 px-4 py-3.5">
                <p className="text-muted-foreground text-[11px] font-semibold tracking-wider uppercase">
                  Request activity
                </p>
                <ul className="mt-2.5 flex flex-col gap-2 text-xs">
                  <li className="flex items-center gap-2.5">
                    <CalendarDaysIcon className="size-3.5 text-emerald-700 dark:text-emerald-300" />
                    <span className="text-muted-foreground">Request created</span>
                    <span className="text-foreground ml-auto font-mono">
                      {hydrated
                        ? formatLocalDateTime(match.created_at)
                        : formatDateTime(match.created_at)}
                    </span>
                  </li>
                  <li className="flex items-center gap-2.5">
                    <span
                      className={cn('size-2 shrink-0 rounded-full', STATUS_DOTS[match.status])}
                    />
                    <span className="text-muted-foreground">Current status</span>
                    <span className="ml-auto">{statusBadge}</span>
                  </li>
                  {match.last_updated && (
                    <li className="flex items-center gap-2.5">
                      <Clock3Icon className="size-3.5 text-emerald-700 dark:text-emerald-300" />
                      <span className="text-muted-foreground">Last updated</span>
                      <span className="text-foreground ml-auto font-mono">
                        {hydrated
                          ? formatLocalDateTime(match.last_updated)
                          : formatDateTime(match.last_updated)}
                      </span>
                    </li>
                  )}
                </ul>
              </div>
            </div>
          )}

          <p className="text-muted-foreground mt-5 text-center text-xs leading-5">
            For your privacy, this page shows only your request status and reference details.
            Conversation content and personal details are not displayed.
          </p>
        </div>
      </div>
    </main>
  );
}
