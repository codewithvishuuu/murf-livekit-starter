'use client';

import React, { useState } from 'react';
import { Track } from 'livekit-client';
import { MicIcon, Volume2Icon } from 'lucide-react';
import { AnimatePresence, type MotionProps, motion } from 'motion/react';
import {
  useAgent,
  useSessionContext,
  useSessionMessages,
  useVoiceAssistant,
} from '@livekit/components-react';
import { AgentAudioVisualizerBar } from '@/components/agents-ui/agent-audio-visualizer-bar';
import { AgentChatTranscript } from '@/components/agents-ui/agent-chat-transcript';
import {
  AgentControlBar,
  type AgentControlBarControls,
} from '@/components/agents-ui/agent-control-bar';
import { Shimmer } from '@/components/ai-elements/shimmer';
import { cn } from '@/lib/shadcn/utils';

const MotionMessage = motion.create(Shimmer);

const CHAT_MOTION_PROPS: MotionProps = {
  variants: {
    hidden: {
      opacity: 0,
      transition: {
        ease: 'easeOut',
        duration: 0.3,
      },
    },
    visible: {
      opacity: 1,
      transition: {
        delay: 0.2,
        ease: 'easeOut',
        duration: 0.3,
      },
    },
  },
  initial: 'hidden',
  animate: 'visible',
  exit: 'hidden',
};

const SHIMMER_MOTION_PROPS: MotionProps = {
  variants: {
    visible: {
      opacity: 1,
      transition: {
        ease: 'easeIn',
        duration: 0.5,
        delay: 0.8,
      },
    },
    hidden: {
      opacity: 0,
      transition: {
        ease: 'easeIn',
        duration: 0.5,
        delay: 0,
      },
    },
  },
  initial: 'hidden',
  animate: 'visible',
  exit: 'hidden',
};

export interface AgentSessionView_01Props {
  /**
   * Message shown above the controls before the first chat message is sent.
   *
   * @default 'Agent is listening, ask it a question'
   */
  preConnectMessage?: string;
  /**
   * Enables or disables the chat toggle and transcript input controls.
   *
   * @default true
   */
  supportsChatInput?: boolean;
  /**
   * Enables or disables camera controls in the bottom control bar.
   *
   * @default true
   */
  supportsVideoInput?: boolean;
  /**
   * Enables or disables screen sharing controls in the bottom control bar.
   *
   * @default true
   */
  supportsScreenShare?: boolean;
  /**
   * Shows a pre-connect buffer state with a shimmer message before messages appear.
   *
   * @default true
   */
  isPreConnectBufferEnabled?: boolean;

  /** Selects the visualizer style rendered in the main tile area. */
  audioVisualizerType?: 'bar' | 'wave' | 'grid' | 'radial' | 'aura';
  /** Primary hex color used by supported audio visualizer variants. */
  audioVisualizerColor?: `#${string}`;
  /** Hue shift intensity used by certain visualizers. */
  audioVisualizerColorShift?: number;
  /** Number of bars to render when `audioVisualizerType` is `bar`. */
  audioVisualizerBarCount?: number;
  /** Number of rows in the visualizer when `audioVisualizerType` is `grid`. */
  audioVisualizerGridRowCount?: number;
  /** Number of columns in the visualizer when `audioVisualizerType` is `grid`. */
  audioVisualizerGridColumnCount?: number;
  /** Number of radial bars when `audioVisualizerType` is `radial`. */
  audioVisualizerRadialBarCount?: number;
  /** Base radius of the radial visualizer when `audioVisualizerType` is `radial`. */
  audioVisualizerRadialRadius?: number;
  /** Stroke width of the wave path when `audioVisualizerType` is `wave`. */
  audioVisualizerWaveLineWidth?: number;
  /** Optional class name merged onto the outer `<section>` container. */
  className?: string;
  /** Callback when the user intentionally ends the current call. */
  onDisconnectIntent?: () => void;
  /** Callback for media device errors from in-call controls. */
  onDeviceError?: (error: { source: Track.Source; error: Error }) => void;
}

function getAgentStatusContent(agentState?: string) {
  switch (agentState) {
    case 'speaking':
      return {
        title: 'Aarogya Sahayak is speaking',
        subtitle: 'The assistant is responding now.',
        badge: 'Speaks',
        accent: 'border-sky-600/20 bg-sky-500/10 text-sky-700 dark:text-sky-300',
        dot: 'bg-sky-600 animate-pulse',
      };
    case 'thinking':
      return {
        title: 'Aarogya Sahayak is thinking',
        subtitle: 'Preparing a response for you.',
        badge: 'Thinking',
        accent: 'border-amber-600/20 bg-amber-500/10 text-amber-700 dark:text-amber-300',
        dot: 'bg-amber-600 animate-pulse',
      };
    case 'connecting':
    case 'initializing':
    case 'pre-connect-buffering':
      return {
        title: 'Connecting...',
        subtitle: 'Please wait while we connect you to Aarogya Sahayak.',
        badge: 'Connecting',
        accent: 'border-amber-600/20 bg-amber-500/10 text-amber-700 dark:text-amber-300',
        dot: 'bg-amber-600 animate-pulse',
      };
    default:
      return {
        title: 'Listening to you',
        subtitle: 'Speak naturally about your symptoms, wellness, or health concern.',
        badge: 'Listening',
        accent: 'border-emerald-600/20 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
        dot: 'bg-emerald-600 animate-pulse',
      };
  }
}

