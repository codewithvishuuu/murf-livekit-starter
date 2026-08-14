import { existsSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import path from 'node:path';

/**
 * Aggregate call analytics, exactly as mirrored by the backend
 * (`backend/src/call_outcomes.py`) into its JSON mirror. The backend only
 * ever writes aggregate counts — never per-call details, transcripts, or
 * caller information.
 */
export interface CallOutcomes {
  total: number;
  successful: number;
  failed: number;
  updated_at: string | null;
}

/** One privacy-safe call-history row (never transcripts or caller content). */
export interface RecentCall {
  call_id: string;
  started_at: string;
  ended_at: string;
  channel: string;
  outcome: string;
  reason: string | null;
  duration_s: number | null;
  avg_latency_s: number | null;
  language: string | null;
  failure_category: string | null;
}

export interface ChannelBreakdown {
  channel: string;
  total: number;
  successful: number;
  failed: number;
}

export interface CallsOverTimePoint {
  date: string;
  total: number;
  successful: number;
  failed: number;
}

export interface FailureCategoryCount {
  category: string;
  count: number;
}

export interface LanguageCount {
  language: string;
  count: number;
}

/**
 * The full analytics dashboard payload mirrored by the backend into
 * `call_outcomes_analytics.json` and served (filtered, live) by
 * `/api/analytics`. Contains only whitelisted, non-sensitive analytics
 * metadata — never transcripts, medical details, or caller identity.
 */
export interface CallAnalytics {
  updated_at: string | null;
  total: number;
  successful: number;
  failed: number;
  success_rate: number;
  avg_latency_s: number | null;
  channels: ChannelBreakdown[];
  calls_over_time: CallsOverTimePoint[];
  failure_categories: FailureCategoryCount[];
  languages: LanguageCount[];
  recent_calls: RecentCall[];
}

const CANDIDATE_PATHS = [
  process.env.CALL_OUTCOMES_JSON_PATH,
  // Running from frontend/ (the normal development layout)
  path.resolve(process.cwd(), '..', 'backend', 'data', 'call_outcomes.json'),
  // Running from the repository root
  path.resolve(process.cwd(), 'backend', 'data', 'call_outcomes.json'),
].filter((entry): entry is string => Boolean(entry));

/**
 * Read the real call outcome counts from the backend mirror.
 * Never throws; zero-counts when the mirror is missing or unreadable.
 */
export async function getCallOutcomes(): Promise<CallOutcomes> {
  for (const candidate of CANDIDATE_PATHS) {
    if (!existsSync(candidate)) {
      continue;
    }
    try {
      const raw = await readFile(candidate, 'utf-8');
      const parsed: unknown = JSON.parse(raw);
      if (typeof parsed === 'object' && parsed !== null) {
        const record = parsed as Partial<CallOutcomes>;
        if (
          typeof record.total === 'number' &&
          typeof record.successful === 'number' &&
          typeof record.failed === 'number'
        ) {
          return {
            total: record.total,
            successful: record.successful,
            failed: record.failed,
            updated_at: record.updated_at ?? null,
          };
        }
      }
    } catch {
      // Unreadable or malformed mirror: try the next candidate.
    }
  }
  return { total: 0, successful: 0, failed: 0, updated_at: null };
}

const ANALYTICS_CANDIDATE_PATHS = [
  process.env.CALL_OUTCOMES_ANALYTICS_JSON_PATH,
  // Running from frontend/ (the normal development layout)
  path.resolve(process.cwd(), '..', 'backend', 'data', 'call_outcomes_analytics.json'),
  // Running from the repository root
  path.resolve(process.cwd(), 'backend', 'data', 'call_outcomes_analytics.json'),
].filter((entry): entry is string => Boolean(entry));

const EMPTY_ANALYTICS: CallAnalytics = {
  updated_at: null,
  total: 0,
  successful: 0,
  failed: 0,
  success_rate: 0,
  avg_latency_s: null,
  channels: [],
  calls_over_time: [],
  failure_categories: [],
  languages: [],
  recent_calls: [],
};

/**
 * Read the richer analytics payload from the backend mirror.
 * Never throws; an empty-safe payload when the mirror is missing or
 * malformed so the dashboard always renders.
 */
export async function getCallAnalytics(): Promise<CallAnalytics> {
  for (const candidate of ANALYTICS_CANDIDATE_PATHS) {
    if (!existsSync(candidate)) {
      continue;
    }
    try {
      const raw = await readFile(candidate, 'utf-8');
      const parsed: unknown = JSON.parse(raw);
      if (typeof parsed === 'object' && parsed !== null) {
        const record = parsed as Partial<CallAnalytics>;
        if (
          typeof record.total === 'number' &&
          typeof record.successful === 'number' &&
          typeof record.failed === 'number' &&
          typeof record.success_rate === 'number'
        ) {
          return {
            updated_at: record.updated_at ?? null,
            total: record.total,
            successful: record.successful,
            failed: record.failed,
            success_rate: record.success_rate,
            avg_latency_s: record.avg_latency_s ?? null,
            channels: Array.isArray(record.channels) ? record.channels : [],
            calls_over_time: Array.isArray(record.calls_over_time) ? record.calls_over_time : [],
            failure_categories: Array.isArray(record.failure_categories)
              ? record.failure_categories
              : [],
            languages: Array.isArray(record.languages) ? record.languages : [],
            recent_calls: Array.isArray(record.recent_calls) ? record.recent_calls : [],
          };
        }
      }
    } catch {
      // Unreadable or malformed mirror: try the next candidate.
    }
  }
  return EMPTY_ANALYTICS;
}
