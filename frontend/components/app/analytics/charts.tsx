'use client';

import type { CallsOverTimePoint } from '@/lib/analytics';

const WIDTH = 640;
const HEIGHT = 200;
const PAD_TOP = 24;
const PAD_BOTTOM = 26;
const PAD_SIDE = 10;
const PLOT_HEIGHT = HEIGHT - PAD_TOP - PAD_BOTTOM;

/**
 * Calls per day over the last 14 days, successful (emerald) stacked with
 * failed (rose). Pure SVG — no chart library — with hover tooltips via
 * <title>. Fully responsive via viewBox.
 */
export function CallsOverTimeChart({ points }: { points: CallsOverTimePoint[] }) {
  const max = Math.max(1, ...points.map((point) => point.total));
  const slot = (WIDTH - PAD_SIDE * 2) / Math.max(1, points.length);
  const barWidth = Math.max(6, slot * 0.62);
  const baseline = PAD_TOP + PLOT_HEIGHT;

  return (
    <svg
      viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
      className="h-auto w-full"
      role="img"
      aria-label="Calls over the last 14 days"
    >
      <line
        x1={PAD_SIDE}
        y1={baseline}
        x2={WIDTH - PAD_SIDE}
        y2={baseline}
        className="stroke-slate-300 dark:stroke-slate-700"
        strokeWidth={1}
      />
      {points.map((point, index) => {
        const center = PAD_SIDE + slot * index + slot / 2;
        const x = center - barWidth / 2;
        const totalHeight = max === 0 ? 0 : (point.total / max) * PLOT_HEIGHT;
        const successHeight = max === 0 ? 0 : (point.successful / max) * PLOT_HEIGHT;
        const successY = baseline - totalHeight;
        const failedY = successY + successHeight;
        const showLabel = index % 2 === 0;
        return (
          <g key={point.date}>
            <rect
              x={x}
              y={successY}
              width={barWidth}
              height={Math.max(successHeight, point.successful > 0 ? 2 : 0)}
              rx={3}
              className="fill-emerald-500/80 dark:fill-emerald-400/80"
            >
              <title>{`${point.date}: ${point.successful} successful, ${point.failed} failed`}</title>
            </rect>
            {point.failed > 0 && (
              <rect
                x={x}
                y={failedY}
                width={barWidth}
                height={Math.max(point.failed > 0 ? 2 : 0, totalHeight - successHeight)}
                rx={3}
                className="fill-rose-500/70 dark:fill-rose-400/70"
              >
                <title>{`${point.date}: ${point.successful} successful, ${point.failed} failed`}</title>
              </rect>
            )}
            {showLabel && (
              <text
                x={center}
                y={baseline + 16}
                textAnchor="middle"
                className="fill-slate-500 text-[10px] dark:fill-slate-400"
              >
                {point.date.slice(5)}
              </text>
            )}
          </g>
        );
      })}
    </svg>
  );
}

/**
 * Successful vs failed calls as a horizontal stacked bar with counts.
 * Pure divs, responsive, and readable on small screens.
 */
export function OutcomeSplitBar({ successful, failed }: { successful: number; failed: number }) {
  const total = successful + failed;
  const successPct = total === 0 ? 0 : (successful / total) * 100;
  const failedPct = total === 0 ? 0 : (failed / total) * 100;

  return (
    <div className="flex w-full flex-col gap-4">
      <div className="flex h-10 w-full overflow-hidden rounded-xl ring-1 ring-slate-300/40 dark:ring-slate-700/50">
        <div
          className="flex items-center justify-start bg-emerald-500/80 pl-3 transition-[width] duration-500 dark:bg-emerald-400/80"
          style={{ width: `${successPct}%` }}
          title={`Successful: ${successful}`}
        />
        <div
          className="bg-rose-500/70 transition-[width] duration-500 dark:bg-rose-400/70"
          style={{ width: `${failedPct}%` }}
          title={`Failed: ${failed}`}
        />
      </div>
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
        <span className="flex items-center gap-2">
          <span className="h-2.5 w-2.5 rounded-full bg-emerald-500/80 dark:bg-emerald-400/80" />
          <span className="text-muted-foreground font-medium">Successful</span>
          <span className="font-mono font-bold">{successful}</span>
        </span>
        <span className="flex items-center gap-2">
          <span className="h-2.5 w-2.5 rounded-full bg-rose-500/70 dark:bg-rose-400/70" />
          <span className="text-muted-foreground font-medium">Failed</span>
          <span className="font-mono font-bold">{failed}</span>
        </span>
        {total === 0 && (
          <span className="text-muted-foreground text-xs">No calls recorded yet</span>
        )}
      </div>
    </div>
  );
}
