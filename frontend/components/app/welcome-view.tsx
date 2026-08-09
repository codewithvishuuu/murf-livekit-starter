import { HeartPulseIcon, Loader2Icon, MicOffIcon, ShieldCheckIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/shadcn/utils';

interface WelcomeViewProps {
  startButtonText: string;
  onStartCall: () => void;
  title?: string;
  description?: string;
  helperText?: string;
  startButtonDisabled?: boolean;
  startButtonLoading?: boolean;
  hideSetupHelp?: boolean;
  secondaryAction?: () => void;
  secondaryButtonText?: string;
  variant?: 'ready' | 'connecting' | 'mic-permission' | 'call-ended';
  callDuration?: string;
}

export const WelcomeView = ({
  startButtonText,
  onStartCall,
  title = 'Ready to start',
  description = 'Get general health information and guidance through voice.',
  helperText,
  startButtonDisabled = false,
  startButtonLoading = false,
  hideSetupHelp = false,
  secondaryAction,
  secondaryButtonText,
  variant = 'ready',
  callDuration,
  className,
  ref,
}: React.ComponentProps<'div'> & WelcomeViewProps) => {
  const isConnecting = variant === 'connecting';
  const isPermissionError = variant === 'mic-permission';
  const isCallEnded = variant === 'call-ended';

  return (
    <div ref={ref} className={cn('h-full w-full overflow-y-auto', className)}>
      <div className="flex min-h-full w-full flex-col items-center justify-center py-4 sm:py-6">
        <section className="bg-background/95 mx-auto flex w-full max-w-2xl flex-col items-center justify-center rounded-[26px] border border-emerald-500/10 px-5 py-7 text-center shadow-[0_24px_80px_-36px_rgba(15,23,42,0.35)] backdrop-blur sm:px-8 sm:py-9 lg:px-10">
          <div
            className={cn(
              'mb-4 flex h-16 w-16 items-center justify-center rounded-full ring-1',
              isPermissionError
                ? 'bg-rose-500/10 text-rose-700 ring-rose-500/20 dark:text-rose-300'
                : isConnecting
                  ? 'bg-amber-500/10 text-amber-700 ring-amber-500/20 dark:text-amber-300'
                  : 'bg-emerald-500/10 text-emerald-700 ring-emerald-500/20 dark:text-emerald-300'
            )}
          >
            {isConnecting ? (
              <Loader2Icon className="size-8 animate-spin" />
            ) : isPermissionError ? (
              <MicOffIcon className="size-8" />
            ) : (
              <HeartPulseIcon className="size-8" />
            )}
          </div>

          <p className="text-foreground text-2xl font-semibold tracking-tight sm:text-3xl">
            Aarogya Sahayak
          </p>
          <p className="text-muted-foreground mt-2 max-w-prose text-sm leading-6 sm:text-base">
            AI Health Access Assistant
          </p>
          <p className="text-foreground mt-4 max-w-prose text-base leading-7 font-semibold sm:text-lg">
            {title}
          </p>
          <p className="text-muted-foreground mt-2 max-w-prose text-sm leading-6 sm:text-base">
            {description}
          </p>

          {helperText && (
            <div className="mt-5 w-full max-w-md rounded-2xl border border-emerald-500/15 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-800 dark:text-emerald-300">
              {helperText}
            </div>
          )}

          {isCallEnded && callDuration && (
            <div className="mt-5 flex w-full max-w-sm items-center justify-between gap-4 rounded-2xl border border-emerald-500/15 bg-emerald-500/5 px-4 py-3.5 shadow-sm">
              <p className="text-muted-foreground text-xs font-medium tracking-[0.2em] uppercase">
                Conversation duration
              </p>
              <p className="text-foreground text-2xl font-semibold tabular-nums">{callDuration}</p>
            </div>
          )}

          <div className="mt-7 flex w-full max-w-sm flex-col gap-3 sm:flex-row sm:justify-center">
            <Button
              size="lg"
              disabled={startButtonDisabled}
              onClick={onStartCall}
              className="inline-flex min-h-12 flex-1 items-center justify-center gap-2 rounded-full px-6 text-sm font-semibold"
            >
              {startButtonLoading && <Loader2Icon className="size-4 animate-spin" />}
              {startButtonText}
            </Button>
            {secondaryAction && secondaryButtonText && (
              <Button
                size="lg"
                variant="outline"
                onClick={secondaryAction}
                className="inline-flex min-h-12 flex-1 items-center justify-center gap-2 rounded-full px-6 text-sm font-semibold"
              >
                {secondaryButtonText}
              </Button>
            )}
          </div>

          <div className="mt-5 flex items-center justify-center gap-2 rounded-full border border-emerald-500/20 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-700 dark:text-emerald-300">
            <ShieldCheckIcon className="size-4 shrink-0" />
            <span className="leading-5">General health guidance only. Not a doctor.</span>
          </div>
        </section>

        {!hideSetupHelp && (
          <div className="mt-4 flex w-full items-center justify-center px-4 text-center">
            <p className="text-muted-foreground max-w-prose text-xs leading-5 font-normal text-pretty sm:text-sm">
              For the best experience, allow microphone access and speak clearly.
            </p>
          </div>
        )}
      </div>
    </div>
  );
};
