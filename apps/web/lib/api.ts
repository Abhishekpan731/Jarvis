const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type User = { id: string; email: string; display_name: string };
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
export type SpeakResult = {
  locale: string;
  language: string;
  text: string;
  truncated: boolean;
  audio: null;
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

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Content-Type", "application/json");
  const access = token();
  if (access) headers.set("Authorization", `Bearer ${access}`);
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers,
    credentials: "include",
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(
      response.status,
      body.error?.code ?? "error",
      body.error?.message ?? response.statusText,
    );
  }
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
  speak: (text: string, locale: string) =>
    request<SpeakResult>("/api/v1/voice/speak", {
      method: "POST",
      body: JSON.stringify({ text, locale }),
    }),
  command: (command: string, locale: string, conversationId: string) =>
    request<CommandResult>("/api/v1/assistant/commands", {
      method: "POST",
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
};

export async function streamMessage(
  conversationId: string,
  content: string,
  onDelta: (text: string) => void,
  signal: AbortSignal,
): Promise<void> {
  const headers = new Headers({ "Content-Type": "application/json" });
  const access = token();
  if (access) headers.set("Authorization", `Bearer ${access}`);
  const response = await fetch(
    `${BASE}/api/v1/conversations/${conversationId}/stream`,
    {
      method: "POST",
      headers,
      body: JSON.stringify({ content }),
      signal,
    },
  );
  if (!response.ok || !response.body) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(
      response.status,
      body.error?.code ?? "error",
      body.error?.message ?? "Request failed.",
    );
  }
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
