"use client";

import {
  ApiError,
  api,
  canOpenRealtime,
  clearToken,
  mintFallsBack,
  saveToken,
  streamMessage,
  type ChatMessage,
  type CommandResult,
  type Conversation,
  type IntegrationItem,
  type Notice,
  type RealtimeSession,
  type Reminder,
  type Preferences,
  type TaskDetail,
  type UserSession,
  type User,
} from "@/lib/api";
import {
  captureAvailable,
  listen,
  openRealtimePeer,
  playAudio,
  realtimeAvailable,
  recognitionErrorMessage,
  recordingAvailable,
  speak,
  startRecording,
  stopSpeaking,
  type RealtimePeer,
} from "@/lib/speech";
import { FormEvent, useCallback, useEffect, useRef, useState } from "react";

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
  const [notesOpen, setNotesOpen] = useState(false);
  const [integrations, setIntegrations] = useState<IntegrationItem[]>([]);
  const [notes, setNotes] = useState<Notice[]>([]);
  const [unread, setUnread] = useState(0);
  const [reminders, setReminders] = useState<Reminder[]>([]);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [preferenceDraft, setPreferenceDraft] = useState<Preferences | null>(null);
  const [settingsSessions, setSettingsSessions] = useState<UserSession[]>([]);
  const [settingsLoading, setSettingsLoading] = useState(false);
  const [settingsError, setSettingsError] = useState("");
  const [settingsNotice, setSettingsNotice] = useState("");
  const [taskDetail, setTaskDetail] = useState<TaskDetail | null>(null);
  const [taskApprovalChoice, setTaskApprovalChoice] =
    useState<"approve" | "reject" | null>(null);
  const [taskDecisionLoading, setTaskDecisionLoading] = useState(false);
  const [taskDecisionStatus, setTaskDecisionStatus] = useState("");
  const [taskDecisionError, setTaskDecisionError] = useState("");
  const [reminderName, setReminderName] = useState("");
  const [reminderTime, setReminderTime] = useState("08:00");
  const [reminderTimezone, setReminderTimezone] = useState("UTC");
  const [notice, setNotice] = useState("");
  const [listening, setListening] = useState(false);
  const [canCapture, setCanCapture] = useState<boolean | null>(null);
  const [sttConfigured, setSttConfigured] = useState(false);
  const [pendingText, setPendingText] = useState("");
  const [commandStatus, setCommandStatus] = useState<{
    conversationId: string;
    taskId: string | null;
    status: string;
    requiresApproval: boolean;
    jobStatus: string | null;
  } | null>(null);
  const abort = useRef<AbortController | null>(null);
  const setupAbort = useRef<AbortController | null>(null);
  const stopListen = useRef<(() => void) | null>(null);
  const held = useRef(false);
  const recorder = useRef<{ stop: () => Promise<Blob> } | null>(null);
  const peer = useRef<RealtimePeer | null>(null);
  const mounted = useRef(false);
  const voiceSubmission = useRef(false);
  const realtimeKeys = useRef(new Map<string, string>());
  const completedRealtimeCalls = useRef(new Set<string>());

  useEffect(() => {
    api
      .conversations()
      .then((res) => setConversations(res.conversations))
      .catch(() => setError("Could not load conversations."));
    api
      .health()
      .then((res) => {
        setAiConfigured(res.ai.configured);
        const sttAvailable = res.speech.stt_configured;
        setSttConfigured(sttAvailable);
        setCanCapture(
          captureAvailable() ||
            realtimeAvailable() ||
            (sttAvailable && recordingAvailable()),
        );
      })
      .catch(() => setAiConfigured(false));
    api
      .notifications()
      .then((res) => {
        setNotes(res.notifications);
        setUnread(res.unread);
      })
      .catch(() => undefined);
    setCanCapture(captureAvailable() || realtimeAvailable());
    const browserTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (browserTimezone) setReminderTimezone(browserTimezone);
  }, []);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      abort.current?.abort();
      setupAbort.current?.abort();
      stopListen.current?.();
      const activeRecorder = recorder.current;
      recorder.current = null;
      if (activeRecorder) void activeRecorder.stop().catch(() => undefined);
      peer.current?.close();
      peer.current = null;
      held.current = false;
      stopSpeaking();
    };
  }, []);

  useEffect(() => {
    let stopped = false;
    let polling = false;
    const cursorKey = `jarvis_sync_${user.id}`;
    const storedCursor = Number(sessionStorage.getItem(cursorKey));
    let cursor = Number.isSafeInteger(storedCursor) ? storedCursor : 0;
    const poll = async () => {
      if (stopped || polling) return;
      polling = true;
      let receivedEvents = false;
      const activeConversation = active;
      try {
        for (let page = 0; page < 5; page += 1) {
          const result = await api.sync(cursor);
          if (stopped) return;
          receivedEvents ||= result.events.length > 0;
          result.events.forEach((event) => {
            if (event.kind !== "command.accepted" || !activeConversation) return;
            if (event.payload.conversation_id !== activeConversation) return;
            const status = event.payload.status;
            if (typeof status !== "string") return;
            const taskId =
              typeof event.payload.task_id === "string"
                ? event.payload.task_id
                : null;
            setCommandStatus((current) =>
              current?.conversationId === activeConversation &&
              current.taskId === taskId
                ? current
                : {
                    conversationId: activeConversation,
                    taskId,
                    status,
                    requiresApproval: event.payload.requires_approval === true,
                    jobStatus: null,
                  },
            );
          });
          const nextCursor = Math.max(cursor, result.cursor);
          const advanced = nextCursor > cursor;
          cursor = nextCursor;
          sessionStorage.setItem(cursorKey, String(cursor));
          if (!advanced || result.events.length < 100) break;
        }
        if (!receivedEvents || stopped) return;
        void api.conversations().then((res) => {
          if (!stopped) setConversations(res.conversations);
        }).catch(() => undefined);
        void api.notifications().then((res) => {
          if (stopped) return;
          setNotes(res.notifications);
          setUnread(res.unread);
        }).catch(() => undefined);
        if (activeConversation) {
          void api.messages(activeConversation).then((res) => {
            if (!stopped) setMessages(res.messages);
          }).catch(() => undefined);
        }
      } catch {
        // Keep the last cursor so the next poll replays any missed events.
      } finally {
        polling = false;
      }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 5000);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [user.id, active]);

  const taskIdForPolling = commandStatus?.taskId;
  useEffect(() => {
    if (!taskIdForPolling) return;
    let stopped = false;
    let terminal = false;
    let expiryTimer: number | null = null;
    const terminalStatuses = [
      "completed",
      "failed",
      "cancelled",
      "waiting_for_approval",
      "waiting_for_user",
      "paused",
      "requires_retry",
    ];
    const refreshTask = async () => {
      try {
        const task = await api.task(taskIdForPolling);
        if (stopped) return;
        setTaskDetail(task);
        terminal = terminalStatuses.includes(task.status);
        if (
          task.status === "waiting_for_approval" &&
          task.approval_expires_at
        ) {
          const expiryDelay = Math.max(
            Date.parse(task.approval_expires_at) - Date.now(),
            1000,
          );
          expiryTimer = window.setTimeout(() => {
            setTaskDecisionStatus("Approval window ended. Checking task status…");
            void refreshTask();
          }, expiryDelay);
        }
        setCommandStatus((current) =>
          current?.taskId === task.id
            ? {
                ...current,
                status: task.status,
                jobStatus: task.job?.status ?? null,
              }
            : current,
        );
      } catch {
        if (!stopped && terminal) {
          expiryTimer = window.setTimeout(() => void refreshTask(), 3000);
        }
      }
    };
    void refreshTask();
    const timer = window.setInterval(() => {
      if (!terminal) void refreshTask();
    }, 3000);
    return () => {
      stopped = true;
      window.clearInterval(timer);
      if (expiryTimer !== null) window.clearTimeout(expiryTimer);
    };
  }, [taskIdForPolling]);


  async function loadNotes() {
    try {
      const [listed, saved] = await Promise.all([
        api.notifications(),
        api.reminders(),
      ]);
      setNotes(listed.notifications);
      setUnread(listed.unread);
      setReminders(saved.reminders);
      showGranted(listed.notifications);
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not load notices.",
      );
    }
  }

  async function markRead(id: string) {
    await api.readNotification(id).catch(() => undefined);
    await loadNotes();
  }

  async function addReminder(event: FormEvent) {
    event.preventDefault();
    const name = reminderName.trim();
    const timezone = reminderTimezone.trim();
    if (!name || !timezone) return;
    const [hour, minute] = reminderTime.split(":").map(Number);
    setReminderName("");
    try {
      await api.createReminder(name, "", hour, minute, timezone);
      await loadNotes();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not save the reminder.",
      );
    }
  }

  function showGranted(items: Notice[]) {
    if (
      typeof Notification === "undefined" ||
      Notification.permission !== "granted"
    )
      return;
    for (const item of items.filter((note) => !note.read).slice(0, 3)) {
      if (sessionStorage.getItem(`jarvis_note_${item.id}`)) continue;
      sessionStorage.setItem(`jarvis_note_${item.id}`, "1");
      new Notification(item.title, { body: item.body });
    }
  }

  function askBrowser() {
    if (typeof Notification === "undefined") {
      setNotice("This browser has no notification permission to grant.");
      return;
    }
    void Notification.requestPermission().then((result) => {
      setNotice(
        result === "granted"
          ? "Browser notifications are allowed. JARVIS still does not push to a device."
          : "Browser notifications stay off.",
      );
    });
  }

  useEffect(() => {
    showGranted(notes);
  }, [notes]);

  const loadIntegrations = useCallback(async () => {
    try {
      const res = await api.integrations();
      setIntegrations(res.integrations);
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not load connections.",
      );
    }
  }, []);

  /** Load persisted preferences and active sessions for the settings panel. */
  async function loadSettings() {
    setSettingsError("");
    setSettingsNotice("");
    setSettingsLoading(true);
    try {
      const [savedPreferences, savedSessions] = await Promise.all([
        api.preferences(),
        api.sessions(),
      ]);
      setPreferenceDraft(savedPreferences.preferences);
      setSettingsSessions(savedSessions.sessions);
    } catch (err) {
      setSettingsError(
        err instanceof ApiError ? err.message : "Could not load account settings.",
      );
    } finally {
      setSettingsLoading(false);
    }
  }

  /** Save the current account preference values. */
  async function savePreferences(event: FormEvent) {
    event.preventDefault();
    if (!preferenceDraft) return;
    setSettingsError("");
    setSettingsNotice("");
    try {
      const saved = await api.updatePreferences(preferenceDraft);
      setPreferenceDraft(saved.preferences);
      setSettingsNotice("Preferences saved.");
    } catch (err) {
      setSettingsError(
        err instanceof ApiError ? err.message : "Could not save preferences.",
      );
    }
  }

  /** Revoke a session and sign out when it is the current session. */
  async function revokeUserSession(sessionId: string) {
    setSettingsError("");
    setSettingsNotice("");
    const selected = settingsSessions.find((item) => item.id === sessionId);
    try {
      await api.revokeSession(sessionId);
    } catch (err) {
      setSettingsError(
        err instanceof ApiError ? err.message : "Could not revoke the session.",
      );
      return;
    }
    if (selected?.current) {
      clearToken();
      onSignedOut();
      return;
    }
    try {
      const refreshed = await api.sessions();
      setSettingsSessions(refreshed.sessions);
      setSettingsNotice("Session revoked.");
    } catch {
      setSettingsError("Session revoked, but the session list could not be refreshed.");
    }
  }

  /** Refresh canonical task state after approval or rejection. */
  async function refreshTaskDetail(taskId: string) {
    const refreshed = await api.task(taskId);
    setTaskDetail(refreshed);
    setCommandStatus((current) =>
      current?.taskId === refreshed.id
        ? {
            ...current,
            status: refreshed.status,
            jobStatus: refreshed.job?.status ?? null,
          }
        : current,
    );
    return refreshed;
  }

  /** Submit a confirmed task decision with the server-issued action hash. */
  async function submitTaskDecision(choice: "approve" | "reject") {
    const task = taskDetail;
    if (
      !task ||
      task.status !== "waiting_for_approval" ||
      !task.approval_action_hash ||
      taskDecisionLoading
    )
      return;
    let decisionRecorded = false;
    setTaskDecisionLoading(true);
    setTaskDecisionError("");
    setTaskDecisionStatus("Submitting decision…");
    try {
      if (choice === "approve") {
        await api.approveTask(task.id, task.approval_action_hash);
      } else {
        await api.rejectTask(task.id, task.approval_action_hash);
      }
      decisionRecorded = true;
      setTaskApprovalChoice(null);
      const refreshed = await refreshTaskDetail(task.id);
      setTaskDecisionStatus(
        `${choice === "approve" ? "Approval" : "Rejection"} recorded. Task status: ${refreshed.status.replace(/_/g, " ")}.`,
      );
    } catch (err) {
      setTaskDecisionError(
        decisionRecorded
          ? "Decision recorded, but task status could not be refreshed yet."
          : err instanceof ApiError
            ? `${err.code}: ${err.message}`
            : "Could not submit the task decision.",
      );
      try {
        const refreshed = await refreshTaskDetail(task.id);
        setTaskDecisionStatus(
          `${decisionRecorded ? "Decision recorded. " : "Task status refreshed: "}${refreshed.status.replace(/_/g, " ")}.`,
        );
        if (decisionRecorded) setTaskDecisionError("");
      } catch {
        setTaskDecisionStatus("");
        setTaskDecisionError((message) => `${message} Task status could not be refreshed.`);
      }
    } finally {
      setTaskDecisionLoading(false);
    }
  }

  const finishRedirect = useCallback(
    async (code: string, state: string, denied: string) => {
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
    },
    [loadIntegrations],
  );

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
  }, [finishRedirect]);

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

  /** Stop active speech and release microphone, peer, and recorder resources. */
  function stopVoiceResources() {
    held.current = false;
    setupAbort.current?.abort();
    setupAbort.current = null;
    stopListen.current?.();
    stopListen.current = null;
    const activeRecorder = recorder.current;
    recorder.current = null;
    if (activeRecorder) void activeRecorder.stop().catch(() => undefined);
    peer.current?.close();
    peer.current = null;
    stopSpeaking();
  }


  /** Start configured server transcription, or use browser recognition as fallback. */
  async function beginRecordingFallback(language: string) {
    if (!held.current) return;
    if (sttConfigured && recordingAvailable()) {
      try {
        const result = await startRecording();
        if (held.current) {
          recorder.current = result;
          setListening(true);
          setError("");
          return;
        }
        void result.stop().catch(() => undefined);
        return;
      } catch {
        // Fall through to browser recognition.
      }
    }
    if (!held.current) return;
    if (!captureAvailable()) {
      held.current = false;
      setError("No supported voice fallback is available. Type instead.");
      return;
    }
    setListening(true);
    setError("");
    stopListen.current = listen(
      language,
      (text) => void submitVoice(text),
      () => setListening(false),
      (error) => setError(recognitionErrorMessage(error)),
    );
  }

  /** Set up a Realtime peer and report setup or connection failures. */
  async function connectRealtime(clientSecret: string) {
    const controller = new AbortController();
    setupAbort.current = controller;
    try {
      const connection = await openRealtimePeer(
        clientSecret,
        {
          onInterim: (text) => setPendingText(text),
          onInterrupt: stopSpeaking,
          onCommand: (text, callId) => void submitVoice(text, callId),
          onAudioFallback: (text) =>
            speak(text, locale === "hi" ? "hi-IN" : "en-US"),
          onError: (message) => {
            if (mounted.current) setError(message);
          },
          onClose: () => {
            realtimeKeys.current = new Map();
            completedRealtimeCalls.current = new Set();
            if (!mounted.current) return;
            held.current = false;
            setListening(false);
            setPendingText("");
          },
        },
        controller.signal,
      );
      if (!held.current || connection.closed) {
        connection.close();
        return;
      }
      peer.current = connection;
      setListening(true);
      setError("");
    } catch (err) {
      if (!controller.signal.aborted && mounted.current) {
        held.current = false;
        setListening(false);
        setError(
          err instanceof Error
            ? err.message
            : "Realtime voice could not start. Type instead.",
        );
      }
    } finally {
      if (setupAbort.current === controller) setupAbort.current = null;
    }
  }

  /** Start realtime; only a 503 mint failure may select the fallback path. */
  async function startRealtimeOrFallback(
    language: string,
    conversationId: string,
  ) {
    let minted: RealtimeSession;
    try {
      minted = await api.openRealtimeSession(locale, conversationId);
    } catch (err) {
      if (err instanceof ApiError && mintFallsBack(err.status)) {
        await beginRecordingFallback(language);
        return;
      }
      if (!held.current) return;
      held.current = false;
      setListening(false);
      setError(
        err instanceof ApiError
          ? err.message
          : "Realtime session could not be created. Type instead.",
      );
      return;
    }
    if (!held.current) return;
    if (!canOpenRealtime(minted) || !minted.client_secret) {
      held.current = false;
      setListening(false);
      setError("The realtime session is unavailable. Type instead.");
      return;
    }
    await connectRealtime(minted.client_secret);
  }

  /** Resolve the server-selected language before opening microphone capture. */
  async function getVoiceLanguage(): Promise<string | null> {
    try {
      return (await api.voiceSession(locale)).language;
    } catch {
      if (held.current) setError("Voice session unavailable. Type instead.");
      held.current = false;
      setListening(false);
      return null;
    }
  }

  /** Begin voice capture only after confirming a usable browser path. */
  async function holdToTalk(event: { preventDefault: () => void }) {
    event.preventDefault();
    if (held.current || !active || listening || sending) return;
    held.current = true;
    stopSpeaking();
    const available =
      captureAvailable() ||
      realtimeAvailable() ||
      (sttConfigured && recordingAvailable());
    if (!available) {
      held.current = false;
      setError("This browser has no speech capture. Type the message instead.");
      return;
    }
    setListening(true);
    const language = await getVoiceLanguage();
    if (!language || !held.current) return;
    if (realtimeAvailable()) {
      await startRealtimeOrFallback(language, active);
      return;
    }
    await beginRecordingFallback(language);
  }

  /** Refresh canonical messages, using a local result if the fetch fails. */
  async function updateVoiceConversation(
    conversationId: string,
    pendingId: string,
    text: string,
    result: CommandResult,
  ) {
    const stored = await api.messages(conversationId).catch(() => null);
    if (!mounted.current) return;
    if (stored) {
      setMessages(stored.messages);
      return;
    }
    setMessages((previous) => [
      ...previous.filter((message) => message.id !== pendingId),
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

  /** Play server audio and fall back to browser speech if it is blocked. */
  async function playVoiceReply(text: string) {
    try {
      const result = await api.speak(text, locale);
      if (result.audio && result.audio_format) {
        if (await playAudio(result.audio, result.audio_format)) return;
      } else {
        speak(text, locale === "hi" ? "hi-IN" : "en-US");
        return;
      }
    } catch {
      // Use browser speech when server audio is unavailable.
    }
    speak(text, locale === "hi" ? "hi-IN" : "en-US");
  }

  /** Submit a voice command with a stable key for each Realtime tool call. */
  async function submitVoice(text: string, callId?: string) {
    if (!active) {
      if (!callId) setDraft(text);
      return;
    }
    if (sending || voiceSubmission.current) {
      if (!callId) setDraft(text);
      return;
    }
    if (callId && completedRealtimeCalls.current.has(callId)) return;
    const idempotencyKey = callId
      ? realtimeKeys.current.get(callId) ?? crypto.randomUUID()
      : crypto.randomUUID();
    if (callId) {
      realtimeKeys.current = new Map([
        ...realtimeKeys.current,
        [callId, idempotencyKey] as const,
      ]);
    }
    voiceSubmission.current = true;
    setDraft("");
    setPendingText("");
    setSending(true);
    setError("");
    try {
      const result = callId
        ? await api.submitRealtimeCommand(active, text, idempotencyKey, locale)
        : await api.command(text, locale, active, idempotencyKey);
      if (callId) {
        completedRealtimeCalls.current = new Set([
          ...completedRealtimeCalls.current,
          callId,
        ]);
      }
      if (!mounted.current) return;
      setCommandStatus({
        conversationId: active,
        taskId: result.task_id,
        status: result.status,
        requiresApproval: result.requires_approval,
        jobStatus: null,
      });
      await updateVoiceConversation(active, idempotencyKey, text, result);
      await playVoiceReply(result.voice_response);
    } catch (err) {
      if (!mounted.current) return;
      if (!callId) setDraft(text);
      setError(
        callId
          ? "Realtime command outcome is unknown. Check the conversation before retrying."
          : err instanceof ApiError
            ? err.message
            : "The voice command failed. Check the conversation before retrying.",
      );
    } finally {
      voiceSubmission.current = false;
      if (mounted.current) setSending(false);
    }
  }

  /** Release voice resources, then transcribe a completed recorder capture. */
  async function releaseTalk() {
    held.current = false;
    setupAbort.current?.abort();
    setupAbort.current = null;
    setListening(false);
    setPendingText("");
    stopListen.current?.();
    stopListen.current = null;
    peer.current?.close();
    peer.current = null;
    stopSpeaking();
    const local = recorder.current;
    recorder.current = null;
    if (!local) return;
    try {
      const blob = await local.stop();
      if (!mounted.current) return;
      const result = await api.transcribe(blob, locale);
      if (!mounted.current) return;
      if (result.text.trim()) void submitVoice(result.text);
      else setError("No speech was detected. Try again or type your message.");
    } catch (err) {
      if (!mounted.current) return;
      setError(
        err instanceof ApiError
          ? err.message
          : "Transcription failed. Try again or type your message.",
      );
    }
  }

  const approvalExpired = Boolean(
    taskDetail?.approval_expires_at &&
      Date.parse(taskDetail.approval_expires_at) <= Date.now(),
  );


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
              setNotesOpen((open) => !open);
              setConnectionsOpen(false);
              void loadNotes();
            }}
          >
            Notices{unread > 0 ? ` (${unread})` : ""}
          </button>
          <button
            className="mt-2 block text-neutral-300"
            onClick={() => {
              setConnectionsOpen(true);
              setNotesOpen(false);
              void loadIntegrations();
            }}
          >
            Connections
          </button>
          <button
            aria-expanded={settingsOpen}
            aria-controls={settingsOpen ? "account-settings-panel" : undefined}
            className="mt-2 block min-h-11 text-neutral-300 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white"
            onClick={() => {
              const opening = !settingsOpen;
              setSettingsOpen(opening);
              setNotesOpen(false);
              setConnectionsOpen(false);
              if (opening) void loadSettings();
            }}
          >
            Account settings
          </button>

          <button
            className="mt-2 text-neutral-500"
            onClick={async () => {
              stopVoiceResources();
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
        <h1 className="sr-only">JARVIS workspace</h1>
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
        {commandStatus?.conversationId === active && (
          <p role="status" aria-live="polite" className="px-4 text-sm text-neutral-300">
            {commandStatus.taskId ? `Task ${commandStatus.taskId}` : "Command"}:
            {" "}{commandStatus.status.replace(/_/g, " ")}
            {commandStatus.jobStatus && ` · Job ${commandStatus.jobStatus}`}
            {commandStatus.requiresApproval && " · Waiting for your approval."}
          </p>
        )}
        {commandStatus?.conversationId === active &&
          commandStatus.taskId &&
          taskDetail?.id === commandStatus.taskId && (
          <section
            aria-labelledby="task-approval-title"
            className="mx-4 space-y-3 rounded-xl border border-white/10 bg-white/5 p-4 text-sm"
          >
            <h2 id="task-approval-title" className="font-medium text-neutral-200">
              Task details
            </h2>
            <p className="text-neutral-300">{taskDetail.title}</p>
            <p role="status" aria-live="polite" className="text-neutral-400">
              Status: {taskDetail.status.replace(/_/g, " ")}
              {taskDetail.error === "approval_expired" && " · Approval expired."}
              {taskDetail.error === "approval_rejected" && " · Rejected."}
            </p>
            <button
              type="button"
              className="min-h-11 rounded-lg border border-white/20 px-3 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white"
              onClick={() => {
                setTaskDecisionError("");
                setTaskDecisionStatus("Refreshing task status…");
                void refreshTaskDetail(taskDetail.id)
                  .then((refreshed) =>
                    setTaskDecisionStatus(
                      `Task status refreshed: ${refreshed.status.replace(/_/g, " ")}.`,
                    ),
                  )
                  .catch((err) => {
                    setTaskDecisionStatus("");
                    setTaskDecisionError(
                      err instanceof ApiError
                        ? `${err.code}: ${err.message}`
                        : "Could not refresh task status.",
                    );
                  });
              }}
            >
              Refresh task status
            </button>
            {taskDetail.status === "waiting_for_approval" && (
              <>
                <ol className="list-decimal space-y-2 pl-5">
                  {taskDetail.steps.map((step) => (
                    <li key={step.position}>
                      {step.action}
                      <span className="text-neutral-500">
                        by {step.agent_id} · {step.risk}
                      </span>
                    </li>
                  ))}
                </ol>
                {taskDetail.approval_expires_at && (
                  <p className="text-neutral-400">
                    Approval expires <time dateTime={taskDetail.approval_expires_at}>
                      {new Date(taskDetail.approval_expires_at).toLocaleString()}
                    </time>.
                  </p>
                )}
                {approvalExpired && (
                  <p role="alert" className="text-amber-200">
                    Approval window expired. Refreshing task state; no decision can be submitted.
                  </p>
                )}
                {taskDetail.approval_action_hash ? (
                  <div className="flex flex-wrap gap-2">
                    {taskApprovalChoice ? (
                      <>
                        <p className="basis-full text-neutral-200">
                          Confirm {taskApprovalChoice} for the actions listed above?
                        </p>
                        <button
                          type="button"
                          disabled={taskDecisionLoading || approvalExpired}
                          className="min-h-11 rounded-lg bg-white px-4 text-black disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white"
                          onClick={() => void submitTaskDecision(taskApprovalChoice)}
                        >
                          Confirm {taskApprovalChoice}
                        </button>
                        <button
                          type="button"
                          disabled={taskDecisionLoading}
                          className="min-h-11 rounded-lg border border-white/20 px-4 disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white"
                          onClick={() => setTaskApprovalChoice(null)}
                        >
                          Cancel
                        </button>
                      </>
                    ) : (
                      <>
                        <button
                          type="button"
                          disabled={taskDecisionLoading || approvalExpired}
                          className="min-h-11 rounded-lg bg-white px-4 text-black disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white"
                          onClick={() => setTaskApprovalChoice("approve")}
                        >
                          Approve
                        </button>
                        <button
                          type="button"
                          disabled={taskDecisionLoading || approvalExpired}
                          className="min-h-11 rounded-lg border border-white/20 px-4 disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white"
                          onClick={() => setTaskApprovalChoice("reject")}
                        >
                          Reject
                        </button>
                      </>
                    )}
                  </div>
                ) : (
                  <p role="alert" className="text-amber-200">
                    Approval details are not available yet; task state is being refreshed.
                  </p>
                )}
              </>
            )}
            {taskDecisionStatus && (
              <p role="status" aria-live="polite" className="text-emerald-200">
                {taskDecisionStatus}
              </p>
            )}
            {taskDecisionError && (
              <p role="alert" className="text-red-300">{taskDecisionError}</p>
            )}
          </section>
        )}
        {error && (
          <p role="alert" className="px-4 text-sm text-red-300">
            {error}
          </p>
        )}
        {notesOpen && (
          <section className="border-t border-white/10 px-4 py-3 text-sm">
            <div className="mb-2 flex items-center justify-between">
              <p className="text-neutral-300">Notices</p>
              <button
                type="button"
                className="text-neutral-500"
                onClick={askBrowser}
              >
                Browser permission
              </button>
            </div>
            <ul className="space-y-2">
              {notes.length === 0 && (
                <li className="text-neutral-500">
                  Nothing waiting. Email is not configured.
                </li>
              )}
              {notes.map((item) => (
                <li
                  key={item.id}
                  className="flex items-start justify-between gap-3"
                >
                  <span>
                    {item.title}
                    <span className="mt-1 block text-neutral-500">
                      {item.body}
                    </span>
                  </span>
                  {!item.read && (
                    <button
                      type="button"
                      onClick={() => void markRead(item.id)}
                    >
                      Read
                    </button>
                  )}
                </li>
              ))}
            </ul>
            <form
              onSubmit={addReminder}
              className="mt-3 grid gap-2 sm:grid-cols-[1fr_auto_1fr_auto]"
            >
              <label className="sr-only" htmlFor="reminder-name">
                Reminder name
              </label>
              <input
                id="reminder-name"
                className="rounded-lg border border-white/10 bg-black/30 px-3 py-3"
                placeholder="Reminder name"
                value={reminderName}
                onChange={(e) => setReminderName(e.target.value)}
                required
              />
              <label className="sr-only" htmlFor="reminder-time">
                Reminder time
              </label>
              <input
                id="reminder-time"
                className="rounded-lg border border-white/10 bg-black/30 px-3 py-3"
                type="time"
                value={reminderTime}
                onChange={(e) => setReminderTime(e.target.value)}
                required
              />
              <label className="sr-only" htmlFor="reminder-timezone">
                Reminder time zone
              </label>
              <input
                id="reminder-timezone"
                className="rounded-lg border border-white/10 bg-black/30 px-3 py-3"
                value={reminderTimezone}
                onChange={(e) => setReminderTimezone(e.target.value)}
                placeholder="IANA time zone"
                required
              />
              <button
                type="submit"
                className="rounded-lg bg-white/10 px-3 py-3"
              >
                Save
              </button>
            </form>
            <ul className="mt-2 space-y-1 text-neutral-500">
              {reminders.map((item) => (
                <li key={item.id} className="flex justify-between">
                  <span>
                    {item.name} at {String(item.hour).padStart(2, "0")}:
                    {String(item.minute).padStart(2, "0")} {item.timezone}
                  </span>
                  <button
                    type="button"
                    onClick={() =>
                      void api.deleteReminder(item.id).then(loadNotes)
                    }
                  >
                    Remove
                  </button>
                </li>
              ))}
            </ul>
          </section>
        )}
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
        {settingsOpen && (
          <section
            id="account-settings-panel"
            aria-labelledby="settings-title"
            className="border-t border-white/10 px-4 py-3 text-sm"
          >
            <h2 id="settings-title" className="mb-3 font-medium text-neutral-200">
              Account settings
            </h2>
            {settingsLoading && (
              <p role="status" className="text-neutral-400">Loading settings…</p>
            )}
            <form onSubmit={savePreferences} className="grid gap-3 sm:max-w-lg">
              <label className="grid gap-1" htmlFor="preference-timezone">
                Time zone
                <input
                  id="preference-timezone"
                  disabled={!preferenceDraft || settingsLoading}
                  className="rounded-lg border border-white/10 bg-black/30 px-3 py-3 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white"
                  value={preferenceDraft?.timezone ?? ""}
                  onChange={(event) =>
                    setPreferenceDraft((current) =>
                      current ? { ...current, timezone: event.target.value } : current,
                    )
                  }
                  minLength={1}
                  maxLength={100}
                  required
                />
              </label>
              <label className="grid gap-1" htmlFor="preference-language">
                Briefing language
                <select
                  id="preference-language"
                  disabled={!preferenceDraft || settingsLoading}
                  className="rounded-lg border border-white/10 bg-black/30 px-3 py-3 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white"
                  value={preferenceDraft?.briefing_language ?? "en"}
                  onChange={(event) =>
                    setPreferenceDraft((current) =>
                      current
                        ? {
                            ...current,
                            briefing_language: event.target.value as Preferences["briefing_language"],
                          }
                        : current,
                    )
                  }
                >
                  <option value="en">English</option>
                  <option value="hi">Hindi</option>
                </select>
              </label>
              <button
                type="submit"
                disabled={!preferenceDraft || settingsLoading}
                className="min-h-11 rounded-lg bg-white/10 px-4 disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white sm:justify-self-start"
              >
                Save preferences
              </button>
            </form>
            <h3 className="mb-2 mt-5 font-medium text-neutral-200">Active sessions</h3>
            <ul className="space-y-2">
              {settingsSessions.map((item) => (
                <li
                  key={item.id}
                  className="flex flex-wrap items-center justify-between gap-3"
                >
                  <span>
                    {item.current ? "This session" : item.user_agent ?? "Unknown browser"}
                    <span className="block text-neutral-500">
                      Expires {new Date(item.expires_at).toLocaleString()}
                    </span>
                  </span>
                  <button
                    type="button"
                    disabled={settingsLoading}
                    className="min-h-11 rounded-lg border border-white/20 px-3 disabled:opacity-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-white"
                    onClick={() => void revokeUserSession(item.id)}
                  >
                    {item.current ? "Sign out this session" : "Revoke session"}
                  </button>
                </li>
              ))}
              {!settingsLoading && settingsSessions.length === 0 && (
                <li className="text-neutral-500">No active sessions found.</li>
              )}
            </ul>
            {settingsNotice && (
              <p role="status" aria-live="polite" className="mt-3 text-emerald-200">
                {settingsNotice}
              </p>
            )}
            {settingsError && (
              <p role="alert" className="mt-3 text-red-300">{settingsError}</p>
            )}
          </section>
        )}
        {canCapture === false && (
          <p role="status" className="px-4 text-sm text-neutral-400">
            Voice input is unavailable in this browser. Type your message instead.
          </p>
        )}
        {pendingText && (
          <p role="status" className="px-4 text-sm text-neutral-300">
            {pendingText}
          </p>
        )}
        {canCapture === true && (
          <p role="status" className="px-4 text-sm text-neutral-400">
            Your browser may ask for microphone access when you hold to talk. You can type instead.
          </p>
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
            aria-label="Hold to talk"
            aria-pressed={listening}
            className={`rounded-xl px-3 py-3 text-sm ${listening ? "bg-red-400 text-black" : "bg-white/10"}`}
            disabled={!active || sending || canCapture !== true}
            onMouseDown={holdToTalk}
            onMouseUp={releaseTalk}
            onMouseLeave={releaseTalk}
            onTouchStart={holdToTalk}
            onTouchEnd={releaseTalk}
            onTouchCancel={releaseTalk}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                void holdToTalk(event);
              }
            }}
            onKeyUp={(event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                void releaseTalk();
              }
            }}
            onBlur={() => void releaseTalk()}
          >
            {listening
              ? "Listening"
              : canCapture === null
                ? "Checking…"
                : canCapture
                  ? "Hold"
                  : "Unavailable"}
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
