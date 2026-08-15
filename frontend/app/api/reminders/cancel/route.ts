import { NextResponse } from 'next/server';
import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import path from 'node:path';

export const dynamic = 'force-dynamic';

const REFERENCE_ID_RE = /^REM-\d{8}-\d{3}$/i;

/**
 * Cancel a still-pending reminder (Scheduled Reminder page).
 *
 * POST JSON body: { "reference_id": "REM-YYYYMMDD-NNN" }
 *
 * Runs the backend's own CLI (`backend/src/reminders.py cancel REF`) so
 * the existing atomic store logic lives in one place: only `pending`
 * reminders can be cancelled (an UPDATE guarded by status), so an
 * already-claimed/triggered/completed/failed reminder can never be
 * cancelled incorrectly. No second store, no direct file writes.
 */
async function findScriptPath(): Promise<string | null> {
  const candidates = [
    process.env.REMINDERS_SRC_PATH,
    path.resolve(process.cwd(), '..', 'backend', 'src', 'reminders.py'),
    path.resolve(process.cwd(), 'backend', 'src', 'reminders.py'),
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
      timeout: 30_000,
      windowsHide: true,
    });
    if (result.status !== null) {
      return { status: result.status, stdout: result.stdout ?? '' };
    }
  }
  return null;
}

export async function POST(request: Request) {
  let referenceId: unknown;
  try {
    referenceId = (await request.json()).reference_id;
  } catch {
    return NextResponse.json({ ok: false, error: 'Missing reference ID.' }, { status: 400 });
  }

  const needle = String(referenceId ?? '').trim();
  if (!REFERENCE_ID_RE.test(needle)) {
    return NextResponse.json({ ok: false, error: 'Invalid reference ID.' }, { status: 400 });
  }

  const scriptPath = await findScriptPath();
  if (!scriptPath) {
    return NextResponse.json(
      { ok: false, error: 'Reminder backend could not be found.' },
      { status: 503 }
    );
  }

  const result = runBackend(scriptPath, ['cancel', needle]);
  if (result?.status === 0) {
    return NextResponse.json({ ok: true, reference_id: needle });
  }

  const firstLine = (result?.stdout ?? '').split('\n')[0].trim();
  const error = firstLine.replace(/^FAILURE\s+/, '') || 'The reminder could not be cancelled.';
  return NextResponse.json({ ok: false, error }, { status: 409 });
}
