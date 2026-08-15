import { afterEach, describe, expect, it, vi } from 'vitest';
import { cancelReminder, fetchReminderStatus } from './reminder-client';

const PENDING_SAFE = {
  reference_id: 'REM-20260815-001',
  status: 'pending',
  scheduled_at: '2026-08-15T10:05:00+00:00',
  message: 'Drink a glass of water.',
  created_at: '2026-08-15T09:55:00+00:00',
};

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('fetchReminderStatus', () => {
  it('returns the safe reminder when the API finds it', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse({
          found: true,
          reminder: {
            ...PENDING_SAFE,
            destination: 'sip:vishal_demo123@sip.linphone.org',
            claim_id: 'claim-token-123',
          },
        })
      )
    );
    const result = await fetchReminderStatus('REM-20260815-001');
    expect(result.found).toBe(true);
    expect(result.reminder).toEqual(PENDING_SAFE);
    expect(JSON.stringify(result.reminder)).not.toContain('vishal_demo123');
    expect(JSON.stringify(result.reminder)).not.toContain('claim-token-123');
  });

  it('asks the read-only endpoint with the reference ID', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ found: true, reminder: PENDING_SAFE }));
    vi.stubGlobal('fetch', fetchMock);
    await fetchReminderStatus(' REM-20260815-001 ');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const url = String(fetchMock.mock.calls[0]?.[0]);
    expect(url).toContain('/api/reminders?ref=REM-20260815-001');
    expect(fetchMock.mock.calls[0]?.[1]).toEqual({ cache: 'no-store' });
  });

  it('returns found=false for an empty reference', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    expect(await fetchReminderStatus('')).toEqual({ found: false });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('returns found=false when the API says not found', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse({ found: false }))
    );
    expect(await fetchReminderStatus('REM-99999999-999')).toEqual({ found: false });
  });

  it('returns found=false on HTTP errors and malformed bodies', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse({ found: true, reminder: PENDING_SAFE }, 500))
    );
    expect(await fetchReminderStatus('REM-20260815-001')).toEqual({ found: false });

    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse('not-json-object'))
    );
    expect(await fetchReminderStatus('REM-20260815-001')).toEqual({ found: false });

    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse({ found: true, reminder: {} }))
    );
    expect(await fetchReminderStatus('REM-20260815-001')).toEqual({ found: false });
  });

  it('returns found=false when the network fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => Promise.reject(new Error('offline')))
    );
    expect(await fetchReminderStatus('REM-20260815-001')).toEqual({ found: false });
  });
});

describe('cancelReminder', () => {
  it('returns ok when the backend confirms the cancellation', async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({ ok: true, reference_id: 'REM-20260815-001' })
    );
    vi.stubGlobal('fetch', fetchMock);
    const result = await cancelReminder('REM-20260815-001');
    expect(result).toEqual({ ok: true });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain('/api/reminders/cancel');
    expect(init?.method).toBe('POST');
    expect(JSON.parse(String(init?.body))).toEqual({ reference_id: 'REM-20260815-001' });
  });

  it('surfaces the backend error message on refusal', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse(
          { ok: false, error: 'REM-20260815-001 could not be cancelled (not pending)' },
          409
        )
      )
    );
    expect(await cancelReminder('REM-20260815-001')).toEqual({
      ok: false,
      error: 'REM-20260815-001 could not be cancelled (not pending)',
    });
  });

  it('falls back to a generic message when the response is malformed', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse('oops', 500))
    );
    expect(await cancelReminder('REM-20260815-001')).toEqual({
      ok: false,
      error: 'The reminder could not be cancelled.',
    });
  });

  it('reports a friendly error when the network fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => Promise.reject(new Error('offline')))
    );
    expect(await cancelReminder('REM-20260815-001')).toEqual({
      ok: false,
      error: 'Could not reach the reminder service.',
    });
  });
});
