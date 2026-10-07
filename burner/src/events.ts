import { EventEmitter } from "node:events";
import type { BurnerEvent } from "./types.js";

// Typed in-process event bus. Scheduler/runners emit; store persists; server streams over SSE.
export class Bus {
  private ee = new EventEmitter();
  constructor() {
    this.ee.setMaxListeners(200);
  }
  emit(e: BurnerEvent) {
    this.ee.emit("event", e);
  }
  on(fn: (e: BurnerEvent) => void): () => void {
    this.ee.on("event", fn);
    return () => this.ee.off("event", fn);
  }
}

export const createBus = () => new Bus();

// Process-wide default bus. Tests should use createBus() instead.
export const bus = createBus();
