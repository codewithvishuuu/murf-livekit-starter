import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import type { SafeReminder } from '@/lib/reminders';
import { ReminderStatusLookup } from './reminder-status';

function record(
  referenceId: string,
  status: SafeReminder['status'],
  message = 'Drink a glass of water.'
): SafeReminder {
  return {
    reference_id: referenceId,
    status,
    scheduled_at: '2026-08-15T10:05:00+00:00',
    message,
    created_at: '2026-08-15T09:55:00+00:00',
  };
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function mockFetch(
  records: Record<string, SafeReminder>,
  cancelResponse?: { ok: boolean; error?: string }
) {
  const fetchMock = vi.fn(async (input: string) => {
    if (input.includes('/api/reminders/cancel')) {
      if (cancelResponse?.ok) {
        return jsonResponse({ ok: true });
      }
      return jsonResponse(
        { ok: false, error: cancelResponse?.error ?? 'not pending' },
        cancelResponse ? 409 : 500
      );
    }
    const url = new URL(String(input), 'http://localhost');
    const ref = url.searchParams.get('ref') ?? '';
    const reminder = records[ref];
    if (!reminder) {
      return jsonResponse({ found: false });
    }
    return jsonResponse({ found: true, reminder });
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

const PENDING = record('REM-20260815-001', 'pending');
const TRIGGERED = record('REM-20260815-002', 'triggered');
const COMPLETED = record('REM-20260815-003', 'completed');
const FAILED = record('REM-20260815-004', 'failed');
const CANCELLED = record('REM-20260815-005', 'cancelled');

const ALL_RECORDS = [PENDING, TRIGGERED, COMPLETED, FAILED, CANCELLED];

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

async function submitReference(referenceId: string) {
  fireEvent.change(screen.getByLabelText('Reference ID'), {
    target: { value: referenceId },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Check Status' }));
  await act(async () => {
    await vi.advanceTimersByTimeAsync(400);
  });
}

describe('ReminderStatusLookup', () => {
  it('shows a pending reminder with a Cancel Reminder button', async () => {
    vi.useFakeTimers();
    mockFetch({ [PENDING.reference_id]: PENDING });
    render(<ReminderStatusLookup reminders={ALL_RECORDS} />);

    await submitReference(PENDING.reference_id);

    expect(screen.getByText(PENDING.reference_id)).toBeTruthy();
    expect(screen.getByText('Pending')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Cancel Reminder' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Refresh Status' })).toBeTruthy();
  });

  it.each([
    ['Triggered', TRIGGERED],
    ['Completed', COMPLETED],
    ['Failed', FAILED],
    ['Cancelled', CANCELLED],
  ] as const)('does not show a Cancel button for a %s reminder', async (label, reminder) => {
    vi.useFakeTimers();
    mockFetch({ [reminder.reference_id]: reminder });
    render(<ReminderStatusLookup reminders={ALL_RECORDS} />);

    await submitReference(reminder.reference_id);

    expect(screen.getByText(label)).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Cancel Reminder' })).toBeNull();
  });

  it('asks for confirmation before cancelling and shows Cancelled afterwards', async () => {
    vi.useFakeTimers();
    const records: Record<string, SafeReminder> = { [PENDING.reference_id]: PENDING };
    const fetchMock = mockFetch(records, { ok: true });
    render(<ReminderStatusLookup reminders={ALL_RECORDS} />);

    await submitReference(PENDING.reference_id);
    fireEvent.click(screen.getByRole('button', { name: 'Cancel Reminder' }));

    expect(screen.getByText('Are you sure you want to cancel this reminder?')).toBeTruthy();
    expect(screen.getByText('Keep reminder')).toBeTruthy();

    records[PENDING.reference_id] = { ...PENDING, status: 'cancelled' };
    fireEvent.click(screen.getByRole('button', { name: 'Yes, cancel reminder' }));
    await act(async () => {
      await Promise.resolve();
    });

    expect(screen.getByText('Cancelled')).toBeTruthy();
    expect(screen.queryByText('Are you sure you want to cancel this reminder?')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Cancel Reminder' })).toBeNull();

    const cancelCalls = fetchMock.mock.calls.filter(([url]) =>
      String(url).includes('/api/reminders/cancel')
    );
    expect(cancelCalls).toHaveLength(1);
    const [, init] = cancelCalls[0] as [string, RequestInit];
    expect(JSON.parse(String(init?.body))).toEqual({ reference_id: PENDING.reference_id });
  });

  it('keeps the reminder and shows no error when the confirmation is declined', async () => {
    vi.useFakeTimers();
    mockFetch({ [PENDING.reference_id]: PENDING });
    render(<ReminderStatusLookup reminders={ALL_RECORDS} />);

    await submitReference(PENDING.reference_id);
    fireEvent.click(screen.getByRole('button', { name: 'Cancel Reminder' }));
    fireEvent.click(screen.getByRole('button', { name: 'Keep reminder' }));

    expect(screen.queryByText('Are you sure you want to cancel this reminder?')).toBeNull();
    expect(screen.getByText('Pending')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Cancel Reminder' })).toBeTruthy();
  });

  it('shows the backend error when cancellation is refused (e.g. already triggered)', async () => {
    vi.useFakeTimers();
    const records: Record<string, SafeReminder> = { [PENDING.reference_id]: PENDING };
    const fetchMock = mockFetch(records, {
      ok: false,
      error: 'REM-20260815-001 could not be cancelled (not pending)',
    });
    render(<ReminderStatusLookup reminders={ALL_RECORDS} />);

    await submitReference(PENDING.reference_id);
    fireEvent.click(screen.getByRole('button', { name: 'Cancel Reminder' }));
    fireEvent.click(screen.getByRole('button', { name: 'Yes, cancel reminder' }));
    await act(async () => {
      await Promise.resolve();
    });

    expect(screen.getByText('REM-20260815-001 could not be cancelled (not pending)')).toBeTruthy();
    expect(
      fetchMock.mock.calls.filter(([url]) => String(url).includes('/api/reminders/cancel'))
    ).toHaveLength(1);
  });

  it('refreshes to the latest status when Refresh Status is clicked', async () => {
    vi.useFakeTimers();
    const records: Record<string, SafeReminder> = { [PENDING.reference_id]: PENDING };
    mockFetch(records);
    render(<ReminderStatusLookup reminders={ALL_RECORDS} />);

    await submitReference(PENDING.reference_id);
    expect(screen.getByText('Pending')).toBeTruthy();

    records[PENDING.reference_id] = { ...PENDING, status: 'completed' };
    fireEvent.click(screen.getByRole('button', { name: 'Refresh Status' }));
    await act(async () => {
      await Promise.resolve();
    });

    expect(screen.getByText('Completed')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Cancel Reminder' })).toBeNull();
  });

  it('auto-refreshes every 10 seconds while pending and stops after a terminal status', async () => {
    vi.useFakeTimers();
    const records: Record<string, SafeReminder> = { [PENDING.reference_id]: PENDING };
    const fetchMock = mockFetch(records);
    render(<ReminderStatusLookup reminders={ALL_RECORDS} />);

    await submitReference(PENDING.reference_id);
    expect(screen.getByText('Auto-refreshing every 10 seconds')).toBeTruthy();

    const lookupCalls = () =>
      fetchMock.mock.calls.filter(([url]) =>
        String(url).includes('/api/reminders?ref=REM-20260815-001')
      ).length;

    const afterSubmit = lookupCalls();
    expect(afterSubmit).toBe(0);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(lookupCalls()).toBe(1);
    expect(screen.getByText('Pending')).toBeTruthy();

    records[PENDING.reference_id] = { ...PENDING, status: 'completed' };
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(lookupCalls()).toBe(2);
    expect(screen.getByText('Completed')).toBeTruthy();
    expect(screen.queryByText('Auto-refreshing every 10 seconds')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Cancel Reminder' })).toBeNull();

    const afterTerminal = lookupCalls();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(lookupCalls()).toBe(afterTerminal);
  });

  it('shows Reminder not found for an unknown reference ID', async () => {
    vi.useFakeTimers();
    mockFetch({});
    render(<ReminderStatusLookup reminders={ALL_RECORDS} />);

    await submitReference('REM-99999999-999');

    expect(screen.getByText('Reminder not found.')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Check Status' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Cancel Reminder' })).toBeNull();
  });
});
