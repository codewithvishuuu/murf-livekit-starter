import { NextResponse } from 'next/server';
import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import path from 'node:path';

export const dynamic = 'force-dynamic';

/**
 * Day 7 optional — resolution callback trigger for the admin Human Support
 * Queue.
 *
 * POST form fields: `reference_id` (required), `force` ("1" to explicitly
 * retrigger a callback that was already made).
 *
 * Runs the backend's own CLI (`backend/src/escalations.py callback REF`) so
 * all safety rules live in one place: only RESOLVED requests, only callers
 * with a dialable number, duplicate-callback protection, and the existing
 * Day 6 outbound dialer. Redirects back to /admin with a result marker.
 */
async function findScriptPath(): Promise<string | null> {
  const candidates = [
    process.env.ESCALATIONS_SRC_PATH,
    path.resolve(process.cwd(), '..', 'backend', 'src', 'escalations.py'),
    path.resolve(process.cwd(), 'backend', 'src', 'escalations.py'),
  ].filter((entry): entry is string => Boolean(entry));
  for (const candidate of candidates) {
    if (existsSync(candidate)) {
      return candidate;
    }
  }
  return null;
}

const PYTHON_CANDIDATES: string[][] = [['python'], ['python3'], ['py', '-3']];

function runBackend(scriptPath: string, args: string[]): { status: number; stdout: string } | null {
  for (const candidate of PYTHON_CANDIDATES) {
    const result = spawnSync(candidate[0], [...candidate.slice(1), scriptPath, ...args], {
      encoding: 'utf-8',
      timeout: 90_000,
      windowsHide: true,
    });
    if (result.status !== null) {
      return { status: result.status, stdout: result.stdout ?? '' };
    }
  }
  return null;
}

export async function POST(request: Request) {
  const formData = await request.formData();
  const referenceId = String(formData.get('reference_id') ?? '').trim();
  const force = String(formData.get('force') ?? '') === '1';
  const base = new URL(request.url);

  if (!referenceId) {
    return NextResponse.redirect(
      new URL('/admin?callback=error&reason=missing+reference+id', base)
    );
  }

  const scriptPath = await findScriptPath();
  if (!scriptPath) {
    return NextResponse.redirect(new URL('/admin?callback=error&reason=backend+not+found', base));
  }

  const args = ['callback', referenceId];
  if (force) {
    args.push('--force');
  }
  const result = runBackend(scriptPath, args);
  const ok = result?.status === 0;
  if (ok) {
    return NextResponse.redirect(new URL('/admin?callback=ok', base));
  }

  const firstLine = (result?.stdout ?? '').split('\n')[0].trim();
  const reason = firstLine.replace(/^FAILURE\s+/, '') || 'callback could not be requested';
  return NextResponse.redirect(
    new URL(`/admin?callback=error&reason=${encodeURIComponent(reason)}`, base)
  );
}
