/** Poll current held keys. At most one command is in flight; no edge queue. */
export function createControls(send, onError, period = 33) {
  let latest = [], sent = null, timer = null, pending = null;
  let enabled = false, epoch = 0;
  function poll() {
    if (!enabled || pending) return;
    const signature = JSON.stringify(latest);
    if (signature === sent) return;
    const generation = epoch;
    const snapshot = [...latest];
    sent = signature;
    pending = Promise.resolve().then(() =>
      generation === epoch ? send(snapshot) : undefined,
    ).catch((error) => {
      if (generation === epoch) {
        enabled = false;
        clearInterval(timer);
        onError(error);
      }
    }).finally(() => { pending = null; });
  }
  return {
    set(keys) { latest = [...new Set(keys)].sort(); },
    start() {
      if (enabled) return;
      enabled = true;
      sent = null;
      timer = setInterval(poll, period);
    },
    async stop() {
      enabled = false;
      epoch += 1;
      clearInterval(timer);
      latest = [];
      await pending;
    },
  };
}
