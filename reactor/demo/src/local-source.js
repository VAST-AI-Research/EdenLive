// The frames this page sends on the model's `source_video` track.
//
// The canvas of the game running in this page is drawn onto a canvas of our own, and the
// canvas's capture track is handed exactly one frame per tick of a drift-free timer.
// So the track carries the chosen send rate whatever the game's own frame rate is,
// and changing the rate never replaces the track (no republish). With source `client`
// the server plays its output at the rate frames arrive, so this is the output rate too.

export const SEND_RATES = [24, 30, 40];
// Frames wider than this are scaled down before sending; the server covers 1280x704.
const MAX_WIDTH = 1280;

export function createLocalSource() {
  const canvas = document.createElement("canvas");
  canvas.width = 1280;
  canvas.height = 704;
  const ctx = canvas.getContext("2d");
  // captureStream(0): no frame is captured until requestFrame() asks for one.
  const track = canvas.captureStream(0).getVideoTracks()[0];
  let source = null, // the game's canvas being drawn
    timer = null,
    fps = SEND_RATES[0];
  const sent = [];

  function fit() {
    const w = source.width,
      h = source.height;
    if (!w || !h) return;
    const scale = Math.min(1, MAX_WIDTH / w);
    const W = Math.round((w * scale) / 2) * 2,
      H = Math.round((h * scale) / 2) * 2;
    if (canvas.width !== W || canvas.height !== H) {
      canvas.width = W;
      canvas.height = H;
    }
  }

  // Tick n is due at t0 + n * period; each timeout aims at the next deadline, so
  // timer lateness does not accumulate into a lower rate.
  function run() {
    clearTimeout(timer);
    const period = 1000 / fps,
      t0 = performance.now();
    let n = 0;
    const tick = () => {
      if (source) {
        fit();
        ctx.drawImage(source, 0, 0, canvas.width, canvas.height);
        track.requestFrame();
        sent.push(performance.now());
        if (sent.length > 200) sent.splice(0, sent.length - 200);
      }
      n += 1;
      timer = setTimeout(tick, Math.max(0, t0 + n * period - performance.now()));
    };
    tick();
  }

  function stop() {
    clearTimeout(timer);
    timer = null;
    source = null;
  }

  /** Start sending the frames of `gameCanvas`. */
  function use(gameCanvas) {
    stop();
    source = gameCanvas;
    run();
  }

  return {
    canvas,
    track,
    use,
    stop,
    setRate(next) {
      fps = next;
      if (source) run();
    },
    /** Frames handed to the track per second over the last 2 s. */
    sentRate() {
      const since = performance.now() - 2000;
      return sent.filter((t) => t >= since).length / 2;
    },
  };
}
