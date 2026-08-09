'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useTheme } from 'next-themes';
import { Track } from 'livekit-client';
import { AnimatePresence, motion } from 'motion/react';
import { useSessionContext } from '@livekit/components-react';
import type { AppConfig } from '@/app-config';
import { AgentSessionView_01 } from '@/components/agents-ui/blocks/agent-session-view-01';
import { WelcomeView } from '@/components/app/welcome-view';

function formatConversationDuration(durationMs: number) {
  const totalSeconds = Math.max(0, Math.floor(durationMs / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;

  return `${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`;
}

const MotionWelcomeView = motion.create(WelcomeView);
const MotionSessionView = motion.create(AgentSessionView_01);

const VIEW_MOTION_PROPS = {
  variants: {
    visible: {
      opacity: 1,
    },
    hidden: {
      opacity: 0,
    },
  },
  initial: 'hidden',
  animate: 'visible',
  exit: 'hidden',
  transition: {
    duration: 0.5,
    ease: 'linear',
  },
};

interface ViewControllerProps {
  appConfig: AppConfig;
}

type UiState = 'READY' | 'CONNECTING' | 'CALL_ENDED' | 'MIC_PERMISSION_ERROR';

function isMicrophonePermissionError(error: unknown) {
  if (!(error instanceof Error)) {
    return false;
  }

  const name = error.name.toLowerCase();
  const message = error.message.toLowerCase();

  return (
    name.includes('notallowed') ||
    name.includes('permission') ||
    message.includes('permission') ||
    message.includes('notallowed') ||
    message.includes('microphone access') ||
    message.includes('denied')
  );
}

export function ViewController({ appConfig }: ViewControllerProps) {
  const { isConnected, start, end } = useSessionContext();
  const { resolvedTheme } = useTheme();
  const [uiState, setUiState] = useState<UiState>('READY');
  const [statusMessage, setStatusMessage] = useState<string | undefined>(undefined);
  const [conversationDuration, setConversationDuration] = useState('00:00');
  const [isStartPending, setIsStartPending] = useState(false);
  const wasConnectedRef = useRef(false);
  const disconnectIntentRef = useRef(false);
  const connectingTimeoutRef = useRef<number | null>(null);
  const sessionStartTimeRef = useRef<number | null>(null);

  const clearConnectingTimeout = useCallback(() => {
    if (connectingTimeoutRef.current !== null) {
      window.clearTimeout(connectingTimeoutRef.current);
      connectingTimeoutRef.current = null;
    }
  }, []);

  useEffect(() => {
    return () => {
      clearConnectingTimeout();
    };
  }, [clearConnectingTimeout]);

  useEffect(() => {
    if (isConnected) {
      clearConnectingTimeout();
      setIsStartPending(false);
      setUiState('READY');
      setStatusMessage(undefined);
      wasConnectedRef.current = true;
      return;
    }

    if (wasConnectedRef.current) {
      clearConnectingTimeout();
      setIsStartPending(false);
      const endedAt = Date.now();
      const durationMs =
        sessionStartTimeRef.current !== null ? endedAt - sessionStartTimeRef.current : 0;
      setConversationDuration(formatConversationDuration(durationMs));
      setUiState('CALL_ENDED');
      setStatusMessage(
        disconnectIntentRef.current
          ? undefined
          : 'Call disconnected unexpectedly. Please check your internet and start again.'
      );
      disconnectIntentRef.current = false;
      wasConnectedRef.current = false;
    }
  }, [clearConnectingTimeout, isConnected]);

  const handleStartCall = useCallback(async () => {
    if (isConnected || isStartPending) {
      return;
    }

    sessionStartTimeRef.current = Date.now();
    setConversationDuration('00:00');
    setUiState('CONNECTING');
    setStatusMessage(undefined);
    setIsStartPending(true);
    disconnectIntentRef.current = false;
    clearConnectingTimeout();

    connectingTimeoutRef.current = window.setTimeout(() => {
      setIsStartPending(false);
      setUiState('READY');
      setStatusMessage("Couldn't connect yet. Please check your network and try again.");
      sessionStartTimeRef.current = null;
    }, 20_000);

    try {
      await Promise.resolve(start());
    } catch (error) {
      clearConnectingTimeout();
      setIsStartPending(false);
      sessionStartTimeRef.current = null;

      if (isMicrophonePermissionError(error)) {
        setUiState('MIC_PERMISSION_ERROR');
        setStatusMessage(undefined);
        return;
      }

      setUiState('READY');
      setStatusMessage("We couldn't connect right now. Please try again.");
    }
  }, [clearConnectingTimeout, isConnected, isStartPending, start]);

  const handleDisconnectIntent = useCallback(() => {
    disconnectIntentRef.current = true;
  }, []);

  const handleBackToReady = useCallback(() => {
    clearConnectingTimeout();
    setIsStartPending(false);
    setConversationDuration('00:00');
    setUiState('READY');
    setStatusMessage(undefined);
    disconnectIntentRef.current = false;
    sessionStartTimeRef.current = null;
  }, [clearConnectingTimeout]);

  const handleInCallDeviceError = useCallback(
    ({ source, error }: { source: Track.Source; error: Error }) => {
      if (source === Track.Source.Microphone && isMicrophonePermissionError(error)) {
        disconnectIntentRef.current = true;
        clearConnectingTimeout();
        setIsStartPending(false);
        setUiState('MIC_PERMISSION_ERROR');
        setStatusMessage(undefined);
        end();
      }
    },
    [clearConnectingTimeout, end]
  );

  return (
    <AnimatePresence mode="wait">
      {isConnected ? (
        <MotionSessionView
          key="session-view"
          {...VIEW_MOTION_PROPS}
          supportsChatInput={appConfig.supportsChatInput}
          supportsVideoInput={appConfig.supportsVideoInput}
          supportsScreenShare={appConfig.supportsScreenShare}
          isPreConnectBufferEnabled={appConfig.isPreConnectBufferEnabled}
          audioVisualizerType={appConfig.audioVisualizerType}
          audioVisualizerColor={
            resolvedTheme === 'dark'
              ? appConfig.audioVisualizerColorDark
              : appConfig.audioVisualizerColor
          }
          audioVisualizerColorShift={appConfig.audioVisualizerColorShift}
          audioVisualizerBarCount={appConfig.audioVisualizerBarCount}
          audioVisualizerGridRowCount={appConfig.audioVisualizerGridRowCount}
          audioVisualizerGridColumnCount={appConfig.audioVisualizerGridColumnCount}
          audioVisualizerRadialBarCount={appConfig.audioVisualizerRadialBarCount}
          audioVisualizerRadialRadius={appConfig.audioVisualizerRadialRadius}
          audioVisualizerWaveLineWidth={appConfig.audioVisualizerWaveLineWidth}
          onDisconnectIntent={handleDisconnectIntent}
          onDeviceError={handleInCallDeviceError}
          className="w-full"
        />
      ) : (
        <MotionWelcomeView
          key={uiState}
          {...VIEW_MOTION_PROPS}
          className="w-full"
          variant={
            uiState === 'MIC_PERMISSION_ERROR'
              ? 'mic-permission'
              : uiState === 'CONNECTING'
                ? 'connecting'
                : uiState === 'CALL_ENDED'
                  ? 'call-ended'
                  : 'ready'
          }
          title={
            uiState === 'MIC_PERMISSION_ERROR'
              ? 'Microphone access is needed'
              : uiState === 'CONNECTING'
                ? 'Connecting to Aarogya Sahayak...'
                : uiState === 'CALL_ENDED'
                  ? 'Conversation ended'
                  : 'Ready to start'
          }
          description={
            uiState === 'MIC_PERMISSION_ERROR'
              ? 'Please allow microphone access in your browser settings, then try again.'
              : uiState === 'CONNECTING'
                ? 'Please wait while we connect you to Aarogya Sahayak.'
                : uiState === 'CALL_ENDED'
                  ? 'Your health matters. We’re here whenever you need guidance.'
                  : 'Get general health information and wellness guidance through a simple voice conversation.'
          }
          helperText={statusMessage}
          startButtonText={
            uiState === 'MIC_PERMISSION_ERROR'
              ? 'Try Again'
              : uiState === 'CALL_ENDED'
                ? 'Start Again'
                : uiState === 'CONNECTING'
                  ? 'Connecting...'
                  : appConfig.startButtonText
          }
          onStartCall={handleStartCall}
          secondaryAction={
            uiState === 'MIC_PERMISSION_ERROR' || uiState === 'CONNECTING'
              ? handleBackToReady
              : undefined
          }
          secondaryButtonText={
            uiState === 'MIC_PERMISSION_ERROR' || uiState === 'CONNECTING' ? 'Back' : undefined
          }
          startButtonDisabled={uiState === 'CONNECTING' || isStartPending}
          startButtonLoading={uiState === 'CONNECTING' || isStartPending}
          hideSetupHelp={uiState === 'CONNECTING' || uiState === 'MIC_PERMISSION_ERROR'}
          callDuration={uiState === 'CALL_ENDED' ? conversationDuration : undefined}
        />
      )}
    </AnimatePresence>
  );
}
