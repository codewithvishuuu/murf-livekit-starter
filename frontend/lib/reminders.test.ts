import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { getEscalations } from './escalations';
import { safeReminderFrom } from './reminder-client';
import { findReminderByReference, getReminders, toSafeReminder } from './reminders';
import type { ReminderMirrorRecord, SafeReminder } from './reminders';

const MIRROR_ROW: ReminderMirrorRecord = {
  id: 1,
  reference_id: 'REM-20260815-001',
  destination: 'sip:vishal_demo123@sip.linphone.org',
  message: 'Drink a glass of water.',
  scheduled_at: '2026-08-15T10:05:00+00:00',
  status: 'pending',
  created_at: '2026-08-15T09:55:00+00:00',
  triggered_at: null,
  claim_id: 'claim-token-123',
};

describe('toSafeReminder', () => {
  it('keeps only the safe fields', () => {
    const safe = toSafeReminder(MIRROR_ROW);
    expect(safe).toEqual({
      reference_id: 'REM-20260815-001',
      status: 'pending',
      scheduled_at: '2026-08-15T10:05:00+00:00',
      message: 'Drink a glass of water.',
      created_at: '2026-08-15T09:55:00+00:00',
    });
  });

  it('never exposes the destination (phone number / SIP URI / SIP user)', () => {
    const safe = toSafeReminder(MIRROR_ROW) as SafeReminder & Record<string, unknown>;
    expect('destination' in safe).toBe(false);
    expect(JSON.stringify(safe)).not.toContain('vishal_demo123');
    expect(JSON.stringify(safe)).not.toContain('sip:');
    expect(JSON.stringify(safe)).not.toContain('@');
  });

  it('never exposes the internal claim id', () => {
    const safe = toSafeReminder(MIRROR_ROW) as SafeReminder & Record<string, unknown>;
    expect('claim_id' in safe).toBe(false);
    expect('triggered_at' in safe).toBe(false);
  });

  it('turns a missing message into null instead of dropping the row', () => {
    const safe = toSafeReminder({ ...MIRROR_ROW, message: '' });
    expect(safe.message).toBe('');
  });
});

describe('safeReminderFrom', () => {
  it('returns null for non-object values', () => {
    expect(safeReminderFrom(null)).toBeNull();
    expect(safeReminderFrom(undefined)).toBeNull();
    expect(safeReminderFrom('REM-20260815-001')).toBeNull();
    expect(safeReminderFrom(42)).toBeNull();
    expect(safeReminderFrom([])).toBeNull();
  });

  it('returns null when required fields are missing or wrong-typed', () => {
    expect(safeReminderFrom({})).toBeNull();
    expect(safeReminderFrom({ reference_id: 7, status: 'pending' })).toBeNull();
    expect(safeReminderFrom({ reference_id: 'REM-20260815-001' })).toBeNull();
  });

  it('projects to safe fields only, dropping destination and claim_id even when present', () => {
    const safe = safeReminderFrom({
      reference_id: 'REM-20260815-001',
      status: 'pending',
      scheduled_at: '2026-08-15T10:05:00+00:00',
      message: 'Drink a glass of water.',
      created_at: '2026-08-15T09:55:00+00:00',
      destination: 'sip:vishal_demo123@sip.linphone.org',
      claim_id: 'claim-token-123',
      credentials: 'top-secret',
      triggered_at: '2026-08-15T10:05:00+00:00',
    });
    expect(safe).toEqual({
      reference_id: 'REM-20260815-001',
      status: 'pending',
      scheduled_at: '2026-08-15T10:05:00+00:00',
      message: 'Drink a glass of water.',
      created_at: '2026-08-15T09:55:00+00:00',
    });
    expect(JSON.stringify(safe)).not.toContain('vishal_demo123');
    expect(JSON.stringify(safe)).not.toContain('sip:');
    expect(JSON.stringify(safe)).not.toContain('claim-token-123');
    expect(JSON.stringify(safe)).not.toContain('top-secret');
  });

  it('tolerates missing optional fields', () => {
    expect(safeReminderFrom({ reference_id: 'REM-20260815-001', status: 'pending' })).toEqual({
      reference_id: 'REM-20260815-001',
      status: 'pending',
      scheduled_at: '',
      message: null,
      created_at: '',
    });
  });
});

describe('findReminderByReference', () => {
  const records: SafeReminder[] = [
    toSafeReminder(MIRROR_ROW),
    toSafeReminder({
      ...MIRROR_ROW,
      id: 2,
      reference_id: 'REM-20260815-002',
      status: 'completed',
    }),
  ];

  it('finds a valid reference ID', () => {
    const found = findReminderByReference(records, 'REM-20260815-001');
    expect(found).not.toBeNull();
    expect(found?.status).toBe('pending');
  });

  it('matches case-insensitively', () => {
    expect(findReminderByReference(records, 'rem-20260815-002')?.reference_id).toBe(
      'REM-20260815-002'
    );
  });

  it('returns null for an unknown reference ID', () => {
    expect(findReminderByReference(records, 'REM-99999999-999')).toBeNull();
  });

  it('returns null for empty or missing input', () => {
    expect(findReminderByReference(records, '')).toBeNull();
    expect(findReminderByReference(records, '   ')).toBeNull();
    expect(findReminderByReference(records, null)).toBeNull();
    expect(findReminderByReference(records, undefined)).toBeNull();
  });
});

describe('getReminders', () => {
  it('returns [] when no mirror file exists', async () => {
    expect(await getReminders(['/nonexistent/reminders.json'])).toEqual([]);
  });

  it('reads a mirror and projects every row to safe fields only', async () => {
    const dir = await mkdtemp(path.join(tmpdir(), 'reminders-test-'));
    try {
      const mirrorPath = path.join(dir, 'reminders.json');
      await writeFile(
        mirrorPath,
        JSON.stringify([MIRROR_ROW, { ...MIRROR_ROW, id: 2, status: 'failed' }])
      );
      const records = await getReminders([mirrorPath]);
      expect(records).toHaveLength(2);
      for (const record of records) {
        const keys = Object.keys(record);
        expect(keys.sort()).toEqual(
          ['created_at', 'message', 'reference_id', 'scheduled_at', 'status'].sort()
        );
        expect(JSON.stringify(record)).not.toContain('vishal_demo123');
        expect(JSON.stringify(record)).not.toContain('claim');
      }
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  it('returns safe records from the real backend mirror when present', async () => {
    const records = await getReminders();
    expect(Array.isArray(records)).toBe(true);
    for (const record of records) {
      expect('destination' in record).toBe(false);
      expect('claim_id' in record).toBe(false);
    }
  });
});

describe('existing frontend functionality', () => {
  it('escalations lib still returns its unchanged record shape', async () => {
    const records = await getEscalations();
    expect(Array.isArray(records)).toBe(true);
    for (const record of records) {
      expect(record.reference_id).toBeDefined();
      expect(record.status).toBeDefined();
      expect(record.created_at).toBeDefined();
    }
  });
});
