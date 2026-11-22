import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Home from "../app/page";

const doubles = vi.hoisted(() => {
  class MockApiError extends Error {
    constructor(public status: number, message: string) {
      super(message);
    }
  }
  return {
    ApiError: MockApiError,
    realtimeHandlers: null as null | {
      onCommand?: (text: string, callId: string) => void;
    },
    api: {
      session: vi.fn(),
      conversations: vi.fn(),
      health: vi.fn(),
      notifications: vi.fn(),
      reminders: vi.fn(),
      sync: vi.fn(),
      messages: vi.fn(),
      task: vi.fn(),
      voiceSession: vi.fn(),
      openRealtimeSession: vi.fn(),
      submitRealtimeCommand: vi.fn(),
      createReminder: vi.fn(),
      integrations: vi.fn(),
      finishIntegration: vi.fn(),
    },
    speech: {
      captureAvailable: vi.fn(),
      listen: vi.fn(),
      openRealtimePeer: vi.fn(),
      playAudio: vi.fn(),
      realtimeAvailable: vi.fn(),
      recognitionErrorMessage: vi.fn(),
      recordingAvailable: vi.fn(),
      speak: vi.fn(),
      startRecording: vi.fn(),
      stopSpeaking: vi.fn(),
    },
  };
});

vi.mock("@/lib/api", () => ({
  ApiError: doubles.ApiError,
  api: doubles.api,
  clearToken: vi.fn(),
  saveToken: vi.fn(),
  canOpenRealtime: (session: { client_secret?: string; transport?: string }) =>
    Boolean(session.client_secret) && session.transport === "webrtc",
  mintFallsBack: (status: number) => status === 503,
  streamMessage: vi.fn(),
}));

vi.mock("@/lib/speech", () => doubles.speech);

const peer = {
  close: vi.fn(),
  mute: vi.fn(),
  interrupt: vi.fn(),
  closed: false,
};

async function openConversation() {
  render(<Home />);
  await screen.findByText("Ada");
  fireEvent.click(await screen.findByRole("button", { name: "Inbox" }));
  await waitFor(() => expect(doubles.api.messages).toHaveBeenCalledWith("c1"));
}

async function holdToTalk() {
  const button = await screen.findByRole("button", { name: "Hold to talk" });
  await waitFor(() => expect(button).toBeEnabled());
  fireEvent.mouseDown(button);
  return button;
}

beforeEach(() => {
  vi.clearAllMocks();
  doubles.realtimeHandlers = null;
  window.history.replaceState({}, "", "/");
  doubles.api.session.mockResolvedValue({
    user: { id: "u1", email: "ada@example.test", display_name: "Ada" },
  });
  doubles.api.conversations.mockResolvedValue({
    conversations: [{ id: "c1", title: "Inbox", status: "active" }],
  });
  doubles.api.health.mockResolvedValue({
    ai: { configured: true, model: "test" },
    speech: { stt_configured: false, tts_configured: false, locales: [], browser_capture: false },
  });
  doubles.api.notifications.mockResolvedValue({ notifications: [], unread: 0 });
  doubles.api.integrations.mockResolvedValue({ integrations: [] });
  doubles.api.finishIntegration.mockResolvedValue({
    connected: true,
    provider: "google",
  });
  doubles.api.reminders.mockResolvedValue({ reminders: [] });
  doubles.api.sync.mockResolvedValue({ events: [], cursor: 0 });
  doubles.api.messages.mockResolvedValue({ messages: [] });
  doubles.api.voiceSession.mockResolvedValue({ language: "en-US" });
  doubles.api.openRealtimeSession.mockResolvedValue({
    transport: "webrtc",
    client_secret: "mint-secret",
  });
  doubles.api.submitRealtimeCommand.mockResolvedValue({
    request_id: "r1",
    conversation_id: "c1",
    task_id: "task-7",
    status: "waiting_for_approval",
    response_text: "Queued",
    voice_response: "Queued",
    requires_approval: true,
    source: "web",
  });
  doubles.api.task.mockResolvedValue({
    id: "task-7",
    title: "Task",
    status: "waiting_for_approval",
    created_at: "",
    approval_action_hash: "approval-hash",
    approval_expires_at: null,
    error: null,
    steps: [
      {
        position: 0,
        agent_id: "research",
        action: "Do the task",
        risk: "low",
        status: "pending",
        attempt: 0,
      },
    ],
    job: null,
  });
  doubles.api.createReminder.mockResolvedValue({ id: "rem-1" });
  doubles.speech.captureAvailable.mockReturnValue(false);
  doubles.speech.realtimeAvailable.mockReturnValue(true);
  doubles.speech.recordingAvailable.mockReturnValue(false);
  doubles.speech.listen.mockReturnValue(vi.fn());
  doubles.speech.openRealtimePeer.mockImplementation(async (_secret, handlers) => {
    doubles.realtimeHandlers = handlers;
    return peer;
  });
});

