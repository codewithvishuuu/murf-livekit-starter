import { NextResponse } from 'next/server';
import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { type CallAnalytics, getCallAnalytics } from '@/lib/analytics';

export const dynamic = 'force-dynamic';
export const revalidate = 0;

/**
 * Analytics API for the live dashboard.
 *
 * Runs the backend's own query layer (`backend/src/call_outcomes.py`, pure
 * stdlib) against the real SQLite database so filters operate on stored call
 * rows — never on a pre-aggregated snapshot. The payload is the same
 * privacy-safe shape as the JSON mirror. If the backend CLI is unreachable,
 * falls back to the mirror snapshot so the dashboard never hard-fails.
 */
async function findScriptPath(): Promise<string | null> {
  const candidates = [
    process.env.CALL_OUTCOMES_SRC_PATH,
    path.resolve(process.cwd(), '..', 'backend', 'src', 'call_outcomes.py'),
    path.resolve(process.cwd(), 'backend', 'src', 'call_outcomes.py'),
  ].filter((entry): entry is string => Boolean(entry));
  for (const candidate of candidates) {
    if (existsSync(candidate)) {
      return candidate;
    }
  }
  return null;
}

const PYTHON_CANDIDATES: string[][] = [['python'], ['python3'], ['py', '-3']];

function runBackend(scriptPath: string, args: string[]): string | null {
  for (const candidate of PYTHON_CANDIDATES) {
    const result = spawnSync(candidate[0], [...candidate.slice(1), scriptPath, ...args], {
      encoding: 'utf-8',
      timeout: 15_000,
      windowsHide: true,
    });
    if (result.status === 0 && result.stdout) {
      return result.stdout;
    }
  }
  return null;
}

function parsePayload(raw: string): CallAnalytics | null {
  try {
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed === 'object' && parsed !== null) {
      const record = parsed as Partial<CallAnalytics>;
      if (typeof record.total === 'number' && typeof record.successful === 'number') {
        return record as CallAnalytics;
      }
    }
  } catch {
    // Malformed backend output: fall through to the mirror.
  }
  return null;
}

export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const channel = searchParams.get('channel') ?? undefined;
  const outcome = searchParams.get('outcome') ?? undefined;
  const dateFrom = searchParams.get('dateFrom') ?? undefined;
  const dateTo = searchParams.get('dateTo') ?? undefined;
  const language = searchParams.get('language') ?? undefined;
  const rawLimit = Number.parseInt(searchParams.get('limit') ?? '20', 10);
  const limit = Number.isFinite(rawLimit) ? Math.min(Math.max(rawLimit, 1), 100) : 20;

  const args = ['analytics', '--json', '--limit', String(limit)];
  if (channel) {
    args.push('--channel', channel);
  }
  if (outcome) {
    args.push('--outcome', outcome);
  }
  if (dateFrom) {
    args.push('--date-from', dateFrom);
  }
  if (dateTo) {
    args.push('--date-to', dateTo);
  }
  if (language) {
    args.push('--language', language);
  }

  const scriptPath = await findScriptPath();
  if (scriptPath) {
    const raw = runBackend(scriptPath, args);
    const payload = raw ? parsePayload(raw) : null;
    if (payload) {
      return NextResponse.json(payload, {
        headers: { 'Cache-Control': 'no-store, no-cache, must-revalidate' },
      });
    }
  }

  // Backend unavailable: serve the latest mirror snapshot (unfiltered) so
  // the dashboard keeps showing real data.
  const mirror = await getCallAnalytics();
  return NextResponse.json(mirror, {
    headers: { 'Cache-Control': 'no-store, no-cache, must-revalidate' },
  });
}
