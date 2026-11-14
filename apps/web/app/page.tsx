"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import {
  ApiError,
  ChatMessage,
  Conversation,
  User,
  api,
  clearToken,
  saveToken,
  streamMessage,
} from "@/lib/api";

export default function Home() {
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    api.session().then((res) => setUser(res.user)).catch(() => setUser(null)).finally(() => setReady(true));
  }, []);

  if (!ready) return <main className="grid min-h-screen place-items-center text-sm text-neutral-400">Loading…</main>;
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
      <form onSubmit={submit} className="w-full max-w-sm rounded-2xl border border-white/10 bg-white/5 p-6 backdrop-blur">
        <p className="text-xs tracking-[0.3em] text-neutral-400">JARVIS</p>
        <h1 className="mt-2 text-2xl font-semibold">{mode === "signin" ? "Welcome back" : "Create your account"}</h1>
        <p className="mt-1 text-sm text-neutral-400">One account, every browser. Nothing to install.</p>
        {mode === "register" && (
          <input className="mt-3 w-full rounded-lg border border-white/10 bg-black/30 px-3 py-2" placeholder="Name" value={name} onChange={(e) => setName(e.target.value)} />
        )}
        <input className="mt-3 w-full rounded-lg border border-white/10 bg-black/30 px-3 py-2" type="email" required placeholder="Email" value={email} onChange={(e) => setEmail(e.target.value)} />
        <input className="mt-3 w-full rounded-lg border border-white/10 bg-black/30 px-3 py-2" type="password" required minLength={8} placeholder="Password" value={password} onChange={(e) => setPassword(e.target.value)} />
        {error && <p className="mt-3 text-sm text-red-300">{error}</p>}
        <button className="mt-4 w-full rounded-lg bg-white px-4 py-2.5 font-medium text-black">{mode === "signin" ? "Sign in" : "Create account"}</button>
        <button
          type="button"
          className="mt-3 w-full text-sm text-neutral-400"
          onClick={() => setMode(mode === "signin" ? "register" : "signin")}
        >
          {mode === "signin" ? "Need an account? Register" : "Already registered? Sign in"}
        </button>
      </form>
    </main>
  );
}

function Workspace({ user, onSignedOut }: { user: User; onSignedOut: () => void }) {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [active, setActive] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const [aiConfigured, setAiConfigured] = useState<boolean | null>(null);
  const [error, setError] = useState("");
  const abort = useRef<AbortController | null>(null);

  useEffect(() => {
    api.conversations().then((res) => setConversations(res.conversations)).catch(() => setError("Could not load conversations."));
    api.health().then((res) => setAiConfigured(res.ai.configured)).catch(() => setAiConfigured(false));
  }, []);

  useEffect(() => {
    if (!active) return;
    api.messages(active).then((res) => setMessages(res.messages)).catch(() => setError("Could not load messages."));
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
    setMessages((prev) => [...prev, { id: crypto.randomUUID(), role: "user", content, created_at: new Date().toISOString() }]);
    setStreaming(true);
    abort.current = new AbortController();
    let reply = "";
    try {
      await streamMessage(active, content, (delta) => {
        reply += delta;
        setMessages((prev) => {
          const without = prev.filter((message) => message.id !== "streaming");
          return [...without, { id: "streaming", role: "assistant", content: reply, created_at: "" }];
        });
      }, abort.current.signal);
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

  return (
    <div className="grid min-h-screen grid-cols-1 md:grid-cols-[260px_1fr]">
      <aside className="border-b border-white/10 p-4 md:border-b-0 md:border-r">
        <div className="flex items-center justify-between">
          <p className="text-xs tracking-[0.3em] text-neutral-400">JARVIS</p>
          <button className="text-sm text-neutral-300" onClick={startConversation}>New</button>
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
            className="mt-2 text-neutral-500"
            onClick={async () => { await api.logout().catch(() => undefined); clearToken(); onSignedOut(); }}
          >
            Sign out
          </button>
        </div>
      </aside>
      <main className="flex min-h-screen flex-col">
        {aiConfigured === false && (
          <p className="border-b border-amber-400/30 bg-amber-400/10 px-4 py-2 text-sm text-amber-200">
            No model provider is configured. Set OPENAI_API_KEY on the API to receive replies.
          </p>
        )}
        <div className="flex-1 space-y-4 overflow-y-auto px-4 py-6">
          {messages.map((message) => (
            <div key={message.id} className={message.role === "user" ? "text-right" : ""}>
              <p className="inline-block max-w-[40rem] rounded-2xl bg-white/5 px-4 py-2 text-left">{message.content}</p>
            </div>
          ))}
          {!active && <p className="text-neutral-500">Start a new conversation to begin.</p>}
        </div>
        {error && <p className="px-4 text-sm text-red-300">{error}</p>}
        <form onSubmit={send} className="flex gap-2 border-t border-white/10 p-4">
          <input
            className="flex-1 rounded-xl border border-white/10 bg-black/30 px-4 py-3"
            placeholder={active ? "Message JARVIS" : "Create a conversation first"}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            disabled={!active}
          />
          {streaming ? (
            <button type="button" className="rounded-xl bg-white/10 px-4" onClick={() => abort.current?.abort()}>Stop</button>
          ) : (
            <button className="rounded-xl bg-white px-4 font-medium text-black" disabled={!active || sending}>Send</button>
          )}
        </form>
      </main>
    </div>
  );
}
