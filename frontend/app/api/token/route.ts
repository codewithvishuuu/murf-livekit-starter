import { cookies } from 'next/headers';
import { NextResponse } from 'next/server';
import { AccessToken, type AccessTokenOptions, type VideoGrant } from 'livekit-server-sdk';
import { randomUUID } from 'node:crypto';
import { RoomConfiguration } from '@livekit/protocol';

type ConnectionDetails = {
  serverUrl: string;
  roomName: string;
  participantName: string;
  participantToken: string;
};

// NOTE: you are expected to define the following environment variables in `.env.local`:
const API_KEY = process.env.LIVEKIT_API_KEY;
const API_SECRET = process.env.LIVEKIT_API_SECRET;
const LIVEKIT_URL = process.env.LIVEKIT_URL;
const AGENT_NAME = process.env.AGENT_NAME;

// don't cache the results
export const revalidate = 0;

const CALLER_ID_COOKIE = 'aarogya_caller_id';
const CALLER_ID_PREFIX = 'aarogya_caller_';
const CALLER_ID_MAX_AGE = 60 * 60 * 24 * 365; // one year

// The caller's stable identity, so the agent can recognize a returning caller
// across separate calls. The first call creates an ID and stores it in a
// cookie; later calls reuse it. Rooms themselves stay unique per call.
async function resolveCallerIdentity(): Promise<string> {
  const store = await cookies();
  const existing = store.get(CALLER_ID_COOKIE)?.value;
  if (existing && existing.startsWith(CALLER_ID_PREFIX)) {
    return existing;
  }
  const identity = `${CALLER_ID_PREFIX}${randomUUID()}`;
  store.set(CALLER_ID_COOKIE, identity, {
    httpOnly: true,
    sameSite: 'lax',
    path: '/',
    maxAge: CALLER_ID_MAX_AGE,
  });
  return identity;
}

export async function POST(req: Request) {
  try {
    if (LIVEKIT_URL === undefined) {
      throw new Error('LIVEKIT_URL is not defined');
    }
    if (API_KEY === undefined) {
      throw new Error('LIVEKIT_API_KEY is not defined');
    }
    if (API_SECRET === undefined) {
      throw new Error('LIVEKIT_API_SECRET is not defined');
    }

    // Parse room config from request body (if provided).
    const body = await req.json().catch(() => ({}));
    let roomConfig: RoomConfiguration | undefined;
    if (body?.room_config) {
      roomConfig = RoomConfiguration.fromJson(body.room_config, { ignoreUnknownFields: true });
    } else if (AGENT_NAME) {
      // When AGENT_NAME is set, configure explicit agent dispatch so the named
      // agent worker picks up the job when a user joins the room.
      roomConfig = RoomConfiguration.fromJson(
        { agents: [{ agentName: AGENT_NAME }] },
        { ignoreUnknownFields: true }
      );
    }

    // Generate participant token
    const participantName = 'user';
    const participantIdentity = await resolveCallerIdentity();
    const roomName = `voice_assistant_room_${Math.floor(Math.random() * 10_000)}`;

    const participantToken = await createParticipantToken(
      { identity: participantIdentity, name: participantName },
      roomName,
      roomConfig
    );

    // Return connection details
    const data: ConnectionDetails = {
      serverUrl: LIVEKIT_URL,
      roomName,
      participantName,
      participantToken,
    };
    const headers = new Headers({
      'Cache-Control': 'no-store',
    });
    return NextResponse.json(data, { headers });
  } catch (error) {
    if (error instanceof Error) {
      console.error(error);
      return new NextResponse(error.message, { status: 500 });
    }
  }
}

function createParticipantToken(
  userInfo: AccessTokenOptions,
  roomName: string,
  roomConfig?: RoomConfiguration
): Promise<string> {
  const at = new AccessToken(API_KEY, API_SECRET, {
    ...userInfo,
    ttl: '15m',
  });
  const grant: VideoGrant = {
    room: roomName,
    roomJoin: true,
    canPublish: true,
    canPublishData: true,
    canSubscribe: true,
  };
  at.addGrant(grant);

  if (roomConfig) {
    at.roomConfig = roomConfig;
  }

  return at.toJwt();
}
