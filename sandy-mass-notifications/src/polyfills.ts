// MessageChannel / MessagePort polyfill for the Cloudflare Workers runtime.
//
// React 19's server renderer (react-dom/server) references MessageChannel at
// module-evaluation time. workerd does not expose that global, so without this
// the worker throws "ReferenceError: MessageChannel is not defined" on startup.
//
// This module has no imports, so bundlers evaluate it before react-dom/server —
// keep it imported first in index.tsx. renderToString is synchronous, so the
// simple setTimeout-based delivery below is only ever a fallback; its real job is
// to make the global exist.

class PolyfillMessagePort {
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  private other: PolyfillMessagePort | null = null;

  _link(other: PolyfillMessagePort) {
    this.other = other;
  }
  postMessage(data: unknown) {
    const target = this.other;
    if (!target) return;
    // Deliver asynchronously, like a real MessagePort.
    setTimeout(() => target.onmessage?.({ data }), 0);
  }
  addEventListener(type: string, listener: (ev: { data: unknown }) => void) {
    if (type === "message") this.onmessage = listener;
  }
  removeEventListener(type: string) {
    if (type === "message") this.onmessage = null;
  }
  start() {}
  close() {}
}

class PolyfillMessageChannel {
  port1 = new PolyfillMessagePort();
  port2 = new PolyfillMessagePort();
  constructor() {
    this.port1._link(this.port2);
    this.port2._link(this.port1);
  }
}

const g = globalThis as unknown as {
  MessageChannel?: unknown;
  MessagePort?: unknown;
};
if (typeof g.MessageChannel === "undefined") {
  g.MessageChannel = PolyfillMessageChannel;
  g.MessagePort = PolyfillMessagePort;
}
