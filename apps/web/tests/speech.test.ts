import { afterEach, describe, expect, it, vi } from "vitest";
import { listen, openRealtimePeer, startRecording } from "../lib/speech";

type FakeChannel = {
  readyState: string;
  onmessage: ((event: { data: string }) => void) | null;
  close: ReturnType<typeof vi.fn>;
  send: ReturnType<typeof vi.fn>;
};

class TestChannel implements FakeChannel {
  readyState = "open";
  onmessage: FakeChannel["onmessage"] = null;
  close = vi.fn();
  send = vi.fn();

  emit(event: Record<string, unknown>) {
    this.onmessage?.({ data: JSON.stringify(event) });
  }
}

class TestAudio {
  static latest: TestAudio;
  autoplay = false;
  srcObject: MediaStream | null = null;
  muted = false;
  play = vi.fn(() => Promise.resolve());
  pause = vi.fn();

  constructor() {
    TestAudio.latest = this;
  }
}

class TestPeerConnection {
  static latest: TestPeerConnection;
  connectionState = "new";
  onconnectionstatechange: (() => void) | null = null;
  ontrack: ((event: { streams: MediaStream[] }) => void) | null = null;
  addTrack = vi.fn();
  close = vi.fn();
  createOffer = vi.fn(async () => ({ sdp: "offer" }));
  setLocalDescription = vi.fn(async () => undefined);
  setRemoteDescription = vi.fn(async () => undefined);
  channel = new TestChannel();

  constructor() {
    TestPeerConnection.latest = this;
  }

  createDataChannel() {
    return this.channel;
  }
}

