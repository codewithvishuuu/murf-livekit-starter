'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import {
  ActivityIcon,
  CheckCircle2Icon,
  HeartPulseIcon,
  InboxIcon,
  PhoneCallIcon,
  RefreshCwIcon,
  XCircleIcon,
} from 'lucide-react';
import { CallsOverTimeChart, OutcomeSplitBar } from '@/components/app/analytics/charts';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import type { CallAnalytics } from '@/lib/analytics';
import { cn } from '@/lib/shadcn/utils';

export const POLL_INTERVAL_MS = 4000;

const CHANNELS = [
  { value: 'all', label: 'All channels' },
  { value: 'browser', label: 'Browser' },
  { value: 'sip', label: 'SIP' },
  { value: 'outbound', label: 'Outbound' },
  { value: 'console', label: 'Console' },
] as const;

const OUTCOMES = [
  { value: 'all', label: 'All outcomes' },
  { value: 'success', label: 'Successful' },
  { value: 'failed', label: 'Failed' },
] as const;

const REASON_LABELS: Record<string, string> = {
  health_guidance: 'Health guidance',
  escalation_created: 'Escalation created',
  no_useful_outcome: 'No useful outcome',
};

const CATEGORY_LABELS: Record<string, string> = {
  user_hangup: 'User hang-up',
  no_response: 'No response',
  incomplete_task: 'Incomplete task',
  tool_failure: 'Tool failure',
  api_error: 'API error',
  technical_error: 'Technical error',
  other: 'Other',
};

const CATEGORY_STYLES: Record<string, string> = {
  user_hangup: 'bg-slate-500/10 text-slate-700 ring-slate-500/25 dark:text-slate-300',
  no_response: 'bg-slate-500/10 text-slate-700 ring-slate-500/25 dark:text-slate-300',
  incomplete_task: 'bg-amber-500/10 text-amber-700 ring-amber-500/25 dark:text-amber-300',
  tool_failure: 'bg-orange-500/10 text-orange-700 ring-orange-500/25 dark:text-orange-300',
  api_error: 'bg-sky-500/10 text-sky-700 ring-sky-500/25 dark:text-sky-300',
  technical_error: 'bg-rose-500/10 text-rose-700 ring-rose-500/25 dark:text-rose-300',
  other: 'bg-slate-500/10 text-slate-700 ring-slate-500/25 dark:text-slate-300',
};

const CHANNEL_STYLES: Record<string, string> = {
  browser: 'bg-emerald-500/10 text-emerald-700 ring-emerald-500/25 dark:text-emerald-300',
  sip: 'bg-sky-500/10 text-sky-700 ring-sky-500/25 dark:text-sky-300',
  outbound: 'bg-violet-500/10 text-violet-700 ring-violet-500/25 dark:text-violet-300',
  console: 'bg-slate-500/10 text-slate-700 ring-slate-500/25 dark:text-slate-300',
};

export interface AnalyticsFilters {
  channel: string;
  outcome: string;
  dateFrom: string;
  dateTo: string;
  language: string;
}

const DEFAULT_FILTERS: AnalyticsFilters = {
  channel: 'all',
  outcome: 'all',
  dateFrom: '',
  dateTo: '',
  language: 'all',
};

function badge(styles: string | undefined, label: string) {
  return (
    <span
      className={cn(
        'inline-flex shrink-0 rounded-full px-2.5 py-0.5 text-xs font-semibold capitalize ring-1',
        styles
      )}
    >
      {label}
    </span>
  );
}

const DATE_TIME_OPTIONS: Intl.DateTimeFormatOptions = {
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
  // Fixed locale + UTC so server and client render identical text during
  // SSR/hydration (backend stores timestamps in UTC; avoids hydration
  // mismatch). After hydration the table switches to browser-local time.
  return date.toLocaleString('en-US', { timeZone: 'UTC', ...DATE_TIME_OPTIONS });
}

function formatLocalDateTime(isoTime: string) {
  const date = new Date(isoTime);
  if (Number.isNaN(date.getTime())) {
    return isoTime;
  }
  // No timeZone option: Intl renders in the browser's own local timezone.
  return date.toLocaleString('en-US', DATE_TIME_OPTIONS);
}

function useHydrated() {
  const [hydrated, setHydrated] = useState(false);
  useEffect(() => {
    setHydrated(true);
  }, []);
  return hydrated;
}

