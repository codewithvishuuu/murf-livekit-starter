import Link from 'next/link';
import { ShieldCheckIcon, SparklesIcon } from 'lucide-react';
import { WELLNESS_DISCLAIMER, WELLNESS_TIP_CATEGORIES } from '@/lib/wellness-tips';

export const metadata = {
  title: 'Wellness Tips',
};

export default function WellnessTipsPage() {
  return (
    <main className="flex h-full min-h-0 w-full justify-center overflow-y-auto bg-[radial-gradient(circle_at_top,_rgba(16,185,129,0.12),_transparent_55%)] px-3 py-6 sm:px-4 lg:px-6">
      <div className="w-full max-w-2xl">
        <div className="mb-5 flex items-center justify-between gap-3">
          <div className="flex items-center gap-2.5">
            <div className="flex h-10 w-10 items-center justify-center rounded-full bg-emerald-500/10 text-emerald-700 ring-1 ring-emerald-500/20 dark:text-emerald-300">
              <SparklesIcon className="size-5" />
            </div>
            <div>
              <h1 className="text-foreground text-lg font-semibold tracking-tight sm:text-xl">
                Wellness Tips
              </h1>
              <p className="text-muted-foreground text-xs leading-5">
                Simple tips for hydration, sleep & healthy habits
              </p>
            </div>
          </div>
          <Link
            href="/"
            className="text-muted-foreground hover:text-foreground text-xs font-semibold underline underline-offset-4"
          >
            Back to conversation
          </Link>
        </div>

        <div className="bg-background/95 w-full rounded-3xl border border-emerald-500/10 px-5 py-6 shadow-[0_12px_40px_-24px_rgba(15,23,42,0.35)] backdrop-blur sm:px-6">
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            {WELLNESS_TIP_CATEGORIES.map((category) => {
              const Icon = category.icon;
              return (
                <section
                  key={category.id}
                  className="rounded-2xl border border-emerald-500/10 bg-emerald-500/5 px-4 py-4"
                >
                  <div className="flex items-center gap-2.5">
                    <div className="flex size-8 shrink-0 items-center justify-center rounded-full bg-emerald-500/10 text-emerald-700 ring-1 ring-emerald-500/20 dark:text-emerald-300">
                      <Icon className="size-4" />
                    </div>
                    <div className="min-w-0">
                      <h2 className="text-foreground text-sm font-semibold tracking-tight">
                        {category.title}
                      </h2>
                      <p className="text-muted-foreground truncate text-[11px] leading-4">
                        {category.description}
                      </p>
                    </div>
                  </div>
                  <ul className="mt-3 space-y-2.5">
                    {category.tips.map((tip, index) => (
                      <li
                        key={index}
                        className="text-muted-foreground flex gap-2 text-xs leading-5"
                      >
                        <span className="mt-1.5 size-1 shrink-0 rounded-full bg-emerald-500/60" />
                        {tip}
                      </li>
                    ))}
                  </ul>
                </section>
              );
            })}
          </div>

          <p className="text-muted-foreground mt-6 flex items-center justify-center gap-1.5 text-center text-xs leading-5">
            <ShieldCheckIcon className="size-3.5 shrink-0" />
            {WELLNESS_DISCLAIMER}
          </p>
        </div>
      </div>
    </main>
  );
}
