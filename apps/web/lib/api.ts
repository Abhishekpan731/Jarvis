const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type User = { id: string; email: string; display_name: string };
/** Saved account preferences accepted by the preferences endpoint. */
export type Preferences = { timezone: string; briefing_language: "en" | "hi" };
/** Active session metadata, without credential values. */
export type UserSession = {
  id: string;
  user_agent: string | null;
  created_at: string;
  expires_at: string;
  current: boolean;
};
export type Conversation = {
  id: string;
  title: string;
  status: string;
  created_at: string;
  updated_at: string;
};
export type ChatMessage = {
  id: string;
  role: string;
  content: string;
  created_at: string;
};
export type SpeechStatus = {
  stt_configured: boolean;
  tts_configured: boolean;
  locales: string[];
  browser_capture: boolean;
};
export type VoiceSession = SpeechStatus & {
  locale: string;
  language: string;
  push_to_talk: boolean;
  wake_word: boolean;
  text_fallback: boolean;
};
/** Required fields returned by the authenticated Realtime session-mint endpoint. */
export type RealtimeSession = {
  provider: "openai";
  conversation_id: string;
  client_secret: string;
  expires_at: number;
  transport: "webrtc";
  model: string;
  locale: string;
  language: string;
};

export type RealtimeToolResult = CommandResult & {
  spoken?: string;
};
export type SpeakResult = {
  locale: string;
  language: string;
  text: string;
  truncated: boolean;
  audio: string | null;
  audio_format: string | null;
};
export type TranscribeResult = {
  locale: string;
  language: string;
  text: string;
};
export type IntegrationItem = {
  provider: string;
  name: string;
  configured: boolean;
  connection: {
    id: string;
    provider: string;
    status: string;
    connected: boolean;
    connected_at: string | null;
  } | null;
};
export type Notice = {
  id: string;
  kind: string;
  title: string;
  body: string;
  channel: string;
  read: boolean;
  created_at: string;
};
export type Reminder = {
  id: string;
  name: string;
  body: string;
  hour: number;
  minute: number;
  timezone: string;
  enabled: boolean;
};
export type CommandResult = {
  request_id: string;
  conversation_id: string;
  task_id: string | null;
  status: string;
  response_text: string;
  voice_response: string;
  requires_approval: boolean;
  source: string;
};
/** Task status, approval metadata, execution steps, and job state. */
export type TaskDetail = {
  id: string;
  title: string;
  status: string;
  created_at: string;
  approval_action_hash: string | null;
  approval_expires_at: string | null;
  error: "approval_expired" | "approval_rejected" | null;
  steps: Array<{
    position: number;
    agent_id: string;
    action: string;
    risk: string;
    status: string;
    attempt: number;
  }>;
  job: {
    id: string;
    status: string;
    attempt_count: number;
    last_error: string | null;
    cancel_requested: boolean;
  } | null;
};
export type SyncEvent = {
  id: number;
  kind: string;
  device_id: string | null;
  payload: Record<string, unknown>;
  created_at: string;
};

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
  }
}

function token(): string | null {
  return typeof window === "undefined"
    ? null
    : sessionStorage.getItem("jarvis_access");
}

export function saveToken(value: string): void {
  sessionStorage.setItem("jarvis_access", value);
}

export function clearToken(): void {
  sessionStorage.removeItem("jarvis_access");
}

export const REFRESH_PATH = "/api/v1/auth/refresh";

/** Refresh once, never on the refresh path, never after a retry. */
export function shouldRefresh(path: string, status: number, retried: boolean): boolean {
  return status === 401 && path !== REFRESH_PATH && !retried;
}

export const REALTIME_SESSION_PATH = "/api/v1/voice/realtime/session";
export const REALTIME_TOOL_PATH = "/api/v1/voice/realtime/tool";

/** Use the realtime peer only when mint returned a webrtc secret. */
export function canOpenRealtime(session: {
  client_secret?: string;
  transport?: string;
}): boolean {
  return Boolean(session.client_secret) && session.transport === "webrtc";
}

/** A 503 mint falls back. Other mint failures do not start the mic. */
export function mintFallsBack(status: number): boolean {
  return status === 503;
}
type ErrorBody = { error?: { code?: string; message?: string } };

async function readError(response: Response): Promise<ApiError> {
  const body = (await response.json().catch(() => ({}))) as ErrorBody;
  return new ApiError(
    response.status,
    body.error?.code ?? "error",
    body.error?.message ?? response.statusText,
  );
}

