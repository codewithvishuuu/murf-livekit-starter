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