function mockRealtimeBrowser() {
  const track = { stop: vi.fn(), enabled: true };
  const stream = {
    getTracks: () => [track],
    getAudioTracks: () => [track],
  } as unknown as MediaStream;
  const getUserMedia = vi.fn(async () => stream);
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: { getUserMedia },
  });
  vi.stubGlobal("Audio", TestAudio);
  vi.stubGlobal("RTCPeerConnection", TestPeerConnection);
  vi.stubGlobal("fetch", vi.fn<typeof fetch>().mockResolvedValue(new Response("answer")));
  return { track, stream, getUserMedia };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("speech resource behavior", () => {
  it("stops browser recognition after cleanup", () => {
    let emitResult: (transcript: string) => void = () => undefined;
    let aborted = false;
    class TestRecognition {
      lang = "";
      interimResults = false;
      continuous = false;
      start = vi.fn();
      stop = vi.fn();
      onresult: ((event: { results: ArrayLike<ArrayLike<{ transcript: string }>> }) => void) | null = null;
      onerror: ((event: { error: string }) => void) | null = null;
      onend: (() => void) | null = null;

      abort() {
        aborted = true;
      }

      constructor() {
        emitResult = (transcript) =>
          this.onresult?.({ results: [[{ transcript }]] });
      }
    }
    Object.defineProperty(window, "SpeechRecognition", {
      configurable: true,
      value: TestRecognition,
    });
    const onText = vi.fn();
    const cleanup = listen("en-US", onText, vi.fn(), vi.fn());
    emitResult("  hello  ");

    cleanup();
    emitResult("ignored");

    expect(onText).toHaveBeenCalledOnce();
    expect(onText).toHaveBeenCalledWith("hello");
    expect(aborted).toBe(true);
  });
 

  it("stops recording tracks once when a recording is released", async () => {
    const { track, stream, getUserMedia } = mockRealtimeBrowser();
    class TestRecorder {
      state = "recording";
      mimeType = "audio/webm";
      ondataavailable: ((event: { data: Blob }) => void) | null = null;
      onstop: ((event: Event) => void) | null = null;
      start = vi.fn();
      stop = vi.fn(() => {
        this.state = "inactive";
        this.onstop?.(new Event("stop"));
      });
    }
    vi.stubGlobal("MediaRecorder", TestRecorder);

    const recorder = await startRecording();
    const firstStop = recorder.stop();
    const secondStop = recorder.stop();

    expect(await firstStop).toBeInstanceOf(Blob);
    expect(secondStop).toBe(firstStop);
    expect(getUserMedia).toHaveBeenCalledWith({ audio: true });
    expect(track.stop).toHaveBeenCalledOnce();
    expect(stream.getTracks()).toHaveLength(1);
  });

  it("finishes cleanup if the recorder is already inactive", async () => {
    const { track } = mockRealtimeBrowser();
    const stopRecorder = vi.fn();
    class TestRecorder {
      static latest: TestRecorder;
      state = "recording";
      mimeType = "audio/webm";
      ondataavailable: ((event: { data: Blob }) => void) | null = null;
      onstop: ((event: Event) => void) | null = null;
      start = vi.fn();
      stop = stopRecorder;

      constructor() {
        TestRecorder.latest = this;
      }
    }
    vi.stubGlobal("MediaRecorder", TestRecorder);

    const recorder = await startRecording();
    TestRecorder.latest.state = "inactive";
    const blob = await recorder.stop();

    expect(blob).toBeInstanceOf(Blob);
    expect(stopRecorder).not.toHaveBeenCalled();
    expect(track.stop).toHaveBeenCalledOnce();
  });

  it("validates realtime tool names and accepts only a string command", async () => {
    mockRealtimeBrowser();
    const onCommand = vi.fn();
    const onError = vi.fn();
    const peer = await openRealtimePeer("mint-secret", { onCommand, onError });
    const channel = TestPeerConnection.latest.channel;

    channel.emit({
      type: "response.function_call_arguments.done",
      name: "delete_everything",
      call_id: "bad-tool",
      arguments: '{"command":"run"}',
    });
    channel.emit({
      type: "response.function_call_arguments.done",
      name: "submit_command",
      call_id: "bad-args",
      arguments: '{"command":42}',
    });
    channel.emit({
      type: "response.function_call_arguments.done",
      name: "submit_command",
      call_id: "valid-call",
      arguments: '{"command":"  remind me  "}',
    });

    expect(onError).toHaveBeenCalledWith("Realtime requested an unsupported tool.");
    expect(onCommand).toHaveBeenCalledOnce();
    expect(onCommand).toHaveBeenCalledWith("remind me", "valid-call");
    peer.close();
  });

  it("interrupts output, falls back to transcript, and releases realtime resources", async () => {
    const { track } = mockRealtimeBrowser();
    const onInterrupt = vi.fn();
    const onAudioFallback = vi.fn();
    const onClose = vi.fn();
    const peer = await openRealtimePeer("mint-secret", {
      onInterrupt,
      onAudioFallback,
      onClose,
    });
    const connection = TestPeerConnection.latest;
    const audio = TestAudio.latest;
    const channel = connection.channel;
    audio.play.mockRejectedValue(new Error("playback blocked"));

    channel.emit({ type: "input_audio_buffer.speech_started" });
    expect(onInterrupt).toHaveBeenCalledOnce();
    expect(audio.muted).toBe(true);

    channel.emit({ type: "response.created" });
    await Promise.resolve();
    peer.interrupt();
    expect(channel.send).toHaveBeenCalledWith('{"type":"response.cancel"}');
    expect(audio.muted).toBe(true);
    channel.emit({ type: "response.audio_transcript.delta", delta: "Say " });
    channel.emit({ type: "response.audio_transcript.delta", delta: "hello." });
    channel.emit({ type: "response.done" });

    expect(onAudioFallback).toHaveBeenCalledWith("Say hello.");
    peer.close();

    expect(track.stop).toHaveBeenCalledOnce();
    expect(channel.close).toHaveBeenCalledOnce();
    expect(connection.close).toHaveBeenCalledOnce();
    expect(audio.pause).toHaveBeenCalledOnce();
    expect(onClose).toHaveBeenCalledOnce();
    expect(peer.closed).toBe(true);
  });
});