/** Local active review time. Visibility, 30 s inactivity and system waits do not count. */
export class ReviewClock {
  activeMs = 0;
  private last = performance.now();
  private activity = this.last;
  ready = false;
  waiting = false;
  tick(now = performance.now()) {
    if (this.ready && !this.waiting && document.visibilityState === "visible")
      this.activeMs += Math.max(0, Math.min(now, this.activity + 30000) - this.last);
    this.last = now;
  }
  interact = () => { this.tick(); this.activity = performance.now(); };
  visibility = () => { this.last = performance.now(); this.activity = this.last; };
  setReady(value: boolean) { this.tick(); this.ready = value; }
  setWaiting(value: boolean) { this.tick(); this.waiting = value; }
  reset() { this.activeMs = 0; this.last = this.activity = performance.now(); }
}
