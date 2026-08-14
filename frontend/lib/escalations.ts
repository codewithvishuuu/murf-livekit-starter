import { existsSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import path from 'node:path';

/**
 * A human-help escalation record, exactly as stored by the backend
 * (`backend/src/escalations.py`), read from its JSON mirror.
 */
export interface EscalationRecord {
  id: number;
  reference_id: string;
  caller_id: string | null;
  summary: string;
  what_happened: string;
  agent_checked: string | null;
  urgency: string;
  language: string | null;
  preferred_follow_up: string | null;
  status: string;
  created_at: string;
  resolved_callback_at?: string | null;
  resolved_callback_count?: number | null;
}

const CANDIDATE_PATHS = [
  process.env.ESCALATIONS_JSON_PATH,
  // Running from frontend/ (the normal development layout)
  path.resolve(process.cwd(), '..', 'backend', 'data', 'escalations.json'),
  // Running from the repository root
  path.resolve(process.cwd(), 'backend', 'data', 'escalations.json'),
].filter((entry): entry is string => Boolean(entry));

/** Read the escalation queue (newest first). Never throws; empty list on failure. */
export async function getEscalations(): Promise<EscalationRecord[]> {
  for (const candidate of CANDIDATE_PATHS) {
    if (!existsSync(candidate)) {
      continue;
    }
    try {
      const raw = await readFile(candidate, 'utf-8');
      const parsed: unknown = JSON.parse(raw);
      if (Array.isArray(parsed)) {
        return parsed as EscalationRecord[];
      }
    } catch {
      // Unreadable or malformed mirror: try the next candidate.
    }
  }
  return [];
}