describe("authentication and integration redirects", () => {
  it("switches from sign-in to account registration", async () => {
    doubles.api.session.mockRejectedValue(new Error("Not signed in"));
    render(<Home />);

    expect(await screen.findByRole("heading", { name: "Welcome back" }))
      .toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "Need an account? Register" }),
    );

    expect(await screen.findByRole("heading", { name: "Create your account" }))
      .toBeInTheDocument();
    expect(screen.getByPlaceholderText("Name")).toBeInTheDocument();
  });

  it("finishes an OAuth redirect once without repeating the callback", async () => {
    window.history.replaceState({}, "", "/?code=oauth-code&state=oauth-state");
    render(<Home />);

    await waitFor(() =>
      expect(doubles.api.integrations).toHaveBeenCalledOnce(),
    );
    expect(doubles.api.finishIntegration).toHaveBeenCalledOnce();
    expect(doubles.api.finishIntegration).toHaveBeenCalledWith(
      "oauth-code",
      "oauth-state",
      "",
    );
  });
});

describe("mocked frontend workflows", () => {
  it("does not open realtime capture until the mint has a valid WebRTC secret", async () => {
    doubles.api.openRealtimeSession.mockResolvedValue({
      transport: "webrtc",
      client_secret: "",
    });
    await openConversation();
    await holdToTalk();

    expect(await screen.findByText("The realtime session is unavailable. Type instead."))
      .toBeInTheDocument();
    expect(doubles.speech.openRealtimePeer).not.toHaveBeenCalled();
  });

  it("uses browser recognition only when mint returns 503", async () => {
    doubles.speech.captureAvailable.mockReturnValue(true);
    doubles.api.openRealtimeSession.mockRejectedValue(
      new doubles.ApiError(503, "Realtime is unavailable"),
    );
    await openConversation();
    await holdToTalk();

    await waitFor(() => expect(doubles.speech.listen).toHaveBeenCalledOnce());
    expect(doubles.speech.openRealtimePeer).not.toHaveBeenCalled();
  });

  it.each(["other HTTP error", "network error"])(`does not start microphone fallback after a %s`, async (failure) => {
    const error = failure === "network error"
      ? new Error("offline")
      : new doubles.ApiError(500, "server error");
    doubles.api.openRealtimeSession.mockRejectedValue(error);
    doubles.speech.captureAvailable.mockReturnValue(true);
    await openConversation();
    await holdToTalk();

    await screen.findByRole("alert");
    expect(doubles.speech.listen).not.toHaveBeenCalled();
    expect(doubles.speech.openRealtimePeer).not.toHaveBeenCalled();
  });

  it("shows task approval state and ignores a duplicate realtime tool call", async () => {
    await openConversation();
    await holdToTalk();
    await waitFor(() =>
      expect(doubles.speech.openRealtimePeer).toHaveBeenCalledWith(
        "mint-secret",
        expect.any(Object),
        expect.any(AbortSignal),
      ),
    );
    const commandHandler = doubles.realtimeHandlers?.onCommand;
    expect(commandHandler).toBeDefined();
    commandHandler?.("Do the task", "call-1");

    expect(await screen.findByText(/Task task-7: waiting for approval/))
      .toHaveTextContent(
        "Task task-7: waiting for approval · Waiting for your approval.",
      );
    expect(await screen.findByRole("button", { name: "Approve" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeEnabled();
    await waitFor(() => expect(doubles.api.submitRealtimeCommand).toHaveBeenCalledOnce());
    const idempotencyKey = doubles.api.submitRealtimeCommand.mock.calls[0]?.[2];
    expect(idempotencyKey).toEqual(expect.any(String));

    commandHandler?.("Do the task", "call-1");
    await Promise.resolve();
    expect(doubles.api.submitRealtimeCommand).toHaveBeenCalledOnce();
  });

  it("saves reminder time with the entered timezone", async () => {
    await openConversation();
    fireEvent.click(screen.getByRole("button", { name: "Notices" }));
    fireEvent.change(screen.getByLabelText("Reminder name"), {
      target: { value: "Daily standup" },
    });
    fireEvent.change(screen.getByLabelText("Reminder time"), {
      target: { value: "09:45" },
    });
    fireEvent.change(screen.getByLabelText("Reminder time zone"), {
      target: { value: "America/Los_Angeles" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(doubles.api.createReminder).toHaveBeenCalledWith(
        "Daily standup",
        "",
        9,
        45,
        "America/Los_Angeles",
      ),
    );
  });
});