function formatDuration(durationS: number | null) {
  if (durationS === null || durationS === undefined) {
    return '—';
  }
  const seconds = Math.round(durationS);
  if (seconds < 60) {
    return `${seconds} sec`;
  }
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return rest === 0 ? `${minutes} min` : `${minutes} min ${rest} sec`;
}

function formatLatency(latencyS: number | null) {
  if (latencyS === null || latencyS === undefined) {
    return '—';
  }
  return `${latencyS.toFixed(2)}s`;
}

function formatRate(rate: number) {
  const rounded = Math.round(rate * 10) / 10;
  return Number.isInteger(rounded) ? `${rounded}%` : `${rounded.toFixed(1)}%`;
}

function statCardStyle(tone: 'neutral' | 'success' | 'failed' | 'accent') {
  switch (tone) {
    case 'success':
      return 'text-emerald-700 dark:text-emerald-300 ring-emerald-500/25 bg-emerald-500/10';
    case 'failed':
      return 'text-rose-700 dark:text-rose-300 ring-rose-500/25 bg-rose-500/10';
    case 'accent':
      return 'text-teal-700 dark:text-teal-300 ring-teal-500/25 bg-teal-500/10';
    default:
      return 'text-foreground ring-slate-500/20 bg-slate-400/10';
  }
}

function StatCard({
  label,
  value,
  tone,
  Icon,
  hint,
}: {
  label: string;
  value: string;
  tone: 'neutral' | 'success' | 'failed' | 'accent';
  Icon: typeof PhoneCallIcon;
  hint?: string;
}) {
  return (
    <div className="bg-background/95 flex w-full flex-col items-center rounded-3xl border border-emerald-500/10 px-5 py-6 text-center shadow-[0_12px_40px_-24px_rgba(15,23,42,0.35)] backdrop-blur sm:px-6">
      <div
        className={cn(
          'flex h-12 w-12 items-center justify-center rounded-full ring-1',
          statCardStyle(tone)
        )}
      >
        <Icon className="size-6" />
      </div>
      <p className="text-foreground mt-4 font-mono text-4xl font-bold tracking-tight">{value}</p>
      <p className="text-muted-foreground mt-2 text-xs font-semibold tracking-wider uppercase">
        {label}
      </p>
      {hint && <p className="text-muted-foreground mt-1 text-[11px] leading-4">{hint}</p>}
    </div>
  );
}

function Card({
  title,
  subtitle,
  children,
  className,
}: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section
      className={cn(
        'bg-background/95 flex w-full flex-col rounded-3xl border border-emerald-500/10 p-5 shadow-[0_12px_40px_-24px_rgba(15,23,42,0.35)] backdrop-blur sm:p-6',
        className
      )}
    >
      <div className="mb-4">
        <h2 className="text-foreground text-sm font-semibold tracking-tight">{title}</h2>
        {subtitle && <p className="text-muted-foreground mt-0.5 text-xs">{subtitle}</p>}
      </div>
      {children}
    </section>
  );
}

function buildQuery(filters: AnalyticsFilters) {
  const params = new URLSearchParams();
  if (filters.channel !== 'all') {
    params.set('channel', filters.channel);
  }
  if (filters.outcome !== 'all') {
    params.set('outcome', filters.outcome);
  }
  if (filters.dateFrom) {
    params.set('dateFrom', filters.dateFrom);
  }
  if (filters.dateTo) {
    params.set('dateTo', filters.dateTo);
  }
  if (filters.language !== 'all') {
    params.set('language', filters.language);
  }
  const query = params.toString();
  return query ? `?${query}` : '';
}