/** One in-flight refresh. A 401 must not start a second refresh. */
let refreshInFlight: Promise<string | null> | null = null;


async function refreshAccess(): Promise<string | null> {
  if (refreshInFlight) return refreshInFlight;
  refreshInFlight = refreshOnce().finally(() => {
    refreshInFlight = null;
  });
  return refreshInFlight;
}

async function refreshOnce(): Promise<string | null> {
  const response = await fetch(`${BASE}${REFRESH_PATH}`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
  });
  if (!response.ok) {
    clearToken();
    return null;
  }
  const body = (await response.json().catch(() => ({}))) as {
    access_token?: string;
  };
  if (!body.access_token) {
    clearToken();
    return null;
  }
  saveToken(body.access_token);
  return body.access_token;
}

async function authenticatedFetch(
  path: string,
  init: RequestInit = {},
  retried = false,
  usedToken: string | null = token(),
): Promise<Response> {
  const headers = new Headers(init.headers);
  if (!headers.has("Content-Type") && init.body && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  if (usedToken) headers.set("Authorization", `Bearer ${usedToken}`);
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers,
    credentials: "include",
  });
  if (!shouldRefresh(path, response.status, retried)) return response;
  const latestToken = token();
  if (latestToken && latestToken !== usedToken) {
    return authenticatedFetch(path, init, true, latestToken);
  }
  if (!(await refreshAccess())) return response;
  return authenticatedFetch(path, init, true, token());
}

async function request<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const response = await authenticatedFetch(path, init);
  if (!response.ok) throw await readError(response);
  return response.json();
}

