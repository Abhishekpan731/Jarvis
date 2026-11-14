"use client";

import {
    ApiError,
    ChatMessage,
    Conversation,
    IntegrationItem,
    User,
    api,
    clearToken,
    saveToken,
    streamMessage,
} from "@/lib/api";
import { captureAvailable, listen, speak, stopSpeaking } from "@/lib/speech";
import { FormEvent, useEffect, useRef, useState } from "react";

export default function Home() {
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.has("code") || params.has("state") || params.has("error")) {
      sessionStorage.setItem(
        "jarvis_oauth",
        JSON.stringify({
          code: params.get("code") ?? "",
          state: params.get("state") ?? "",
          error: params.get("error") ?? "",
        }),
      );
      window.history.replaceState({}, "", "/");
    }
    api
      .session()
      .then((res) => setUser(res.user))
      .catch(() => setUser(null))
      .finally(() => setReady(true));
  }, []);

  if (!ready)
    return (
      <main className="grid min-h-screen place-items-center text-sm text-neutral-400">
        Loading…
      </main>
    );
  if (!user) return <Auth onSignedIn={setUser} />;
  return <Workspace user={user} onSignedOut={() => setUser(null)} />;
}

function Auth({ onSignedIn }: { onSignedIn: (user: User) => void }) {
  const [mode, setMode] = useState<"signin" | "register">("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [error, setError] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    try {
      const result =
        mode === "register"
          ? await api.register(email, password, name || email.split("@")[0])
          : await api.login(email, password);
      saveToken(result.access_token);
      const session = await api.session();
      onSignedIn(session.user);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong.");
    }
  }

  return (
    <main className="grid min-h-screen place-items-center px-4">
      <form
        onSubmit={submit}
        className="w-full max-w-sm rounded-2xl border border-white/10 bg-white/5 p-6 backdrop-blur"
      >
        <p className="text-xs tracking-[0.3em] text-neutral-400">JARVIS</p>
        <h1 className="mt-2 text-2xl font-semibold">
          {mode === "signin" ? "Welcome back" : "Create your account"}
        </h1>
        <p className="mt-1 text-sm text-neutral-400">
          One account, every browser. Nothing to install.
        </p>
        {mode === "register" && (
          <input
            className="mt-3 w-full rounded-lg border border-white/10 bg-black/30 px-3 py-2"
            placeholder="Name"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        )}
        <input
          className="mt-3 w-full rounded-lg border border-white/10 bg-black/30 px-3 py-2"
          type="email"
          required
          placeholder="Email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
        <input
          className="mt-3 w-full rounded-lg border border-white/10 bg-black/30 px-3 py-2"
          type="password"
          required
          minLength={8}
          placeholder="Password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        {error && <p className="mt-3 text-sm text-red-300">{error}</p>}
        <button className="mt-4 w-full rounded-lg bg-white px-4 py-2.5 font-medium text-black">
          {mode === "signin" ? "Sign in" : "Create account"}
        </button>
        <button
          type="button"
          className="mt-3 w-full text-sm text-neutral-400"
          onClick={() => setMode(mode === "signin" ? "register" : "signin")}
        >
          {mode === "signin"
            ? "Need an account? Register"
            : "Already registered? Sign in"}
        </button>
      </form>
    </main>
  );
}