export function AnalyticsDashboard({ initialData }: { initialData: CallAnalytics }) {
  const [data, setData] = useState<CallAnalytics>(initialData);
  const [filters, setFilters] = useState<AnalyticsFilters>(DEFAULT_FILTERS);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [live, setLive] = useState(true);
  const hydrated = useHydrated();
  const inFlight = useRef(false);

  const refresh = useCallback(async () => {
    if (inFlight.current) {
      return;
    }
    inFlight.current = true;
    try {
      const response = await fetch(`/api/analytics${buildQuery(filters)}`, {
        cache: 'no-store',
      });
      if (!response.ok) {
        throw new Error(`analytics request failed: ${response.status}`);
      }
      const payload: CallAnalytics = await response.json();
      setData(payload);
      setLastUpdated(new Date());
      setLive(true);
    } catch {
      // Keep showing the last good data; mark the feed as stale.
      setLive(false);
    } finally {
      inFlight.current = false;
    }
  }, [filters]);

  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const hasFilters =
    filters.channel !== 'all' ||
    filters.outcome !== 'all' ||
    filters.dateFrom !== '' ||
    filters.dateTo !== '' ||
    filters.language !== 'all';

  const languages = Array.from(
    new Set(
      data.recent_calls
        .map((call) => call.language)
        .filter((language): language is string => language !== null)
    )
  );
  if (data.languages.length > 0) {
    languages.push(...data.languages.map((entry) => entry.language));
  }
  const languageOptions = Array.from(new Set(languages)).sort();

  return (
    <main className="flex h-full min-h-0 w-full justify-center overflow-y-auto bg-[radial-gradient(circle_at_top,_rgba(16,185,129,0.12),_transparent_55%)] px-3 py-6 sm:px-4 lg:px-6">
      <div className="w-full max-w-5xl">
        <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
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
          <div className="flex items-center gap-3">
            <span
              className={cn(
                'inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-semibold ring-1',
                live
                  ? 'bg-emerald-500/10 text-emerald-700 ring-emerald-500/25 dark:text-emerald-300'
                  : 'bg-amber-500/10 text-amber-700 ring-amber-500/25 dark:text-amber-300'
              )}
            >
              <RefreshCwIcon
                className={cn('size-3', live && 'animate-spin')}
                style={{ animationDuration: '2s' }}
              />
              {live ? 'Live' : 'Reconnecting'}
            </span>
            {lastUpdated && (
              <span className="text-muted-foreground text-[11px]">
                updated{' '}
                {lastUpdated.toLocaleTimeString('en-US', {
                  timeZone: 'UTC',
                  hour: '2-digit',
                  minute: '2-digit',
                })}
              </span>
            )}
            <Link
              href="/"
              className="text-muted-foreground hover:text-foreground text-xs font-semibold underline underline-offset-4"
            >
              Back to conversation
            </Link>
          </div>
        </div>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <StatCard
            label="Total Calls"
            value={String(data.total)}
            tone="neutral"
            Icon={PhoneCallIcon}
          />
          <StatCard
            label="Successful Calls"
            value={String(data.successful)}
            tone="success"
            Icon={CheckCircle2Icon}
          />
          <StatCard
            label="Failed Calls"
            value={String(data.failed)}
            tone="failed"
            Icon={XCircleIcon}
          />
          <StatCard
            label="Success Rate"
            value={formatRate(data.success_rate)}
            tone="accent"
            Icon={ActivityIcon}
            hint={data.total === 0 ? 'No calls yet' : 'of all recorded calls'}
          />
        </div>

        <div className="mt-3 grid grid-cols-1 gap-3 lg:grid-cols-2">
          <Card title="Calls over time" subtitle="Last 14 days (UTC)">
            <CallsOverTimeChart points={data.calls_over_time} />
          </Card>
          <Card title="Successful vs failed" subtitle="Across all channels">
            <OutcomeSplitBar successful={data.successful} failed={data.failed} />
            <div className="mt-5">
              <p className="text-muted-foreground mb-2 text-xs font-semibold tracking-wider uppercase">
                By channel
              </p>
              <ul className="flex flex-col gap-2">
                {data.channels
                  .filter((entry) => entry.total > 0)
                  .map((entry) => (
                    <li key={entry.channel} className="flex items-center gap-2 text-sm">
                      {badge(CHANNEL_STYLES[entry.channel], entry.channel)}
                      <span className="text-muted-foreground ml-auto font-mono text-xs">
                        {entry.successful} ok / {entry.failed} failed
                      </span>
                    </li>
                  ))}
                {data.channels.every((entry) => entry.total === 0) && (
                  <li className="text-muted-foreground text-xs">No calls recorded yet</li>
                )}
              </ul>
            </div>
          </Card>
        </div>

        <div className="mt-3 grid grid-cols-1 gap-3 lg:grid-cols-3">
          <Card title="Average Latency" subtitle="Agent response time per turn">
            <div className="flex items-center gap-3">
              <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-full bg-teal-500/10 text-teal-700 ring-1 ring-teal-500/25 dark:text-teal-300">
                <ActivityIcon className="size-5" />
              </div>
              <p className="text-foreground font-mono text-3xl font-bold tracking-tight">
                {formatLatency(data.avg_latency_s)}
              </p>
            </div>
            <p className="text-muted-foreground mt-3 text-xs leading-5">
              Time from the caller finishing their utterance to the agent beginning its spoken
              response. Only real, measured turns are counted — never invented values.
            </p>
          </Card>
          <Card
            title="Failure Categories"
            subtitle="Why failed calls failed"
            className="lg:col-span-2"
          >
            {data.failure_categories.length === 0 ? (
              <div className="text-muted-foreground flex items-center gap-2 text-sm">
                <InboxIcon className="size-4" />
                No failed calls to categorize{data.failed > 0 ? ' yet' : ''}.
              </div>
            ) : (
              <ul className="flex flex-col gap-2.5">
                {data.failure_categories.map((entry) => {
                  const maxCount = Math.max(...data.failure_categories.map((item) => item.count));
                  return (
                    <li key={entry.category}>
                      <div className="flex items-center justify-between gap-2 text-sm">
                        <span className="flex items-center gap-2">
                          {badge(CATEGORY_STYLES[entry.category], CATEGORY_LABELS[entry.category])}
                        </span>
                        <span className="font-mono text-xs font-bold">{entry.count}</span>
                      </div>
                      <div className="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-slate-400/15">
                        <div
                          className="h-full rounded-full bg-rose-500/60 transition-[width] duration-500 dark:bg-rose-400/60"
                          style={{ width: `${(entry.count / maxCount) * 100}%` }}
                        />
                      </div>
                    </li>
                  );
                })}
              </ul>
            )}
          </Card>
        </div>

        <section className="bg-background/95 mt-3 w-full rounded-3xl border border-emerald-500/10 p-5 shadow-[0_12px_40px_-24px_rgba(15,23,42,0.35)] backdrop-blur sm:p-6">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="text-foreground text-sm font-semibold tracking-tight">
                Recent Call History
              </h2>
              <p className="text-muted-foreground mt-0.5 text-xs">
                Latest {data.recent_calls.length} calls{hasFilters ? ' matching filters' : ''} —
                analytics metadata only, never conversation content
              </p>
            </div>
            {hasFilters && (
              <button
                type="button"
                onClick={() => setFilters(DEFAULT_FILTERS)}
                className="text-muted-foreground hover:text-foreground text-xs font-semibold underline underline-offset-4"
              >
                Reset filters
              </button>
            )}
          </div>

          <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <div>
              <label className="text-muted-foreground mb-1 block text-[11px] font-semibold tracking-wider uppercase">
                Channel
              </label>
              <Select
                value={filters.channel}
                onValueChange={(value) => setFilters((prev) => ({ ...prev, channel: value }))}
              >
                <SelectTrigger className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {CHANNELS.map((entry) => (
                    <SelectItem key={entry.value} value={entry.value}>
                      {entry.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div>
              <label className="text-muted-foreground mb-1 block text-[11px] font-semibold tracking-wider uppercase">
                Outcome
              </label>
              <Select
                value={filters.outcome}
                onValueChange={(value) => setFilters((prev) => ({ ...prev, outcome: value }))}
              >
                <SelectTrigger className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {OUTCOMES.map((entry) => (
                    <SelectItem key={entry.value} value={entry.value}>
                      {entry.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div>
              <label className="text-muted-foreground mb-1 block text-[11px] font-semibold tracking-wider uppercase">
                Language
              </label>
              <Select
                value={filters.language}
                onValueChange={(value) => setFilters((prev) => ({ ...prev, language: value }))}
              >
                <SelectTrigger className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All languages</SelectItem>
                  {languageOptions.map((language) => (
                    <SelectItem key={language} value={language}>
                      {language}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="grid grid-cols-2 gap-2">
              <div>
                <label className="text-muted-foreground mb-1 block text-[11px] font-semibold tracking-wider uppercase">
                  From
                </label>
                <input
                  type="date"
                  value={filters.dateFrom}
                  max={filters.dateTo || undefined}
                  onChange={(event) =>
                    setFilters((prev) => ({ ...prev, dateFrom: event.target.value }))
                  }
                  className="border-input dark:bg-input/30 focus-visible:border-ring focus-visible:ring-ring/50 h-9 w-full rounded-md border bg-transparent px-3 text-sm shadow-xs outline-none focus-visible:ring-[3px]"
                />
              </div>
              <div>
                <label className="text-muted-foreground mb-1 block text-[11px] font-semibold tracking-wider uppercase">
                  To
                </label>
                <input
                  type="date"
                  value={filters.dateTo}
                  min={filters.dateFrom || undefined}
                  onChange={(event) =>
                    setFilters((prev) => ({ ...prev, dateTo: event.target.value }))
                  }
                  className="border-input dark:bg-input/30 focus-visible:border-ring focus-visible:ring-ring/50 h-9 w-full rounded-md border bg-transparent px-3 text-sm shadow-xs outline-none focus-visible:ring-[3px]"
                />
              </div>
            </div>
          </div>

          {data.recent_calls.length === 0 ? (
            <div className="text-muted-foreground mt-6 flex flex-col items-center gap-2 rounded-2xl border border-dashed border-slate-300/40 py-10 text-center dark:border-slate-700/40">
              <InboxIcon className="size-6 opacity-60" />
              <p className="text-sm font-medium">No calls match these filters yet</p>
              <p className="max-w-sm text-xs leading-5">
                When a call ends, its privacy-safe outcome appears here automatically.
              </p>
            </div>
          ) : (
            <div className="mt-4 overflow-x-auto">
              <table className="w-full min-w-[720px] text-left text-sm">
                <thead>
                  <tr className="text-muted-foreground border-b border-slate-300/40 text-[11px] tracking-wider uppercase dark:border-slate-700/40">
                    <th className="px-2 py-2 font-semibold">Call ID</th>
                    <th className="px-2 py-2 font-semibold">Date / time</th>
                    <th className="px-2 py-2 font-semibold">Channel</th>
                    <th className="px-2 py-2 font-semibold">Outcome</th>
                    <th className="px-2 py-2 font-semibold">Outcome reason</th>
                    <th className="px-2 py-2 font-semibold">Duration</th>
                    <th className="px-2 py-2 font-semibold">Latency</th>
                    <th className="px-2 py-2 font-semibold">Failure category</th>
                  </tr>
                </thead>
                <tbody>
                  {data.recent_calls.map((call) => (
                    <tr
                      key={call.call_id}
                      className="border-b border-slate-300/30 last:border-0 dark:border-slate-700/30"
                    >
                      <td className="text-muted-foreground px-2 py-2.5 font-mono text-xs">
                        {call.call_id}
                      </td>
                      <td className="text-muted-foreground px-2 py-2.5 text-xs whitespace-nowrap">
                        {hydrated
                          ? formatLocalDateTime(call.ended_at)
                          : formatDateTime(call.ended_at)}
                      </td>
                      <td className="px-2 py-2.5">
                        {badge(CHANNEL_STYLES[call.channel], call.channel)}
                      </td>
                      <td className="px-2 py-2.5">
                        {call.outcome === 'success'
                          ? badge(
                              'bg-emerald-500/10 text-emerald-700 ring-emerald-500/25 dark:text-emerald-300',
                              'Successful'
                            )
                          : badge(
                              'bg-rose-500/10 text-rose-700 ring-rose-500/25 dark:text-rose-300',
                              'Failed'
                            )}
                      </td>
                      <td className="text-muted-foreground px-2 py-2.5 text-xs">
                        {call.reason ? (REASON_LABELS[call.reason] ?? call.reason) : '—'}
                      </td>
                      <td className="text-muted-foreground px-2 py-2.5 font-mono text-xs whitespace-nowrap">
                        {formatDuration(call.duration_s)}
                      </td>
                      <td className="text-muted-foreground px-2 py-2.5 font-mono text-xs whitespace-nowrap">
                        {formatLatency(call.avg_latency_s)}
                      </td>
                      <td className="px-2 py-2.5">
                        {call.failure_category ? (
                          badge(
                            CATEGORY_STYLES[call.failure_category],
                            CATEGORY_LABELS[call.failure_category]
                          )
                        ) : (
                          <span className="text-muted-foreground text-xs">—</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <p className="text-muted-foreground mt-5 text-center text-xs leading-5">
            The dashboard updates automatically every {POLL_INTERVAL_MS / 1000} seconds. All numbers
            and charts come from real recorded call outcomes. No transcripts, medical details, or
            private caller information are ever stored or shown.
          </p>
        </section>
      </div>
    </main>
  );
}