export const api = {
  register: (email: string, password: string, displayName: string) =>
    request<{ access_token: string; user: User }>("/api/v1/auth/register", {
      method: "POST",
      body: JSON.stringify({ email, password, display_name: displayName }),
    }),
  login: (email: string, password: string) =>
    request<{ access_token: string }>("/api/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),
  session: () => request<{ user: User }>("/api/v1/auth/session"),
  /** Read the signed-in user's preferences. */
  preferences: () =>
    request<{ preferences: Preferences }>("/api/v1/auth/preferences"),
  /** Update supported preference fields. */
  updatePreferences: (updates: Partial<Preferences>) =>
    request<{ preferences: Preferences }>("/api/v1/auth/preferences", {
      method: "PATCH",
      body: JSON.stringify(updates),
    }),
  /** List the signed-in user's active sessions. */
  sessions: () =>
    request<{ sessions: UserSession[] }>("/api/v1/auth/sessions"),
  /** Revoke one session by its ID. */
  revokeSession: (id: string) =>
    request<{ revoked: boolean }>(`/api/v1/auth/sessions/${id}`, {
      method: "DELETE",
    }),
  logout: () =>
    request<{ signed_out: boolean }>("/api/v1/auth/logout", { method: "POST" }),
  health: () =>
    request<{
      status: string;
      ai: { configured: boolean; model: string | null };
      speech: SpeechStatus;
    }>("/api/v1/health"),
  voiceSession: (locale: string) =>
    request<VoiceSession>(
      `/api/v1/voice/session?locale=${encodeURIComponent(locale)}`,
    ),
  openRealtimeSession: (locale: string, conversationId: string) =>
    request<RealtimeSession>("/api/v1/voice/realtime/session", {
      method: "POST",
      body: JSON.stringify({
        locale,
        conversation_id: conversationId,
      }),
    }),
  submitRealtimeCommand: (
    conversationId: string,
    command: string,
    idempotencyKey: string,
    locale: string,
  ) =>
    request<RealtimeToolResult>("/api/v1/voice/realtime/tool", {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({
        conversation_id: conversationId,
        command,
        idempotency_key: idempotencyKey,
        locale,
        source: "web",
        input_type: "voice",
      }),
    }),
  speak: (text: string, locale: string) =>
    request<SpeakResult>("/api/v1/voice/speak", {
      method: "POST",
      body: JSON.stringify({ text, locale }),
    }),
  transcribe: async (audioBlob: Blob, locale: string): Promise<TranscribeResult> => {
    const form = new FormData();
    form.append("file", audioBlob);
    form.append("locale", locale);
    return request<TranscribeResult>("/api/v1/voice/transcribe", {
      method: "POST",
      body: form,
      headers: new Headers(),
    });
  },
  command: (
    command: string,
    locale: string,
    conversationId: string,
    idempotencyKey?: string,
  ) =>
    request<CommandResult>("/api/v1/assistant/commands", {
      method: "POST",
      headers: idempotencyKey
        ? { "Idempotency-Key": idempotencyKey }
        : undefined,
      body: JSON.stringify({
        command,
        source: "web",
        input_type: "voice",
        locale,
        conversation_id: conversationId,
      }),
    }),
  conversations: () =>
    request<{ conversations: Conversation[] }>("/api/v1/conversations"),
  createConversation: () =>
    request<Conversation>("/api/v1/conversations", {
      method: "POST",
      body: JSON.stringify({}),
    }),
  renameConversation: (id: string, title: string) =>
    request<Conversation>(`/api/v1/conversations/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    }),
  deleteConversation: (id: string) =>
    request<{ deleted: boolean }>(`/api/v1/conversations/${id}`, {
      method: "DELETE",
    }),
  messages: (id: string) =>
    request<{ messages: ChatMessage[] }>(
      `/api/v1/conversations/${id}/messages`,
    ),
  task: (id: string) => request<TaskDetail>(`/api/v1/tasks/${id}`),
  /** Approve a task using its current action hash. */
  approveTask: (id: string, actionHash: string) =>
    request<TaskDetail>(`/api/v1/tasks/${id}/approve`, {
      method: "POST",
      body: JSON.stringify({ action_hash: actionHash }),
    }),
  /** Reject a task using its current action hash. */
  rejectTask: (id: string, actionHash: string) =>
    request<TaskDetail>(`/api/v1/tasks/${id}/reject`, {
      method: "POST",
      body: JSON.stringify({ action_hash: actionHash }),
    }),
  sync: (after: number) =>
    request<{ events: SyncEvent[]; cursor: number }>(
      `/api/v1/sync?after=${after}&limit=100`,
    ),
  integrations: () =>
    request<{ integrations: IntegrationItem[] }>("/api/v1/integrations"),
  connectIntegration: (provider: string) =>
    request<{ authorize_url: string; provider: string }>(
      `/api/v1/integrations/${provider}/connect`,
      { method: "POST" },
    ),
  finishIntegration: (code: string, state: string, errorCode = "") =>
    request<{ connected: boolean; provider: string }>(
      "/api/v1/integrations/google/callback",
      {
        method: "POST",
        body: JSON.stringify({ code, state, error_code: errorCode }),
      },
    ),
  disconnectIntegration: (provider: string) =>
    request<{ disconnected: boolean }>(`/api/v1/integrations/${provider}`, {
      method: "DELETE",
    }),
  notifications: () =>
    request<{ notifications: Notice[]; unread: number }>(
      "/api/v1/notifications",
    ),
  readNotification: (id: string) =>
    request<{ read: boolean }>(`/api/v1/notifications/${id}/read`, {
      method: "POST",
    }),
  reminders: () =>
    request<{ reminders: Reminder[] }>("/api/v1/notifications/reminders"),
  createReminder: (
    name: string,
    body: string,
    hour: number,
    minute: number,
    timezone: string,
  ) =>
    request<Reminder>("/api/v1/notifications/reminders", {
      method: "POST",
      body: JSON.stringify({ name, body, hour, minute, timezone }),
    }),
  deleteReminder: (id: string) =>
    request<{ deleted: boolean }>(`/api/v1/notifications/reminders/${id}`, {
      method: "DELETE",
    }),
};

export async function streamMessage(
  conversationId: string,
  content: string,
  onDelta: (text: string) => void,
  signal: AbortSignal,
): Promise<void> {
  const response = await authenticatedFetch(
    `/api/v1/conversations/${conversationId}/stream`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
      signal,
    },
  );
  if (!response.ok || !response.body) throw await readError(response);

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() ?? "";
    for (const chunk of chunks) {
      const eventLine = chunk
        .split("\n")
        .find((line) => line.startsWith("event: "));
      const dataLine = chunk
        .split("\n")
        .find((line) => line.startsWith("data: "));
      if (!dataLine || !eventLine) continue;
      const event = eventLine.slice(7);
      const data = JSON.parse(dataLine.slice(6));
      if (event === "error") throw new ApiError(503, data.code, data.message);
      if (event === "text.delta" && data.text) onDelta(data.text);
    }
  }
}
