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
): () => void {
  const Ctor = ctor();
  if (!Ctor) {
    onEnd();
    return () => undefined;
  }
  const recognition = new Ctor();
  let stopped = false;
  recognition.lang = language;
  recognition.interimResults = false;
  recognition.continuous = false;
  recognition.onresult = (event) => {
    if (stopped) return;
    const transcript = event.results[0]?.[0]?.transcript ?? "";
    if (transcript.trim()) onText(transcript.trim());
  };
  recognition.onerror = () => onEnd();
  recognition.onend = () => onEnd();
  recognition.start();
  return () => {
    stopped = true;
    recognition.abort();
  };
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
