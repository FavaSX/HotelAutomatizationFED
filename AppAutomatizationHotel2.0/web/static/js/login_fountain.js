/**
 * Login fountain animation.
 * Draws water over the hotel logo (logo-hotel-blanco.svg) on a canvas that shares the
 * logo's 0..1000 viewBox: a small spray above the top cup, water spilling off both cups
 * and splashes / ripples where it lands. Static logo when the user prefers reduced motion.
 */
(function () {
  const canvas = document.getElementById('fountainCanvas');
  if (!canvas || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  const ctx = canvas.getContext('2d');

  const VIEW = 1000;
  const GRAVITY = 900; // logo units / s²

  // Measured on the traced logo (viewBox units)
  const TOP_CUP = { left: 449, right: 575, rim: 382, center: 512 };
  const MID_BASIN = { left: 386, right: 638, rim: 527 };
  const POOL = { left: 262, right: 762, surface: 704 };

  // Water spills off a lip with a little outward push and lands on the surface below
  const spills = [
    { x: TOP_CUP.left, y: TOP_CUP.rim, dir: -1, land: MID_BASIN.rim, rate: 55 },
    { x: TOP_CUP.right, y: TOP_CUP.rim, dir: 1, land: MID_BASIN.rim, rate: 55 },
    { x: MID_BASIN.left, y: MID_BASIN.rim, dir: -1, land: POOL.surface, rate: 75 },
    { x: MID_BASIN.right, y: MID_BASIN.rim, dir: 1, land: POOL.surface, rate: 75 },
  ];

  const streams = []; // falling water
  const droplets = []; // splash droplets
  const ripples = [];
  let scale = 1;

  function resize() {
    const rect = canvas.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(rect.width * dpr);
    canvas.height = Math.round(rect.height * dpr);
    scale = canvas.width / VIEW;
  }
  new ResizeObserver(resize).observe(canvas);
  resize();

  const rand = (min, max) => min + Math.random() * (max - min);

  function emit(dt) {
    spills.forEach(function (s) {
      let n = s.rate * dt;
      while (n > 0) {
        if (n >= 1 || Math.random() < n) {
          streams.push({
            x: s.x + rand(-3, 3) * s.dir,
            y: s.y + rand(-1, 2),
            vx: s.dir * rand(28, 52),
            vy: rand(-12, 18),
            r: rand(5, 8),
            land: s.land,
          });
        }
        n -= 1;
      }
    });
    // Spray rising above the top cup (where the jet in the logo meets the water)
    if (Math.random() < 45 * dt) {
      droplets.push({
        x: TOP_CUP.center + rand(-12, 12),
        y: TOP_CUP.rim - 4,
        vx: rand(-60, 60),
        vy: rand(-260, -130),
        r: rand(3, 5),
        land: TOP_CUP.rim,
        life: 1,
      });
    }
  }

  function splash(x, y) {
    const count = 1 + Math.floor(Math.random() * 3);
    for (let i = 0; i < count; i++) {
      droplets.push({
        x: x,
        y: y - 1,
        vx: rand(-90, 90),
        vy: rand(-260, -90),
        r: rand(2.6, 4.6),
        land: y,
        life: 1,
      });
    }
    if (Math.random() < 0.14) {
      ripples.push({ x: x, y: y + 3, age: 0, max: rand(34, 56) });
    }
  }

  function update(dt) {
    for (let i = streams.length - 1; i >= 0; i--) {
      const p = streams[i];
      p.vy += GRAVITY * dt;
      p.x += p.vx * dt;
      p.y += p.vy * dt;
      if (p.y >= p.land) {
        splash(p.x, p.land);
        streams.splice(i, 1);
      }
    }
    for (let i = droplets.length - 1; i >= 0; i--) {
      const d = droplets[i];
      d.vy += GRAVITY * dt;
      d.x += d.vx * dt;
      d.y += d.vy * dt;
      d.life -= dt * 0.9;
      if ((d.vy > 0 && d.y >= d.land) || d.life <= 0) droplets.splice(i, 1);
    }
    for (let i = ripples.length - 1; i >= 0; i--) {
      ripples[i].age += dt;
      if (ripples[i].age > 1) ripples.splice(i, 1);
    }
  }

  function draw() {
    ctx.setTransform(scale, 0, 0, scale, 0, 0);
    ctx.clearRect(0, 0, VIEW, VIEW);
    ctx.globalCompositeOperation = 'lighter';

    // Soft mist where the water lands
    [[MID_BASIN.rim, TOP_CUP.left], [MID_BASIN.rim, TOP_CUP.right],
     [POOL.surface, MID_BASIN.left], [POOL.surface, MID_BASIN.right]].forEach(function (m) {
      const x = m[1] + (m[1] < 500 ? -38 : 38);
      const g = ctx.createRadialGradient(x, m[0], 0, x, m[0], 60);
      g.addColorStop(0, 'rgba(150, 205, 255, 0.22)');
      g.addColorStop(1, 'rgba(150, 205, 255, 0)');
      ctx.fillStyle = g;
      ctx.fillRect(x - 60, m[0] - 60, 120, 120);
    });

    // Falling water: short streaks along the velocity give a continuous ribbon
    ctx.lineCap = 'round';
    streams.forEach(function (p) {
      const len = 0.022;
      ctx.strokeStyle = 'rgba(175, 220, 255, 0.5)';
      ctx.lineWidth = p.r;
      ctx.beginPath();
      ctx.moveTo(p.x - p.vx * len, p.y - p.vy * len);
      ctx.lineTo(p.x, p.y);
      ctx.stroke();
    });

    droplets.forEach(function (d) {
      ctx.fillStyle = 'rgba(205, 235, 255, ' + (0.85 * Math.max(d.life, 0)).toFixed(3) + ')';
      ctx.beginPath();
      ctx.arc(d.x, d.y, d.r, 0, Math.PI * 2);
      ctx.fill();
    });

    ctx.lineWidth = 3;
    ripples.forEach(function (r) {
      const t = r.age;
      ctx.strokeStyle = 'rgba(180, 220, 255, ' + (0.45 * (1 - t)).toFixed(3) + ')';
      ctx.beginPath();
      ctx.ellipse(r.x, r.y, r.max * t + 2, (r.max * t + 2) * 0.22, 0, 0, Math.PI * 2);
      ctx.stroke();
    });
    ctx.globalCompositeOperation = 'source-over';
  }

  let last = performance.now();
  function frame(now) {
    // Clamp the step so returning to a hidden tab doesn't dump a burst of water
    const dt = Math.min((now - last) / 1000, 0.05);
    last = now;
    emit(dt);
    update(dt);
    draw();
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
})();