export function AgentSessionView_01({
  preConnectMessage = 'Ask about symptoms, hydration, sleep, or healthy habits',
  supportsChatInput = true,
  supportsVideoInput = true,
  supportsScreenShare = true,
  isPreConnectBufferEnabled = true,
  audioVisualizerType,
  audioVisualizerColor,
  audioVisualizerColorShift,
  audioVisualizerBarCount,
  audioVisualizerGridRowCount,
  audioVisualizerGridColumnCount,
  audioVisualizerRadialBarCount,
  audioVisualizerRadialRadius,
  audioVisualizerWaveLineWidth,
  onDisconnectIntent,
  onDeviceError,
  ref,
  className,
  ...props
}: React.ComponentProps<'section'> & AgentSessionView_01Props) {
  const session = useSessionContext();
  const { messages } = useSessionMessages(session);
  const [chatOpen, setChatOpen] = useState(false);
  const { state: agentState } = useAgent();
  const { audioTrack: agentAudioTrack } = useVoiceAssistant();
  const statusContent = getAgentStatusContent(agentState);

  const controls: AgentControlBarControls = {
    leave: true,
    microphone: true,
    chat: supportsChatInput,
    camera: supportsVideoInput,
    screenShare: supportsScreenShare,
  };

  return (
    <section
      ref={ref}
      className={cn(
        'bg-background relative z-10 mx-auto flex h-full w-full max-w-3xl flex-col overflow-hidden rounded-[28px] border border-emerald-500/10 shadow-[0_24px_80px_-36px_rgba(15,23,42,0.35)] sm:rounded-[32px]',
        className
      )}
      {...props}
    >
      {/* Status card — part of the normal page structure */}
      <div className="shrink-0 px-3 pt-3 sm:px-4 sm:pt-4">
        <div
          className={cn(
            'flex items-center gap-3 rounded-2xl border px-3 py-2.5 shadow-sm backdrop-blur sm:px-4 sm:py-3',
            statusContent.accent
          )}
        >
          <div className="bg-background/70 flex h-10 w-10 shrink-0 items-center justify-center rounded-full">
            <AnimatePresence initial={false} mode="popLayout">
              <motion.span
                key={agentState}
                initial={{ opacity: 0, scale: 0.6 }}
                animate={{ opacity: 1, scale: 1 }}
                exit={{ opacity: 0, scale: 0.6 }}
                transition={{ duration: 0.15 }}
                className="flex"
              >
                {agentState === 'speaking' ? (
                  <Volume2Icon className="size-5" />
                ) : (
                  <MicIcon className="size-5" />
                )}
              </motion.span>
            </AnimatePresence>
          </div>
          <div className="min-w-0 flex-1 text-left">
            <p className="truncate text-sm font-semibold">{statusContent.title}</p>
            <p className="truncate text-xs leading-5 opacity-80">{statusContent.subtitle}</p>
          </div>
          <AgentAudioVisualizerBar
            size="sm"
            state={agentState}
            audioTrack={agentAudioTrack}
            color={audioVisualizerColor}
            barCount={audioVisualizerBarCount ?? 4}
            className="h-8 shrink-0 items-center gap-0 sm:gap-[2px]"
          >
            <span className="h-full min-h-[6px] w-1 rounded-full bg-current/25 transition-colors duration-250 ease-linear data-[lk-highlighted=true]:bg-current" />
          </AgentAudioVisualizerBar>
          <span className={cn('size-3 shrink-0 rounded-full', statusContent.dot)} aria-hidden />
        </div>
      </div>

      {/* Transcript — scrolls inside the available space */}
      <div className="flex min-h-0 flex-1 flex-col">
        {chatOpen ? (
          <motion.div
            {...CHAT_MOTION_PROPS}
            className="flex h-full w-full flex-col overflow-hidden"
          >
            <AgentChatTranscript
              agentState={agentState}
              messages={messages}
              className="mx-auto w-full max-w-3xl"
            />
          </motion.div>
        ) : (
          <div className="flex min-h-0 flex-1 items-center justify-center px-6">
            <p className="text-muted-foreground/70 max-w-xs text-center text-xs leading-5">
              {messages.length === 0
                ? 'Your live conversation with Aarogya Sahayak will appear here.'
                : 'The conversation is running. Tap the message icon in the bottom bar to view it.'}
            </p>
          </div>
        )}
      </div>

      {/* Bottom panel */}
      <div className="bg-background/95 shrink-0 border-t border-emerald-500/10 px-3 pt-3 pb-3 backdrop-blur sm:px-4 sm:pt-4 sm:pb-4">
        {isPreConnectBufferEnabled && (
          <AnimatePresence>
            {messages.length === 0 && (
              <MotionMessage
                key="pre-connect-message"
                duration={2}
                aria-hidden={messages.length > 0}
                {...SHIMMER_MOTION_PROPS}
                className="pointer-events-none mx-auto block w-full max-w-2xl pb-3 text-center text-sm font-semibold"
              >
                {preConnectMessage}
              </MotionMessage>
            )}
          </AnimatePresence>
        )}
        <AgentControlBar
          variant="livekit"
          controls={controls}
          isChatOpen={chatOpen}
          isConnected={session.isConnected}
          onDisconnect={() => {
            onDisconnectIntent?.();
            session.end();
          }}
          onDeviceError={onDeviceError}
          onIsChatOpenChange={setChatOpen}
        />
      </div>
    </section>
  );
}
