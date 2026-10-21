// Modified by Reactor Technologies, Inc.; the changes are listed in
// eden-live/PORTING.md ("The vendored game capture").
// Shared adapter for the seven fixed-step games imported from eden-game-demo-local.
export function createDemoRuntime(host, {Game, FPS, config, createScene, dynamicPrompt}, initial) {
  const scene = createScene();
  let game, elapsed = 0, steps = 0, eventId = 0, pending = [], paused = false;
  const bindings = {
    KeyW: 'forward', KeyS: 'back', KeyA: 'left', KeyD: 'right',
    ArrowLeft: 'lookLeft', ArrowRight: 'lookRight', ArrowUp: 'lookUp', ArrowDown: 'lookDown',
    KeyF: 'fire', Space: 'jump', KeyR: 'unused', KeyP: 'pause', Escape: 'pause',
  };
  for (const action of config.extraActions || []) {
    for (const code of action.codes) bindings[code] = action.action;
  }
  host.input.bind(bindings);

  function reset() {
    game = new Game(initial);
    elapsed = 0;
    steps = 0;
    pending = [];
    paused = false;
    host.input.clear();
  }
  // The held look rate (mouse look) adds to the arrow keys' turn. A rate of 1
  // is LOOK_MAX radians per game step, the largest per-step turn the games
  // accept; each game clamps again to its own limit.
  const LOOK_MAX = 0.2;
  const aim = (keys, rate) => (rate ? Math.max(-LOOK_MAX, Math.min(LOOK_MAX, keys + rate * LOOK_MAX)) : keys);
  function controls() {
    const held = action => Number(host.input.isDown(action));
    const turn = Math.PI / 3 / FPS;
    const [yaw, pitch] = host.input.look ? host.input.look() : [0, 0];
    const input = {
      forward: held('forward') - held('back'), side: held('right') - held('left'),
      lookX: aim((held('lookRight') - held('lookLeft')) * turn, yaw),
      lookY: aim((held('lookUp') - held('lookDown')) * turn, pitch),
      fire: Boolean(held('fire')), jump: host.input.consumePress('jump'),
    };
    for (const action of config.extraActions || []) {
      input[action.action] = action.held ? Boolean(held(action.action)) : host.input.consumePress(action.action);
    }
    return input;
  }
  function update(dt) {
    if (host.input.consumePress('pause')) paused = !paused;
    if (paused || game.s.won || game.s.dead) return;
    elapsed += dt;
    // The host ticks at 60 Hz; gameplay and cooldowns stay at their native 24 Hz.
    const target = Math.floor(elapsed * FPS + 1e-8);
    while (steps < target && !game.s.won && !game.s.dead) {
      game.step(controls());
      pending.push(game.snapshot());
      steps++;
    }
  }
  function read() {
    const s = game.s;
    const events = pending.length ? [{id: ++eventId, type: 'game.action', prompt: dynamicPrompt(pending)}] : [];
    pending = [];
    return {
      slug: config.id, title: config.title, objective: config.objective, instruction: config.instruction,
      state: s.won ? 'WON' : s.dead ? 'LOST' : 'RUNNING', paused,
      frame: s.frame, time: s.time, events, crosshair: config.crosshair !== false,
      health: config.healthMax ? s.health : null, healthMax: config.healthMax || null,
      resource: config.resource ? {label: config.resource.label, value: s[config.resource.key], max: config.resource.max} : null,
      stats: config.statLabels.map((label, index) => ({label, value: config.stats(s)[index]})),
      status: config.liveStatus ? config.liveStatus(s) : '',
      outcome: s.won ? config.won : s.dead ? config.dead : null,
    };
  }
  return {
    canvas: scene.renderer.domElement, reset, update, read,
    render() { scene.render(game.s); },
    // Keep the reference camera's composition at either model resolution.
    layout(width, height) { scene.renderer.setSize(width, height, false); },
    handleUiAction(action) {
      if (action === 'p' || action === 'escape' || action === 'pause') {
        paused = !paused;
        return true;
      }
      if (action === 'restart') { reset(); return true; }
      return false;
    },
    destroy() { scene.renderer.dispose(); },
  };
}
