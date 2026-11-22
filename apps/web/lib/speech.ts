/** Browser speech. Push-to-talk only. No wake word, no install.

The Web Speech API lives in the browser the user already has open. When it is
absent, callers fall back to the text field. Nothing here contacts a vendor.
*/

type Recognition = {
  lang: string;
  interimResults: boolean;
  continuous: boolean;
  start: () => void;
  stop: () => void;
  abort: () => void;
  onresult:
    | ((event: {
        results: ArrayLike<ArrayLike<{ transcript: string }>>;
      }) => void)
    | null;
  onerror: ((event: { error: string }) => void) | null;
  onend: (() => void) | null;
};

type RecognitionCtor = new () => Recognition;

function ctor(): RecognitionCtor | null {
  if (typeof window === "undefined") return null;
  const w = window as unknown as {
    SpeechRecognition?: RecognitionCtor;
    webkitSpeechRecognition?: RecognitionCtor;
  };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

export function captureAvailable(): boolean {
  return ctor() !== null;
}

export function playbackAvailable(): boolean {
  return typeof window !== "undefined" && "speechSynthesis" in window;
}

export function listen(
  language: string,
  onText: (text: string) => void,
  onEnd: () => void,
  onError: (error: string) => void,
): () => void {
  const Ctor = ctor();
  if (!Ctor) {
    onEnd();
    return () => undefined;
  }
  let recognition: Recognition;
  try {
    recognition = new Ctor();
  } catch {
    onError("startup-failed");
    onEnd();
    return () => undefined;
  }
  let stopped = false;
  recognition.lang = language;
  recognition.interimResults = false;
  recognition.continuous = false;
  recognition.onresult = (event) => {
    if (stopped) return;
    const transcript = event.results[0]?.[0]?.transcript ?? "";
    if (transcript.trim()) onText(transcript.trim());
  };
  recognition.onerror = (event) => {
    if (!stopped && event.error !== "aborted") onError(event.error);
    onEnd();
  };
  recognition.onend = () => onEnd();
  try {
    recognition.start();
  } catch {
    stopped = true;
    onError("startup-failed");
    onEnd();
    return () => undefined;
  }
  return () => {
    stopped = true;
    recognition.abort();
  };
}

/** Explain a browser recognition failure and preserve the text fallback. */
export function recognitionErrorMessage(error: string): string {
  switch (error) {
    case "not-allowed":
    case "service-not-allowed":
      return "Microphone access is blocked. Allow access in your browser or type your message.";
    case "audio-capture":
      return "No microphone is available. Check your device or type your message.";
    case "no-speech":
      return "No speech was detected. Try again or type your message.";
    case "network":
      return "Speech recognition is unavailable. Check your connection or type your message.";
    default:
      return "Speech recognition failed. Try again or type your message.";
  }
}

export function speak(text: string, language: string): void {
  if (!playbackAvailable() || !text.trim()) return;
  window.speechSynthesis.cancel();
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = language;
  window.speechSynthesis.speak(utterance);
}

export function stopSpeaking(): void {
  if (playbackAvailable()) window.speechSynthesis.cancel();
}

/** Whether MediaRecorder audio capture is available for server-side transcription. */
export function recordingAvailable(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof MediaRecorder !== "undefined" &&
    !!navigator.mediaDevices?.getUserMedia
  );
}

/** Record microphone audio into a Blob for POST /api/v1/voice/transcribe. */
export async function startRecording(): Promise<{ stop: () => Promise<Blob> }> {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  try {
    const recorder = new MediaRecorder(stream);
    let chunks: BlobPart[] = [];
    recorder.ondataavailable = (event) => {
      if (event.data.size > 0) chunks = [...chunks, event.data];
    };
    recorder.start();
    let stopped: Promise<Blob> | null = null;
    const stop = (): Promise<Blob> => {
      if (stopped) return stopped;
      stopped = new Promise((resolve) => {
        let finished = false;
        const finish = () => {
          if (finished) return;
          finished = true;
          recorder.onstop = null;
          stopTracks(stream);
          resolve(new Blob(chunks, { type: recorder.mimeType || "audio/webm" }));
        };
        recorder.onstop = finish;
        try {
          if (recorder.state === "inactive") finish();
          else recorder.stop();
        } catch {
          finish();
        }
      });
      return stopped;
    };
    return { stop };
  } catch (error) {
    stopTracks(stream);
    throw error;
  }
}

/** Stop every track owned by a captured media stream. */
function stopTracks(stream: MediaStream): void {
  stream.getTracks().forEach((track) => track.stop());
}

/** Play server audio; callers can use browser speech if playback is blocked. */
export async function playAudio(
  base64Audio: string,
  format: string,
 ): Promise<boolean> {
  if (typeof window === "undefined") return false;
  let url = "";
  try {
    const mime = format === "mp3" ? "audio/mpeg" : `audio/${format}`;
    const binary = atob(base64Audio);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    url = URL.createObjectURL(new Blob([bytes], { type: mime }));
    const audio = new Audio(url);
    const cleanup = () => URL.revokeObjectURL(url);
    audio.addEventListener("ended", cleanup, { once: true });
    audio.addEventListener("error", cleanup, { once: true });
    await audio.play();
    return true;
  } catch {
    if (url) URL.revokeObjectURL(url);
    return false;
  }
}

