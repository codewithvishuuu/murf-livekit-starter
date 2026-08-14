'use client';

import { useCallback, useMemo, useRef, useState } from 'react';
import { TokenSource } from 'livekit-client';
import { useSession } from '@livekit/components-react';
import { WarningIcon } from '@phosphor-icons/react/dist/ssr';
import type { AppConfig } from '@/app-config';
import { AgentSessionProvider } from '@/components/agents-ui/agent-session-provider';
import type { PreferredLanguage } from '@/components/app/language-select';
import { ViewController } from '@/components/app/view-controller';
import { Toaster } from '@/components/ui/sonner';
import { useAgentErrors } from '@/hooks/useAgentErrors';
import { useDebugMode } from '@/hooks/useDebug';
import { getSandboxTokenSource } from '@/lib/utils';

const IN_DEVELOPMENT = process.env.NODE_ENV !== 'production';

function AppSetup() {
  useDebugMode({ enabled: IN_DEVELOPMENT });
  useAgentErrors();

  return null;
}

interface AppProps {
  appConfig: AppConfig;
}

export function App({ appConfig }: AppProps) {
  // The caller's chosen conversation language (English or Hindi). The value
  // is picked in the language-selection modal before the call starts and
  // stays fixed for the whole session. The ref is written synchronously in
  // the selection handler so the token fetch (which happens when the call
  // starts) always reads the current value; the state is used to render the
  // UI (the modal is only shown once per session).
  const [preferredLanguage, setPreferredLanguage] = useState<PreferredLanguage | null>(null);
  const preferredLanguageRef = useRef<PreferredLanguage | null>(null);

  const handleSelectLanguage = useCallback((language: PreferredLanguage) => {
    preferredLanguageRef.current = language;
    setPreferredLanguage(language);
  }, []);

  const tokenSource = useMemo(() => {
    if (typeof process.env.NEXT_PUBLIC_CONN_DETAILS_ENDPOINT === 'string') {
      return getSandboxTokenSource(appConfig, preferredLanguageRef);
    }
    // A fresh token is fetched every time a call starts, so the caller's
    // language selection is always included in the participant metadata
    // ({"preferred_language": "en" | "hi"}) the agent reads on join.
    return TokenSource.literal(async () => {
      const res = await fetch('/api/token', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          participant_metadata: preferredLanguageRef.current
            ? JSON.stringify({ preferred_language: preferredLanguageRef.current })
            : undefined,
          room_config: appConfig.agentName
            ? { agents: [{ agentName: appConfig.agentName }] }
            : undefined,
        }),
      });
      if (!res.ok) {
        throw new Error(`Error generating token from /api/token: received ${res.status}`);
      }
      return res.json();
    });
  }, [appConfig]);

  const session = useSession(tokenSource);

  return (
    <AgentSessionProvider session={session}>
      <AppSetup />
      <main className="flex h-full min-h-0 w-full items-center justify-center overflow-hidden bg-[radial-gradient(circle_at_top,_rgba(16,185,129,0.12),_transparent_55%)] px-3 py-3 sm:px-4 sm:py-4 lg:px-6">
        <div className="h-full w-full max-w-6xl">
          <ViewController
            appConfig={appConfig}
            selectedLanguage={preferredLanguage}
            onSelectLanguage={handleSelectLanguage}
          />
        </div>
      </main>
      <Toaster
        icons={{
          warning: <WarningIcon weight="bold" />,
        }}
        position="top-center"
        className="toaster group"
        style={
          {
            '--normal-bg': 'var(--popover)',
            '--normal-text': 'var(--popover-foreground)',
            '--normal-border': 'var(--border)',
          } as React.CSSProperties
        }
      />
    </AgentSessionProvider>
  );
}
