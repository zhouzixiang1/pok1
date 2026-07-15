export interface EventSourceEventLike {
  data?: string;
}

export interface EventSourceLike {
  onopen: (() => void) | null;
  onerror: (() => void) | null;
  addEventListener(
    type: string,
    listener: (event: EventSourceEventLike) => void,
  ): void;
  close(): void;
}

export interface EventSourceScheduler {
  setTimeout(callback: () => void, delayMs: number): unknown;
  clearTimeout(handle: unknown): void;
  now(): number;
}

export interface EventSourceControllerDependencies {
  createSource?: (url: string) => EventSourceLike;
  scheduler?: EventSourceScheduler;
}

export interface EventSourceControllerOptions {
  url: string;
  events: readonly string[];
  reconnectDelayMs?: number;
  pingEvent?: string;
  epochBlockedEvent?: string;
  onConnecting?: () => void;
  onOpen?: () => void;
  validatePing?: (data: unknown) => boolean;
  validateEvent?: (eventType: string, data: unknown) => boolean;
  validateEpochBlocked?: (data: unknown) => boolean;
  onEvent?: (eventType: string, data: unknown) => void;
  onLiveness?: (eventType: string, observedAt: number) => void;
  onMalformed?: (eventType: string) => void;
  onTransportError?: () => void;
  onEpochFence?: () => void;
  onEpochBlocked?: (data: unknown) => void;
}

export interface EventSourceController {
  start(): () => void;
  stop(): void;
}

const defaultScheduler: EventSourceScheduler = {
  setTimeout: (callback, delayMs) => globalThis.setTimeout(callback, delayMs),
  clearTimeout: (handle) => globalThis.clearTimeout(handle as number),
  now: () => Date.now(),
};

function defaultCreateSource(url: string): EventSourceLike {
  return new EventSource(url) as unknown as EventSourceLike;
}

export function createEventSourceController(
  options: EventSourceControllerOptions,
  dependencies: EventSourceControllerDependencies = {},
): EventSourceController {
  const createSource = dependencies.createSource ?? defaultCreateSource;
  const scheduler = dependencies.scheduler ?? defaultScheduler;
  const reconnectDelayMs = options.reconnectDelayMs ?? 5_000;
  const registeredEvents = new Set(options.events);
  if (options.pingEvent) registeredEvents.add(options.pingEvent);
  if (options.epochBlockedEvent) registeredEvents.add(options.epochBlockedEvent);

  let active = false;
  let authorityBlocked = false;
  let generation = 0;
  let currentSource: EventSourceLike | null = null;
  let reconnectTimer: unknown | null = null;

  const cancelReconnect = () => {
    if (reconnectTimer === null) return;
    scheduler.clearTimeout(reconnectTimer);
    reconnectTimer = null;
  };

  const isCurrent = (source: EventSourceLike, sourceGeneration: number) => (
    active
    && !authorityBlocked
    && currentSource === source
    && generation === sourceGeneration
  );

  const scheduleReconnect = () => {
    if (!active || authorityBlocked || reconnectTimer !== null) return;
    const handle = scheduler.setTimeout(() => {
      if (reconnectTimer !== handle) return;
      reconnectTimer = null;
      if (active && !authorityBlocked) connect();
    }, reconnectDelayMs);
    reconnectTimer = handle;
  };

  function connect() {
    if (!active || authorityBlocked) return;
    cancelReconnect();
    const sourceGeneration = ++generation;
    options.onConnecting?.();

    let source: EventSourceLike;
    try {
      source = createSource(options.url);
    } catch {
      options.onTransportError?.();
      scheduleReconnect();
      return;
    }
    currentSource = source;

    source.onopen = () => {
      if (!isCurrent(source, sourceGeneration)) return;
      options.onOpen?.();
    };

    registeredEvents.forEach((eventType) => {
      source.addEventListener(eventType, (event) => {
        if (!isCurrent(source, sourceGeneration)) return;
        if (eventType === options.epochBlockedEvent) {
          // The event name itself is the transport-level authority fence.  Do
          // not leave a malicious/truncated payload connected long enough to
          // deliver stale data or schedule a reconnect.  Payload validation is
          // deliberately downstream of this irreversible local fence.
          authorityBlocked = true;
          currentSource = null;
          cancelReconnect();
          source.close();
          options.onEpochFence?.();

          try {
            if (typeof event.data !== "string") throw new Error("missing SSE data");
            const data: unknown = JSON.parse(event.data);
            if (options.validateEpochBlocked?.(data) === false) {
              throw new Error("invalid epoch-blocked payload");
            }
            options.onEpochBlocked?.(data);
          } catch {
            options.onMalformed?.(eventType);
          }
          return;
        }
        if (eventType === options.pingEvent) {
          try {
            if (typeof event.data !== "string") throw new Error("missing SSE data");
            const data: unknown = JSON.parse(event.data);
            if (options.validatePing?.(data) === false) {
              throw new Error("invalid SSE ping payload");
            }
            options.onLiveness?.(eventType, scheduler.now());
          } catch {
            options.onMalformed?.(eventType);
          }
          return;
        }

        try {
          if (typeof event.data !== "string") throw new Error("missing SSE data");
          const data: unknown = JSON.parse(event.data);
          if (options.validateEvent?.(eventType, data) === false) {
            throw new Error("invalid SSE payload");
          }
          options.onEvent?.(eventType, data);
          options.onLiveness?.(eventType, scheduler.now());
        } catch {
          options.onMalformed?.(eventType);
        }
      });
    });

    source.onerror = () => {
      if (!isCurrent(source, sourceGeneration)) return;
      currentSource = null;
      source.close();
      options.onTransportError?.();
      scheduleReconnect();
    };
  }

  const stop = () => {
    if (!active && currentSource === null && reconnectTimer === null) return;
    active = false;
    authorityBlocked = false;
    generation += 1;
    cancelReconnect();
    const source = currentSource;
    currentSource = null;
    source?.close();
  };

  const start = () => {
    if (!active) {
      active = true;
      authorityBlocked = false;
      connect();
    }
    return stop;
  };

  return { start, stop };
}