/** OpenAI Realtime WebRTC SDP exchange. */
export const REALTIME_CALLS_URL = "https://api.openai.com/v1/realtime/calls";

export type RealtimePeer = {
  close: () => void;
  mute: (muted: boolean) => void;
  interrupt: () => void;
  readonly closed: boolean;
};

export type RealtimeHandlers = {
  onInterim?: (text: string) => void;
  onCommand?: (text: string, callId: string) => void;
  onInterrupt?: () => void;
  onRemoteAudio?: (stream: MediaStream) => void;
  onAudioFallback?: (text: string) => void;
  onError?: (message: string) => void;
  onClose?: () => void;
};

type RealtimeEvent = {
  type?: string;
  transcript?: string;
  delta?: string;
  name?: string;
  call_id?: string;
  arguments?: string;
};

type RealtimeResources = {
  pc: RTCPeerConnection;
  audio: HTMLAudioElement | null;
  stream: MediaStream | null;
  channel: RTCDataChannel | null;
  signal?: AbortSignal;
  abortListener?: () => void;
  closed: boolean;
  responseInProgress: boolean;
  playbackBlocked: boolean;
  outputTranscript: string;
};

export function realtimeAvailable(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof RTCPeerConnection !== "undefined" &&
    !!navigator.mediaDevices?.getUserMedia
  );
}

/** Stop and detach a remote audio stream during peer cleanup. */
export function dropQueuedAudio(audio: HTMLAudioElement | null): void {
  if (!audio) return;
  audio.pause();
  audio.srcObject = null;
}

/** Accept only a non-empty command string from validated tool arguments. */
function parseToolText(raw: string | undefined): string {
  if (!raw) return "";
  try {
    const parsed = JSON.parse(raw) as { command?: unknown };
    return typeof parsed.command === "string" ? parsed.command.trim() : "";
  } catch {
    return "";
  }
}

/** Apply provider events to peer state and the corresponding UI handlers. */
function handleRealtimeEvent(
  event: RealtimeEvent,
  resources: RealtimeResources,
  handlers: RealtimeHandlers,
  pauseOutput: () => void,
  resumeOutput: () => void,
  finishResponse: () => void,
 ): void {
  if (event.type === "input_audio_buffer.speech_started") {
    // WebRTC lets the server cancel and truncate audio it knows was not played.
    handlers.onInterrupt?.();
    pauseOutput();
  } else if (event.type === "response.created") {
    resources.responseInProgress = true;
    resources.outputTranscript = "";
    resumeOutput();
  } else if (event.type === "response.done") {
    resources.responseInProgress = false;
    finishResponse();
  } else if (event.type === "response.output_audio_transcript.delta" || event.type === "response.audio_transcript.delta") {
    resources.outputTranscript += event.delta ?? "";
    if (event.delta) handlers.onInterim?.(event.delta);
  } else if (event.type === "response.output_audio_transcript.done") {
    resources.outputTranscript = event.transcript ?? resources.outputTranscript;
  } else if (event.type === "conversation.item.input_audio_transcription.delta") {
    if (event.delta) handlers.onInterim?.(event.delta);
  } else if (event.type === "conversation.item.input_audio_transcription.completed") {
    if (event.transcript?.trim()) handlers.onInterim?.(event.transcript.trim());
  } else if (event.type === "response.function_call_arguments.done") {
    if (event.name !== "submit_command") {
      handlers.onError?.("Realtime requested an unsupported tool.");
      return;
    }
    const text = parseToolText(event.arguments);
    if (text && event.call_id) handlers.onCommand?.(text, event.call_id);
  }
}

/** Close the data channel, stop microphone tracks, and release the peer. */
function closeRealtimeResources(
  resources: RealtimeResources,
  handlers: RealtimeHandlers,
 ): void {
  if (resources.closed) return;
  resources.closed = true;
  if (resources.signal && resources.abortListener) {
    resources.signal.removeEventListener("abort", resources.abortListener);
  }
  resources.channel?.close();
  if (resources.stream) stopTracks(resources.stream);
  dropQueuedAudio(resources.audio);
  resources.pc.close();
  handlers.onClose?.();
}

/** Silence remote output while server-side VAD handles an interruption. */
function pauseRealtimeOutput(resources: RealtimeResources): void {
  stopSpeaking();
  if (resources.audio) resources.audio.muted = true;
}

/** Speak the transcript when the browser blocked remote audio playback. */
function finishRealtimeResponse(
  resources: RealtimeResources,
  handlers: RealtimeHandlers,
 ): void {
  if (resources.playbackBlocked && resources.outputTranscript.trim()) {
    handlers.onAudioFallback?.(resources.outputTranscript.trim());
  }
}