function Workspace({
  user,
  onSignedOut,
}: {
  user: User;
  onSignedOut: () => void;
}) {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [active, setActive] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const [aiConfigured, setAiConfigured] = useState<boolean | null>(null);
  const [error, setError] = useState("");
  const [locale, setLocale] = useState("en");
  const [connectionsOpen, setConnectionsOpen] = useState(false);
  const [integrations, setIntegrations] = useState<IntegrationItem[]>([]);
  const [notice, setNotice] = useState("");
  const [listening, setListening] = useState(false);
  const [canCapture, setCanCapture] = useState(false);
  const abort = useRef<AbortController | null>(null);
  const stopListen = useRef<(() => void) | null>(null);
  const held = useRef(false);

  useEffect(() => {
    api
      .conversations()
      .then((res) => setConversations(res.conversations))
      .catch(() => setError("Could not load conversations."));
    api
      .health()
      .then((res) => setAiConfigured(res.ai.configured))
      .catch(() => setAiConfigured(false));
    setCanCapture(captureAvailable());
  }, []);

  useEffect(() => {
    const raw = sessionStorage.getItem("jarvis_oauth");
    if (!raw) return;
    sessionStorage.removeItem("jarvis_oauth");
    let saved: { code?: string; state?: string; error?: string };
    try {
      saved = JSON.parse(raw) as {
        code?: string;
        state?: string;
        error?: string;
      };
    } catch {
      return;
    }
    if (!saved.code && !saved.state && !saved.error) return;
    void finishRedirect(saved.code ?? "", saved.state ?? "", saved.error ?? "");
  }, []);

  async function loadIntegrations() {
    try {
      const res = await api.integrations();
      setIntegrations(res.integrations);
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not load connections.",
      );
    }
  }

  async function finishRedirect(code: string, state: string, denied: string) {
    try {
      await api.finishIntegration(code, state, denied);
      setNotice("Connected.");
      setConnectionsOpen(true);
      await loadIntegrations();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "The connection was not saved.",
      );
      setConnectionsOpen(true);
    }
  }

  async function connect(provider: string) {
    setError("");
    try {
      const res = await api.connectIntegration(provider);
      window.location.assign(res.authorize_url);
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : "Could not start the connection.",
      );
    }
  }

  async function disconnect(provider: string) {
    setError("");
    try {
      await api.disconnectIntegration(provider);
      await loadIntegrations();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not disconnect.");
    }
  }

  useEffect(() => {
    if (!active) return;
    api
      .messages(active)
      .then((res) => setMessages(res.messages))
      .catch(() => setError("Could not load messages."));
  }, [active]);

  async function startConversation() {
    const created = await api.createConversation();
    setConversations((prev) => [created, ...prev]);
    setActive(created.id);
    setMessages([]);
  }

  async function send(event: FormEvent) {
    event.preventDefault();
    if (!draft.trim() || !active || sending) return;
    const content = draft;
    setDraft("");
    setSending(true);
    setError("");
    setMessages((prev) => [
      ...prev,
      {
        id: crypto.randomUUID(),
        role: "user",
        content,
        created_at: new Date().toISOString(),
      },
    ]);
    setStreaming(true);
    abort.current = new AbortController();
    let reply = "";
    try {
      await streamMessage(
        active,
        content,
        (delta) => {
          reply += delta;
          setMessages((prev) => {
            const without = prev.filter(
              (message) => message.id !== "streaming",
            );
            return [
              ...without,
              {
                id: "streaming",
                role: "assistant",
                content: reply,
                created_at: "",
              },
            ];
          });
        },
        abort.current.signal,
      );
      const stored = await api.messages(active);
      setMessages(stored.messages);
    } catch (err) {
      if (!(err instanceof DOMException)) {
        setError(err instanceof ApiError ? err.message : "The reply failed.");
      }
    } finally {
      setStreaming(false);
      setSending(false);
    }
  }

  async function holdToTalk(event: { preventDefault: () => void }) {
    event.preventDefault();
    if (held.current || !active || listening || sending) return;
    held.current = true;
    stopSpeaking();
    if (!canCapture) {
      setError("This browser has no speech capture. Type the message instead.");
      return;
    }
    let language = locale === "hi" ? "hi-IN" : "en-US";
    try {
      const session = await api.voiceSession(locale);
      language = session.language;
    } catch {
      if (held.current) setError("Voice session unavailable. Type instead.");
      return;
    }
    if (!held.current) return;
    setListening(true);
    setError("");
    stopListen.current = listen(
      language,
      (text) => void submitVoice(text),
      () => setListening(false),
    );
  }

  async function submitVoice(text: string) {
    if (!active || sending) {
      setDraft(text);
      return;
    }
    const pending = crypto.randomUUID();
    setDraft("");
    setSending(true);
    setError("");
    setMessages((prev) => [
      ...prev,
      {
        id: pending,
        role: "user",
        content: text,
        created_at: new Date().toISOString(),
      },
    ]);
    let result;
    try {
      result = await api.command(text, locale, active);
    } catch (err) {
      setMessages((prev) => prev.filter((message) => message.id !== pending));
      setDraft(text);
      setError(
        err instanceof ApiError
          ? err.message
          : "The voice command failed. You can send it as text.",
      );
      setSending(false);
      return;
    }
    const stored = await api.messages(active).catch(() => null);
    if (stored) setMessages(stored.messages);
    else {
      setMessages((prev) => [
        ...prev.filter((message) => message.id !== pending),
        {
          id: crypto.randomUUID(),
          role: "user",
          content: text,
          created_at: new Date().toISOString(),
        },
        {
          id: crypto.randomUUID(),
          role: "assistant",
          content: result.response_text,
          created_at: new Date().toISOString(),
        },
      ]);
    }
    speak(result.voice_response, locale === "hi" ? "hi-IN" : "en-US");
    setSending(false);
  }

  function releaseTalk() {
    held.current = false;
    stopListen.current?.();
    stopListen.current = null;
    setListening(false);
  }

  return (
    <div className="grid min-h-screen grid-cols-1 md:grid-cols-[260px_1fr]">
      <aside className="border-b border-white/10 p-4 md:border-b-0 md:border-r">
        <div className="flex items-center justify-between">
          <p className="text-xs tracking-[0.3em] text-neutral-400">JARVIS</p>
          <button
            className="text-sm text-neutral-300"
            onClick={startConversation}
          >
            New
          </button>
        </div>
        <ul className="mt-4 space-y-1">
          {conversations.map((conversation) => (
            <li key={conversation.id}>
              <button
                className={`w-full truncate rounded-lg px-3 py-2 text-left text-sm ${conversation.id === active ? "bg-white/10" : "hover:bg-white/5"}`}
                onClick={() => setActive(conversation.id)}
              >
                {conversation.title}
              </button>
            </li>
          ))}
        </ul>
        <div className="mt-6 border-t border-white/10 pt-4 text-sm">
          <p className="truncate text-neutral-300">{user.display_name}</p>
          <button
            className="mt-2 block text-neutral-300"
            onClick={() => {
              setConnectionsOpen(true);
              void loadIntegrations();
            }}
          >
            Connections
          </button>
          <button
            className="mt-2 text-neutral-500"
            onClick={async () => {
              await api.logout().catch(() => undefined);
              clearToken();
              onSignedOut();
            }}
          >
            Sign out
          </button>
        </div>
      </aside>
      <main className="flex min-h-screen flex-col">
        {aiConfigured === false && (
          <p className="border-b border-amber-400/30 bg-amber-400/10 px-4 py-2 text-sm text-amber-200">
            No model provider is configured. Set OPENAI_API_KEY on the API to
            receive replies.
          </p>
        )}
        <div className="flex-1 space-y-4 overflow-y-auto px-4 py-6">
          {messages.map((message) => (
            <div
              key={message.id}
              className={message.role === "user" ? "text-right" : ""}
            >
              <p className="inline-block max-w-[40rem] rounded-2xl bg-white/5 px-4 py-2 text-left">
                {message.content}
              </p>
            </div>
          ))}
          {!active && (
            <p className="text-neutral-500">
              Start a new conversation to begin.
            </p>
          )}
        </div>
        {notice && <p className="px-4 text-sm text-emerald-300">{notice}</p>}
        {error && <p className="px-4 text-sm text-red-300">{error}</p>}
        {connectionsOpen && (
          <section className="border-t border-white/10 px-4 py-3 text-sm">
            <div className="mb-2 flex items-center justify-between">
              <p className="text-neutral-300">Connections</p>
              <button
                type="button"
                className="text-neutral-500"
                onClick={() => setConnectionsOpen(false)}
              >
                Close
              </button>
            </div>
            <ul className="space-y-2">
              {integrations.map((item) => (
                <li
                  key={item.provider}
                  className="flex items-center justify-between gap-3"
                >
                  <span>
                    {item.name}
                    <span className="ml-2 text-neutral-500">
                      {item.connection?.connected
                        ? "connected"
                        : item.configured
                          ? "not connected"
                          : "not configured"}
                    </span>
                  </span>
                  {item.connection?.connected ? (
                    <button
                      type="button"
                      onClick={() => void disconnect(item.provider)}
                    >
                      Disconnect
                    </button>
                  ) : (
                    <button
                      type="button"
                      disabled={!item.configured}
                      onClick={() => void connect(item.provider)}
                    >
                      Connect
                    </button>
                  )}
                </li>
              ))}
            </ul>
          </section>
        )}
        <form
          onSubmit={send}
          className="flex gap-2 border-t border-white/10 p-4"
        >
          <select
            aria-label="Voice language"
            className="rounded-xl border border-white/10 bg-black/30 px-2 text-sm"
            value={locale}
            onChange={(e) => setLocale(e.target.value)}
          >
            <option value="en">EN</option>
            <option value="hi">HI</option>
          </select>
          <button
            type="button"
            className={`rounded-xl px-3 text-sm ${listening ? "bg-red-400 text-black" : "bg-white/10"}`}
            disabled={!active || sending}
            onMouseDown={holdToTalk}
            onMouseUp={releaseTalk}
            onMouseLeave={releaseTalk}
            onTouchStart={holdToTalk}
            onTouchEnd={releaseTalk}
          >
            {listening ? "Listening" : "Hold"}
          </button>
          <input
            className="flex-1 rounded-xl border border-white/10 bg-black/30 px-4 py-3"
            placeholder={
              active
                ? "Message JARVIS, or hold to talk"
                : "Create a conversation first"
            }
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            disabled={!active}
          />
          {streaming ? (
            <button
              type="button"
              className="rounded-xl bg-white/10 px-4"
              onClick={() => abort.current?.abort()}
            >
              Stop
            </button>
          ) : (
            <button
              className="rounded-xl bg-white px-4 font-medium text-black"
              disabled={!active || sending}
            >
              Send
            </button>
          )}
        </form>
      </main>
    </div>
  );
}
