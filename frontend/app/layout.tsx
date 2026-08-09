import { Public_Sans } from 'next/font/google';
import localFont from 'next/font/local';
import { headers } from 'next/headers';
import { HeartPulseIcon } from 'lucide-react';
import { ThemeProvider } from '@/components/app/theme-provider';
import { ThemeToggle } from '@/components/app/theme-toggle';
import { cn } from '@/lib/shadcn/utils';
import { getAppConfig, getStyles } from '@/lib/utils';
import '@/styles/globals.css';

const publicSans = Public_Sans({
  variable: '--font-public-sans',
  subsets: ['latin'],
});

const commitMono = localFont({
  display: 'swap',
  variable: '--font-commit-mono',
  src: [
    {
      path: '../fonts/CommitMono-400-Regular.otf',
      weight: '400',
      style: 'normal',
    },
    {
      path: '../fonts/CommitMono-700-Regular.otf',
      weight: '700',
      style: 'normal',
    },
    {
      path: '../fonts/CommitMono-400-Italic.otf',
      weight: '400',
      style: 'italic',
    },
    {
      path: '../fonts/CommitMono-700-Italic.otf',
      weight: '700',
      style: 'italic',
    },
  ],
});

interface RootLayoutProps {
  children: React.ReactNode;
}

export default async function RootLayout({ children }: RootLayoutProps) {
  const hdrs = await headers();
  const appConfig = await getAppConfig(hdrs);
  const styles = getStyles(appConfig);
  const { pageTitle, pageDescription, companyName } = appConfig;

  return (
    <html
      lang="en"
      suppressHydrationWarning
      className={cn(
        publicSans.variable,
        commitMono.variable,
        'scroll-smooth font-sans antialiased'
      )}
    >
      <head>
        {styles && <style>{styles}</style>}
        <title>{pageTitle}</title>
        <meta name="description" content={pageDescription} />
      </head>
      <body className="overflow-x-hidden">
        <ThemeProvider
          attribute="class"
          defaultTheme="system"
          enableSystem
          disableTransitionOnChange
        >
          <div className="flex h-dvh flex-col overflow-hidden">
            <header className="mx-auto flex w-full max-w-3xl shrink-0 items-center justify-between gap-3 px-4 py-3 sm:px-0">
              <div className="bg-background/80 flex items-center gap-2.5 rounded-full border border-emerald-500/15 py-1.5 pr-3.5 pl-1.5 shadow-sm backdrop-blur">
                <div className="flex h-7 w-7 items-center justify-center rounded-full bg-emerald-500/10 text-emerald-700 dark:text-emerald-300">
                  <HeartPulseIcon className="size-4" />
                </div>
                <span className="text-foreground text-sm font-semibold tracking-tight">
                  {companyName}
                </span>
              </div>
              <div className="flex items-center gap-3">
                <span className="text-muted-foreground hidden font-mono text-[11px] font-bold tracking-wider uppercase lg:inline">
                  Built with{' '}
                  <a
                    target="_blank"
                    rel="noopener noreferrer"
                    href="https://docs.livekit.io/agents"
                    className="underline underline-offset-4"
                  >
                    LiveKit Agents
                  </a>
                </span>
                <ThemeToggle />
              </div>
            </header>
            <div className="min-h-0 flex-1">{children}</div>
          </div>
        </ThemeProvider>
      </body>
    </html>
  );
}