/** Resume the remote media track and retain a text fallback on rejection. */
function resumeRealtimeOutput(
  resources: RealtimeResources,
  handlers: RealtimeHandlers,
 ): void {
  const audio = resources.audio;
  if (!audio) return;
  audio.muted = false;
  void audio.play().then(
    () => {
      resources.playbackBlocked = false;
    },
    () => {
      resources.playbackBlocked = true;
      if (!resources.responseInProgress) finishRealtimeResponse(resources, handlers);
    },
  );
}

/** Cancel an active response and mute output until the next response. */
function cancelRealtimeResponse(
  resources: RealtimeResources,
  handlers: RealtimeHandlers,
 ): void {
  if (resources.responseInProgress && resources.channel?.readyState === "open") {
    try {
      resources.channel.send(JSON.stringify({ type: "response.cancel" }));
    } catch {
      handlers.onError?.("Could not stop the realtime response.");
    }
  }
  pauseRealtimeOutput(resources);
}

/** Expose peer operations without leaking the underlying resources. */
function createRealtimePeer(
  resources: RealtimeResources,
  handlers: RealtimeHandlers,
 ): RealtimePeer {
  return {
    get closed() {
      return resources.closed;
    },
    close: () => closeRealtimeResources(resources, handlers),
    mute: (muted) => {
      for (const track of resources.stream?.getAudioTracks() ?? []) {
        track.enabled = !muted;
      }
    },
    interrupt: () => cancelRealtimeResponse(resources, handlers),
  };
}

/** Exchange the local SDP offer for a remote answer. */
async function exchangeRealtimeSdp(
  resources: RealtimeResources,
  clientSecret: string,
 ): Promise<void> {
  const offer = await resources.pc.createOffer();
  if (resources.closed || resources.signal?.aborted) throw new DOMException("Realtime setup was cancelled.", "AbortError");
  await resources.pc.setLocalDescription(offer);
  const response = await fetch(REALTIME_CALLS_URL, {
    method: "POST",
    body: offer.sdp,
    headers: {
      Authorization: `Bearer ${clientSecret}`,
      "Content-Type": "application/sdp",
    },
    signal: resources.signal,
  });
  if (!response.ok) throw new Error("Realtime call was rejected.");
  const sdp = await response.text();
  if (resources.closed || resources.signal?.aborted) throw new DOMException("Realtime setup was cancelled.", "AbortError");
  await resources.pc.setRemoteDescription({ type: "answer", sdp });
}

/** Acquire audio only after the caller has validated the session mint. */
async function setupRealtimePeer(
  resources: RealtimeResources,
  clientSecret: string,
  handlers: RealtimeHandlers,
 ): Promise<void> {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  resources.stream = stream;
  if (resources.closed || resources.signal?.aborted) {
    stopTracks(stream);
    throw new DOMException("Realtime setup was cancelled.", "AbortError");
  }
  for (const track of stream.getTracks()) resources.pc.addTrack(track, stream);
  resources.pc.ontrack = (event) => {
    const remote = event.streams[0];
    if (resources.audio && remote) {
      resources.audio.srcObject = remote;
      resumeRealtimeOutput(resources, handlers);
    }
    if (remote) handlers.onRemoteAudio?.(remote);
  };
  resources.channel = resources.pc.createDataChannel("oai-events");
  resources.channel.onmessage = ({ data }) => {
    if (resources.closed) return;
    try {
      handleRealtimeEvent(
        JSON.parse(String(data)) as RealtimeEvent,
        resources,
        handlers,
        () => pauseRealtimeOutput(resources),
        () => resumeRealtimeOutput(resources, handlers),
        () => finishRealtimeResponse(resources, handlers),
      );
    } catch {
      handlers.onError?.("Realtime event could not be read.");
    }
  };
  await exchangeRealtimeSdp(resources, clientSecret);
}

/** Open a realtime peer only after the caller has received a valid mint. */
export async function openRealtimePeer(
  clientSecret: string,
  handlers: RealtimeHandlers = {},
  signal?: AbortSignal,
 ): Promise<RealtimePeer> {
  if (signal?.aborted) throw new DOMException("Realtime setup was cancelled.", "AbortError");
  const resources: RealtimeResources = {
    pc: new RTCPeerConnection(),
    audio: typeof Audio !== "undefined" ? new Audio() : null,
    stream: null,
    channel: null,
    signal,
    closed: false,
    responseInProgress: false,
    playbackBlocked: false,
    outputTranscript: "",
  };
  if (resources.audio) resources.audio.autoplay = true;
  resources.abortListener = () => closeRealtimeResources(resources, handlers);
  signal?.addEventListener("abort", resources.abortListener, { once: true });
  resources.pc.onconnectionstatechange = () => {
    if (resources.pc.connectionState !== "failed") return;
    handlers.onError?.("Realtime connection failed.");
    closeRealtimeResources(resources, handlers);
  };
  try {
    await setupRealtimePeer(resources, clientSecret, handlers);
    if (resources.closed || signal?.aborted) throw new DOMException("Realtime setup was cancelled.", "AbortError");
    return createRealtimePeer(resources, handlers);
  } catch (error) {
    closeRealtimeResources(resources, handlers);
    throw error;
  }
}
