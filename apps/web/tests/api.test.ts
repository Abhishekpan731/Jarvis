import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  api,
  canOpenRealtime,
  mintFallsBack,
  REFRESH_PATH,
  saveToken,
  shouldRefresh,
  streamMessage,
} from "../lib/api";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function mockFetch() {
  const fetchMock = vi.fn<typeof fetch>();
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("authenticated API behavior", () => {
  let fetchMock: ReturnType<typeof mockFetch>;

  beforeEach(() => {
    fetchMock = mockFetch();
    saveToken("expired-token");
  });

  it("refreshes once after a JSON request receives 401", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse({}, 401))
      .mockResolvedValueOnce(jsonResponse({ access_token: "fresh-token" }))
      .mockResolvedValueOnce(jsonResponse({ user: { id: "u1" } }));

    await expect(api.session()).resolves.toEqual({ user: { id: "u1" } });

    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(new Headers(fetchMock.mock.calls[0]?.[1]?.headers).get("Authorization"))
      .toBe("Bearer expired-token");
    expect(String(fetchMock.mock.calls[1]?.[0])).toContain(REFRESH_PATH);
    expect(new Headers(fetchMock.mock.calls[2]?.[1]?.headers).get("Authorization"))
      .toBe("Bearer fresh-token");
  });

  it("does not recurse when the retried request also receives 401", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse({}, 401))
      .mockResolvedValueOnce(jsonResponse({ access_token: "fresh-token" }))
      .mockResolvedValueOnce(jsonResponse({}, 401));

    await expect(api.session()).rejects.toMatchObject({ status: 401 });

    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls.filter(([url]) => String(url).includes(REFRESH_PATH)))
      .toHaveLength(1);
  });

  it("shares one refresh request across concurrent 401 responses", async () => {
    let resolveRefresh: (response: Response) => void = () => undefined;
    const refreshResponse = new Promise<Response>((resolve) => {
      resolveRefresh = resolve;
    });
    let initialRequests = 0;
    fetchMock.mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes(REFRESH_PATH)) return refreshResponse;
      initialRequests += 1;
      return initialRequests <= 2
        ? jsonResponse({}, 401)
        : jsonResponse({ user: { id: "u1" } });
    });

    const requests = [api.session(), api.session()];
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    resolveRefresh(jsonResponse({ access_token: "fresh-token" }));

    await expect(Promise.all(requests)).resolves.toHaveLength(2);
    expect(fetchMock.mock.calls.filter(([url]) => String(url).includes(REFRESH_PATH)))
      .toHaveLength(1);
  });

  it("refreshes before retrying a streaming request", async () => {
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(
          new TextEncoder().encode('event: text.delta\ndata: {"text":"hello"}\n\n'),
        );
        controller.close();
      },
    });
    fetchMock
      .mockResolvedValueOnce(jsonResponse({}, 401))
      .mockResolvedValueOnce(jsonResponse({ access_token: "fresh-token" }))
      .mockResolvedValueOnce(new Response(stream, { status: 200 }));
    const deltas: string[] = [];

    await streamMessage(
      "c1",
      "hello",
      (text) => deltas.push(text),
      new AbortController().signal,
    );

    expect(deltas).toEqual(["hello"]);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(new Headers(fetchMock.mock.calls[2]?.[1]?.headers).get("Authorization"))
      .toBe("Bearer fresh-token");
  });

  it("excludes the refresh path and a retried request from refresh eligibility", () => {
    expect(shouldRefresh(REFRESH_PATH, 401, false)).toBe(false);
    expect(shouldRefresh("/api/v1/auth/session", 401, true)).toBe(false);
    expect(shouldRefresh("/api/v1/auth/session", 401, false)).toBe(true);
  });

  it("requires a minted WebRTC secret and only falls back for 503", () => {
    expect(canOpenRealtime({ client_secret: "secret", transport: "webrtc" })).toBe(true);
    expect(canOpenRealtime({ transport: "webrtc" })).toBe(false);
    expect(canOpenRealtime({ client_secret: "secret", transport: "other" })).toBe(false);
    expect(mintFallsBack(503)).toBe(true);
    expect(mintFallsBack(500)).toBe(false);
    expect(mintFallsBack(401)).toBe(false);
  });

  it("sends stable realtime idempotency data in header and body", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ status: "accepted" }));

    await api.submitRealtimeCommand("c1", "Do the task", "key-1", "en");

    const [, init] = fetchMock.mock.calls[0] ?? [];
    expect(new Headers(init?.headers).get("Idempotency-Key")).toBe("key-1");
    expect(JSON.parse(String(init?.body))).toMatchObject({
      command: "Do the task",
      conversation_id: "c1",
      idempotency_key: "key-1",
      input_type: "voice",
    });
  });
});