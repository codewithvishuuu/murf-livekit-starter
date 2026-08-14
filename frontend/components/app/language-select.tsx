'use client';

import { useEffect, useRef } from 'react';
import { CheckIcon, LanguagesIcon, XIcon } from 'lucide-react';
import { AnimatePresence, motion } from 'motion/react';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/shadcn/utils';

export type PreferredLanguage = 'en' | 'hi';

interface LanguageSelectProps {
  open: boolean;
  selected: PreferredLanguage | null;
  onSelect: (language: PreferredLanguage) => void;
  onClose: () => void;
}

interface LanguageOption {
  value: PreferredLanguage;
  label: string;
  sublabel: string;
}

const LANGUAGE_OPTIONS: LanguageOption[] = [
  { value: 'en', label: 'English', sublabel: 'Conversation in English' },
  { value: 'hi', label: 'हिन्दी', sublabel: 'बातचीत हिन्दी में' },
];

export const LanguageSelect = ({ open, selected, onSelect, onClose }: LanguageSelectProps) => {
  const panelRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) {
      return;
    }
    const previouslyFocused = document.activeElement as HTMLElement | null;
    const firstOption = panelRef.current?.querySelector<HTMLButtonElement>('button[data-language]');
    firstOption?.focus();

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation();
        onClose();
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('keydown', handleKeyDown);
      previouslyFocused?.focus();
    };
  }, [open, onClose]);

  return (
    <AnimatePresence>
      {open && (
        <div
          role="presentation"
          className="fixed inset-0 z-50 flex items-center justify-center overflow-y-auto px-4 py-6 sm:px-6"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) {
              onClose();
            }
          }}
        >
          <motion.div
            aria-hidden="true"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.25, ease: 'easeOut' }}
            className="bg-background/60 fixed inset-0 backdrop-blur-sm"
          />
          <motion.div
            ref={panelRef}
            role="dialog"
            aria-modal="true"
            aria-labelledby="language-select-title"
            aria-describedby="language-select-description"
            initial={{ opacity: 0, scale: 0.96, y: 12 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.96, y: 12 }}
            transition={{ duration: 0.25, ease: 'easeOut' }}
            className="bg-background/95 relative w-full max-w-md rounded-[26px] border border-emerald-500/10 p-6 shadow-[0_24px_80px_-36px_rgba(15,23,42,0.45)] backdrop-blur sm:p-7"
          >
            <button
              type="button"
              onClick={onClose}
              aria-label="Close language selection"
              className="text-muted-foreground hover:text-foreground focus-visible:ring-ring/50 absolute top-4 right-4 flex size-8 items-center justify-center rounded-full transition-colors outline-none hover:bg-emerald-500/10 focus-visible:ring-[3px]"
            >
              <XIcon className="size-4" />
            </button>

            <div className="mb-5 flex size-12 items-center justify-center rounded-full bg-emerald-500/10 text-emerald-700 ring-1 ring-emerald-500/20 dark:text-emerald-300">
              <LanguagesIcon className="size-6" />
            </div>

            <h2
              id="language-select-title"
              className="text-foreground text-xl font-semibold tracking-tight sm:text-2xl"
            >
              Choose your language
            </h2>
            <p
              id="language-select-description"
              className="text-muted-foreground mt-2 text-sm leading-6 sm:text-base"
            >
              Select the language you&apos;d like to use during your conversation.
            </p>

            <div className="mt-6 flex flex-col gap-3">
              {LANGUAGE_OPTIONS.map((option) => {
                const isSelected = selected === option.value;
                return (
                  <Button
                    key={option.value}
                    data-language={option.value}
                    type="button"
                    size="lg"
                    variant="outline"
                    aria-pressed={isSelected}
                    onClick={() => onSelect(option.value)}
                    className={cn(
                      'group/option h-auto flex-col items-start gap-1 rounded-2xl px-5 py-4 text-left shadow-xs',
                      'border-emerald-500/15 bg-emerald-500/5',
                      'transition-[border-color,background-color,box-shadow,transform] duration-200 ease-out',
                      'hover:-translate-y-px hover:border-emerald-500/30 hover:bg-emerald-500/10',
                      'focus-visible:border-emerald-500/40 focus-visible:ring-emerald-500/25',
                      isSelected &&
                        'border-emerald-500/60 bg-emerald-500/15 shadow-[0_0_0_1px_rgba(16,185,129,0.25)] hover:border-emerald-500/60 hover:bg-emerald-500/15'
                    )}
                  >
                    <span className="flex w-full items-center justify-between gap-3">
                      <span className="text-foreground text-base leading-6 font-semibold">
                        {option.label}
                      </span>
                      <span
                        aria-hidden="true"
                        className={cn(
                          'flex size-6 shrink-0 items-center justify-center rounded-full transition-all duration-200 ease-out',
                          isSelected
                            ? 'scale-100 bg-emerald-500 text-emerald-50 opacity-100'
                            : 'scale-75 bg-emerald-500/10 text-emerald-700 opacity-0 dark:text-emerald-300'
                        )}
                      >
                        <CheckIcon className="size-3.5" strokeWidth={3} />
                      </span>
                    </span>
                    <span className="text-muted-foreground text-xs leading-5 font-normal">
                      {option.sublabel}
                    </span>
                  </Button>
                );
              })}
            </div>

            <div className="mt-6 flex justify-center">
              <Button
                type="button"
                variant="ghost"
                onClick={onClose}
                className="rounded-full px-5 text-sm font-semibold"
              >
                Back
              </Button>
            </div>
          </motion.div>
        </div>
      )}
    </AnimatePresence>
  );
};
