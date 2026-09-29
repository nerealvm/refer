'use strict';
// Приглашение на день рождения Миши в КидБург.
// Сборка: node build.js → out/*.png (A5, 300 dpi) и out/*.pdf (A4, 2 открытки на листе).
const fs = require('fs');
const os = require('os');
const path = require('path');
const zlib = require('zlib');
const { chromium } = require('playwright-core');

const ROOT = __dirname;
const OUT = path.join(ROOT, 'out');
const LOCAL_CHROME = '/opt/pw-browsers/chromium-1194/chrome-linux/chrome';
const CHROME = process.env.CHROME || (fs.existsSync(LOCAL_CHROME) ? LOCAL_CHROME : undefined);
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'invite-'));
const NAME = 'priglashenie-misha';

// Единицы — 0,1 мм. Открытка A5: 148 × 210 мм.
const W = 1480, H = 2100, M = 50, CX = W / 2;
const FR = { x: M, y: M, w: W - 2 * M, h: H - 2 * M, rx: 64 };
const G = 1985; // линия земли в городе
const AGE = '8'; // сколько исполняется Мише

const PAL = {
  color: {
    ink: '#1E1B2E', paper: '#FFFFFF', shadow: '#1E1B2E',
    red: '#F04E23', orange: '#F7931E', yellow: '#FFC928', lime: '#9BCB3C', green: '#2DB37A',
    teal: '#17B3B0', blue: '#2F80ED', purple: '#8E44AD', magenta: '#D6246E', pink: '#FF8FB8',
    glass: '#CDEBFF', light: '#EEF8FF', sky: '#D9F0FF', grass: '#6CC36A', plate: '#FFF7D9',
    dots: '#9AA3BF',
    rainbow: ['#F04E23', '#F7931E', '#FFC928', '#9BCB3C', '#2DB37A', '#17B3B0', '#2F80ED', '#8E44AD', '#D6246E'],
    confetti: ['#F04E23', '#F7931E', '#FFC928', '#9BCB3C', '#17B3B0', '#2F80ED', '#8E44AD', '#FF6FA3'],
  },
  bw: {
    ink: '#000000', paper: '#FFFFFF', shadow: '#000000',
    red: '#BEBEBE', orange: '#DADADA', yellow: '#F0F0F0', lime: '#DADADA', green: '#BEBEBE',
    teal: '#DADADA', blue: '#BEBEBE', purple: '#A8A8A8', magenta: '#BEBEBE', pink: '#E8E8E8',
    glass: '#FFFFFF', light: '#FFFFFF', sky: '#FFFFFF', grass: '#DADADA', plate: '#FFFFFF',
    dots: '#8C8C8C',
    rainbow: null,
    confetti: ['#8C8C8C', '#B4B4B4', '#6E6E6E'],
  },
};

// ---------- тексты, которые нужно измерить ----------
const TXT = {
  pill: { t: 'ПРИГЛАШЕНИЕ', f: 'Rubik', w: 700, s: 40 },
  hero1: { t: 'ДЕНЬ', f: 'Rubik', w: 900, s: 100 },
  hero2: { t: 'РОЖДЕНИЯ', f: 'Rubik', w: 900, s: 100 },
  hi: { t: 'Привет,', f: 'Rubik', w: 800, s: 70 },
  excl: { t: '!', f: 'Rubik', w: 800, s: 70 },
  wait: { t: 'Жду тебя!', f: 'Rubik', w: 800, s: 54 },
  sign: { t: 'Миша', f: 'Pacifico', w: 400, s: 128 },
};

// ---------- утилиты ----------
const r1 = (n) => Math.round(n * 10) / 10;
function A(o) {
  return Object.entries(o)
    .filter(([, v]) => v !== undefined && v !== null && v !== false)
    .map(([k, v]) => `${k}="${typeof v === 'number' ? r1(v) : v}"`)
    .join(' ');
}
const el = (tag, o, inner) => (inner === undefined ? `<${tag} ${A(o)}/>` : `<${tag} ${A(o)}>${inner}</${tag}>`);
const g = (o, inner) => el('g', o, Array.isArray(inner) ? inner.join('') : inner);
function rng(seed) {
  let s = seed >>> 0;
  return () => ((s = (Math.imul(s, 1664525) + 1013904223) >>> 0) / 4294967296);
}
const sparkle = (r) =>
  `M0 ${-r} Q${r * 0.16} ${-r * 0.16} ${r} 0 Q${r * 0.16} ${r * 0.16} 0 ${r} Q${-r * 0.16} ${r * 0.16} ${-r} 0 Q${-r * 0.16} ${-r * 0.16} 0 ${-r}Z`;

// ---------- текст в кривых ----------
// Весь текст переводится в контуры через HarfBuzz (тот же движок, что в Chrome),
// поэтому в PDF не остаётся шрифтов и он одинаково выглядит в любом просмотрщике.
const FONT_FILES = { Rubik: 'fonts/Rubik-wght.ttf', Pacifico: 'fonts/Pacifico-Regular.ttf' };
let hb = null; // harfbuzzjs (ES-модуль), загружается в main()
const faces = {};
const hbFonts = {};
const glyphs = new Map();
function hbFont(family, weight) {
  const key = `${family}:${weight}`;
  if (!hbFonts[key]) {
    if (!faces[family]) faces[family] = new hb.Face(new hb.Blob(fs.readFileSync(path.join(ROOT, FONT_FILES[family]))));
    const font = new hb.Font(faces[family]);
    if (faces[family].getAxisInfos().wght) font.setVariations([new hb.Variation('wght', weight)]);
    hbFonts[key] = font;
  }
  return hbFonts[key];
}
function shapeRun(family, weight, str) {
  const buf = new hb.Buffer();
  buf.addText(str);
  buf.guessSegmentProperties();
  hb.shape(hbFont(family, weight), buf);
  return buf.getGlyphInfosAndPositions();
}
// ширина строки; ls — межбуквенный интервал (как letter-spacing), после последней буквы не считается
function runWidth(family, weight, size, str, ls = 0) {
  const run = shapeRun(family, weight, str);
  return (run.reduce((sum, gl) => sum + gl.xAdvance, 0) * size) / faces[family].upem + ls * (run.length - 1);
}
// контур строки для <path>: начало базовой линии в (x, y)
function outline(family, weight, size, str, x, y, ls = 0) {
  const font = hbFont(family, weight);
  const k = size / faces[family].upem;
  const d = [];
  let pen = x;
  for (const gl of shapeRun(family, weight, str)) {
    const key = `${family}:${weight}:${gl.codepoint}`;
    if (!glyphs.has(key)) glyphs.set(key, font.glyphToJson(gl.codepoint));
    const ox = pen + gl.xOffset * k, oy = y - gl.yOffset * k;
    for (const c of glyphs.get(key)) {
      const v = [];
      for (let i = 0; i < c.values.length; i += 2) v.push(r1(ox + c.values[i] * k), r1(oy - c.values[i + 1] * k));
      d.push(c.type + v.join(' '));
    }
    pen += gl.xAdvance * k + ls;
  }
  return { d: d.join(''), width: pen - x };
}

// ---------- сборка SVG ----------
function buildSVG(mode, m, idp = '') {
  const P = PAL[mode];
  const BW = mode === 'bw';
  const id = (s) => idp + s;
  const url = (s) => `url(#${idp}${s})`;
  const defs = [];
  const out = [];
  const SW = 6; // основная обводка
  const AV = []; // прямоугольники [x1, y1, x2, y2], свободные от конфетти
  // Текст сразу в кривых. s — строка или список сегментов [строка, цвет].
  const text = (o, s) => {
    const { x = 0, y = 0, 'font-family': fam, 'font-weight': wt = 400, 'font-size': size, 'letter-spacing': ls = 0, 'text-anchor': anchor, ...style } = o;
    const segs = Array.isArray(s) ? s : [[s, style.fill]];
    const total = segs.reduce((sum, [str]) => sum + runWidth(fam, wt, size, str, ls) + ls, 0) - ls;
    let pen = anchor === 'middle' ? x - total / 2 : x;
    const runs = segs.map(([str, fill]) => {
      const o2 = outline(fam, wt, size, str, pen, y, ls);
      pen += o2.width;
      return { d: o2.d, fill };
    });
    if (runs.length === 1) return el('path', { ...style, d: runs[0].d });
    return g(style, runs.map((r) => el('path', r)));
  };

  // --- градиенты ---
  if (!BW) {
    const stops = (cols) => cols.map((c, i) => el('stop', { offset: r1(i / (cols.length - 1)), 'stop-color': c })).join('');
    defs.push(el('linearGradient', { id: id('rainbow'), gradientUnits: 'userSpaceOnUse', x1: 170, y1: 0, x2: 1310, y2: 0 }, stops(P.rainbow)));
    defs.push(el('linearGradient', { id: id('frameGrad'), gradientUnits: 'userSpaceOnUse', x1: 0, y1: 0, x2: W, y2: H }, stops(P.rainbow)));
    defs.push(el('linearGradient', { id: id('pillGrad'), x1: 0, y1: 0, x2: 1, y2: 1 }, stops(['#9333EA', '#3B82F6'])));
    defs.push(el('linearGradient', { id: id('skyGrad'), x1: 0, y1: 0, x2: 0, y2: 1 }, stops(['#FFFFFF', P.sky])));
    defs.push(el('linearGradient', { id: id('signGrad'), x1: 0, y1: 0, x2: 1, y2: 0 }, stops(['#D6246E', '#8E44AD'])));
    defs.push(el('linearGradient', { id: id('ageGrad'), x1: 0, y1: 0, x2: 1, y2: 0 }, stops(['#2F80ED', '#8E44AD', '#D6246E'])));
  }
  defs.push(el('clipPath', { id: id('frameClip') }, el('rect', { x: FR.x, y: FR.y, width: FR.w, height: FR.h, rx: FR.rx })));

  // --- фон ---
  out.push(el('rect', { width: W, height: H, fill: P.paper }));

  // --- небо и город (внизу, под рамкой) ---
  const city = [];
  if (!BW) city.push(el('rect', { x: FR.x, y: 1330, width: FR.w, height: 740, fill: url('skyGrad') }));
  city.push(clouds());
  city.push(balloons());
  city.push(cityBlock());
  out.push(g({ 'clip-path': url('frameClip') }, city));

  // --- рамка ---
  if (BW) {
    out.push(el('rect', { x: FR.x, y: FR.y, width: FR.w, height: FR.h, rx: FR.rx, fill: 'none', stroke: P.ink, 'stroke-width': 8 }));
    out.push(el('rect', { x: FR.x + 18, y: FR.y + 18, width: FR.w - 36, height: FR.h - 36, rx: FR.rx - 16, fill: 'none', stroke: P.ink, 'stroke-width': 3, 'stroke-dasharray': '2 13', 'stroke-linecap': 'round' }));
  } else {
    out.push(el('rect', { x: FR.x, y: FR.y, width: FR.w, height: FR.h, rx: FR.rx, fill: 'none', stroke: url('frameGrad'), 'stroke-width': 14 }));
  }

  // --- плашка «ПРИГЛАШЕНИЕ» ---
  {
    const t = m.pill, ls = 9, n = TXT.pill.t.length;
    const tw = t.w + ls * (n - 1);
    const pw = tw + 2 * 66, ph = 70, px = CX - pw / 2, py = 94;
    AV.push([px - 8, py - 8, px + pw + 14, py + ph + 16]);
    out.push(el('rect', { x: px + 6, y: py + 8, width: pw, height: ph, rx: ph / 2, fill: P.shadow }));
    out.push(el('rect', { x: px, y: py, width: pw, height: ph, rx: ph / 2, fill: BW ? P.ink : url('pillGrad'), stroke: P.ink, 'stroke-width': SW }));
    out.push(text({ x: CX - tw / 2, y: py + ph / 2 + 14.5, 'font-family': 'Rubik', 'font-weight': 700, 'font-size': 40, 'letter-spacing': ls, fill: '#FFFFFF' }, TXT.pill.t));
    // звёздочки по краям плашки
    out.push(el('path', { d: sparkle(15), transform: `translate(${px + 34} ${py + ph / 2})`, fill: BW ? '#FFFFFF' : P.yellow }));
    out.push(el('path', { d: sparkle(15), transform: `translate(${px + pw - 34} ${py + ph / 2})`, fill: BW ? '#FFFFFF' : P.yellow }));
  }

  // --- заголовок «ДЕНЬ РОЖДЕНИЯ»: обводка + «жидкая» радужная заливка ---
  const capRatio = m.capH.ascent / 100; // высота прописной на кегль 100
  const heroTarget = 1150; // ширина строки «РОЖДЕНИЯ»
  const lsK = 0.045;
  const S = heroTarget / (m.hero2.w / 100 + lsK * (TXT.hero2.t.length - 1));
  const hls = S * lsK;
  const cap = S * capRatio;
  const OUTL = 10; // толщина внешнего контура
  const heroLines = [
    { key: 'hero1', y: 198 + cap },
    { key: 'hero2', y: 198 + cap + 56 + cap },
  ];
  heroLines.forEach((L, i) => {
    const t = TXT[L.key].t;
    const w = (m[L.key].w * S) / 100 + hls * (t.length - 1);
    const x = CX - w / 2;
    const ta = { x, y: L.y, 'font-family': 'Rubik', 'font-weight': 900, 'font-size': r1(S), 'letter-spacing': r1(hls) };
    defs.push(el('clipPath', { id: id('hero' + i) }, text(ta, t)));
    AV.push([x - 16, L.y - cap - 16, x + w + 24, L.y + (i === 1 ? S * 0.14 : 0) + 24]);
    // тень и контур
    out.push(text({ ...ta, fill: P.shadow, stroke: P.shadow, 'stroke-width': OUTL * 2, 'stroke-linejoin': 'round', transform: 'translate(8 10)' }, t));
    out.push(text({ ...ta, fill: P.ink, stroke: P.ink, 'stroke-width': OUTL * 2, 'stroke-linejoin': 'round' }, t));
    // волна
    const top = L.y - cap;
    const wy = top + cap * (i === 0 ? 0.5 : 0.47);
    const amp = cap * 0.11;
    const x0 = x - 40, x1 = x + w + 40;
    const rnd = rng(7 + i * 13);
    const seg = S * 0.62;
    let d = `M${r1(x0)} ${r1(wy)}`;
    let cx = x0, k = i;
    while (cx < x1) {
      const a = amp * (0.55 + 0.45 * rnd()) * (k % 2 ? 1 : -1);
      d += ` C${r1(cx + seg * 0.36)} ${r1(wy + a)} ${r1(cx + seg * 0.64)} ${r1(wy + a)} ${r1(cx + seg)} ${r1(wy)}`;
      cx += seg; k++;
    }
    const waveTop = d;
    d += ` L${r1(cx)} ${r1(L.y + 80)} L${r1(x0)} ${r1(L.y + 80)} Z`;
    const fillLayer = [
      el('rect', { x: x0, y: top - 60, width: x1 - x0 + seg, height: cap + 160, fill: P.paper }),
      el('path', { d, fill: BW ? '#C8C8C8' : url('rainbow') }),
    ];
    if (BW) fillLayer.push(el('path', { d: waveTop, fill: 'none', stroke: P.ink, 'stroke-width': 4 }));
    out.push(g({ 'clip-path': url('hero' + i) }, fillLayer));
  });
  const heroBottom = heroLines[1].y;

  // колпак слева и наклейка «8 лет» справа от «ДЕНЬ»
  {
    const w1 = (m.hero1.w * S) / 100 + hls * 3;
    const yMid = heroLines[0].y - cap / 2;
    const hx = CX - w1 / 2 - 150, sx = CX + w1 / 2 + 160, sy = yMid - 14;
    out.push(partyHat(hx, yMid + 64, -16));
    out.push(ageSticker(sx, sy, 10));
    AV.push([hx - 100, yMid - 150, hx + 90, yMid + 96]);
    AV.push([sx - 108, sy - 108, sx + 116, sy + 116]);
  }

  // --- конфетти (заполняется в конце, когда известны все зоны) ---
  const confettiAt = out.length;
  out.push('');

  // --- «Привет, ____!» ---
  const nameY = heroBottom + 54;
  {
    const PH = 128, PW = 800, gap = 22;
    const total = m.hi.w + gap + PW + 26 + m.excl.w;
    const x = CX - total / 2;
    const px = x + m.hi.w + gap;
    const base = nameY + PH * 0.68;
    AV.push([x - 10, nameY - 12, x + total + 12, nameY + PH + 20]);
    out.push(text({ x, y: base, 'font-family': 'Rubik', 'font-weight': 800, 'font-size': 70, fill: P.ink }, TXT.hi.t));
    out.push(el('rect', { x: px + 8, y: nameY + 10, width: PW, height: PH, rx: 28, fill: P.shadow }));
    out.push(el('rect', { x: px, y: nameY, width: PW, height: PH, rx: 28, fill: P.plate, stroke: P.ink, 'stroke-width': SW }));
    out.push(el('line', { x1: px + 34, y1: nameY + PH * 0.76, x2: px + PW - 34, y2: nameY + PH * 0.76, stroke: P.dots, 'stroke-width': 4, 'stroke-dasharray': '0.1 14', 'stroke-linecap': 'round' }));
    out.push(text({ x: px + PW + 26, y: base, 'font-family': 'Rubik', 'font-weight': 800, 'font-size': 70, fill: P.ink }, TXT.excl.t));
  }

  // --- основной текст ---
  const bodyY = nameY + 128 + 82;
  out.push(text({ x: CX, y: bodyY, 'text-anchor': 'middle', 'font-family': 'Rubik', 'font-weight': 700, 'font-size': 46, fill: P.ink },
    [['Приходи ко мне на праздник в ', P.ink], ['КидБург', BW ? P.ink : P.magenta], ['!', P.ink]]));
  out.push(text({ x: CX, y: bodyY + 55, 'text-anchor': 'middle', 'font-family': 'Rubik', 'font-weight': 400, 'font-size': 40, fill: P.ink },
    'Будем играть, пробовать себя в разных профессиях'));
  out.push(text({ x: CX, y: bodyY + 103, 'text-anchor': 'middle', 'font-family': 'Rubik', 'font-weight': 400, 'font-size': 40, fill: P.ink },
    'и веселиться!'));

  AV.push([150, bodyY - 50, 1330, bodyY + 118]);

  // --- плитки с деталями ---
  const TY = bodyY + 140, TH = 208, TG = 30, TW = (1240 - TG) / 2, TX = 120;
  out.push(tile(TX, TY, TW, TH, P.orange, 'КОГДА', [['10 октября', 800, 56], ['суббота', 500, 38]], iconCalendar));
  out.push(tile(TX + TW + TG, TY, TW, TH, P.teal, 'ВО СКОЛЬКО', [['13:00', 800, 66], ['начало праздника', 500, 36]], iconClock));
  out.push(tile(TX, TY + TH + TG, TW, TH, P.blue, 'ГДЕ', [['КидБург', 800, 52], ['ТРК «Питер Радуга»', 500, 33], ['пр. Космонавтов, 14', 500, 33]], iconPin));
  out.push(tile(TX + TW + TG, TY + TH + TG, TW, TH, P.magenta, 'С СОБОЙ', [['хорошее', 800, 50], ['настроение!', 800, 50]], iconSmile));
  const tilesBottom = TY + 2 * TH + TG;
  AV.push([TX - 10, TY - 10, TX + 1240 + 18, tilesBottom + 20]);

  // --- подпись ---
  {
    const sy = tilesBottom + 98;
    const gap = 26;
    const total = m.wait.w + gap + m.sign.w;
    const x = CX - total / 2 - 30;
    out.push(text({ x, y: sy, 'font-family': 'Rubik', 'font-weight': 800, 'font-size': 54, fill: P.ink }, TXT.wait.t));
    out.push(text({ x: x + m.wait.w + gap, y: sy + 26, 'font-family': 'Pacifico', 'font-size': 128, fill: BW ? P.ink : url('signGrad'), transform: `rotate(-5 ${x + m.wait.w + gap + m.sign.w / 2} ${sy})` }, TXT.sign.t));
  }

  out[confettiAt] = confetti(tilesBottom + 20);

  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${H}" width="100%" height="100%">` +
    `<defs>${defs.join('')}</defs>${out.join('')}</svg>`;

  // ================= элементы =================
  function tile(x, y, w, h, color, label, lines, icon) {
    const parts = [];
    parts.push(el('rect', { x: x + 8, y: y + 10, width: w, height: h, rx: 30, fill: P.shadow }));
    parts.push(el('rect', { x, y, width: w, height: h, rx: 30, fill: P.paper, stroke: P.ink, 'stroke-width': SW }));
    const icx = x + 82, icy = y + h / 2;
    parts.push(el('circle', { cx: icx, cy: icy, r: 56, fill: color, stroke: P.ink, 'stroke-width': SW }));
    parts.push(g({ transform: `translate(${icx} ${icy})` }, icon()));
    const tx = x + 162;
    const n = lines.length;
    let ty = y + (n === 3 ? 48 : 56);
    parts.push(text({ x: tx, y: ty, 'font-family': 'Rubik', 'font-weight': 700, 'font-size': 27, 'letter-spacing': 4, fill: BW ? P.ink : color }, label));
    lines.forEach(([t, wt, s], i) => {
      ty += i === 0 ? s * 1.08 : s * (n === 3 ? 1.16 : 1.2);
      parts.push(text({ x: tx, y: ty, 'font-family': 'Rubik', 'font-weight': wt, 'font-size': s, fill: P.ink }, t));
    });
    return g({}, parts);
  }

  function iconCalendar() {
    return [
      el('rect', { x: -31, y: -27, width: 62, height: 60, rx: 9, fill: P.paper, stroke: P.ink, 'stroke-width': 5 }),
      el('path', { d: 'M-31 -9 V-18 a9 9 0 0 1 9 -9 H22 a9 9 0 0 1 9 9 V-9 Z', fill: BW ? P.ink : P.red, stroke: P.ink, 'stroke-width': 5, 'stroke-linejoin': 'round' }),
      el('rect', { x: -19, y: -37, width: 9, height: 18, rx: 4.5, fill: P.paper, stroke: P.ink, 'stroke-width': 4 }),
      el('rect', { x: 10, y: -37, width: 9, height: 18, rx: 4.5, fill: P.paper, stroke: P.ink, 'stroke-width': 4 }),
      text({ x: 0, y: 25, 'text-anchor': 'middle', 'font-family': 'Rubik', 'font-weight': 800, 'font-size': 30, fill: P.ink }, '10'),
    ].join('');
  }
  function iconClock() {
    const ticks = [0, 90, 180, 270].map((a) => el('line', { x1: 0, y1: -26, x2: 0, y2: -20, stroke: P.ink, 'stroke-width': 4, 'stroke-linecap': 'round', transform: `rotate(${a})` })).join('');
    return [
      el('circle', { r: 33, fill: P.paper, stroke: P.ink, 'stroke-width': 5 }),
      ticks,
      el('line', { x1: 0, y1: 0, x2: 0, y2: -21, stroke: P.ink, 'stroke-width': 5, 'stroke-linecap': 'round' }),
      el('line', { x1: 0, y1: 0, x2: 8, y2: -14, stroke: P.ink, 'stroke-width': 6, 'stroke-linecap': 'round' }),
      el('circle', { r: 4.5, fill: P.ink }),
    ].join('');
  }
  function iconPin() {
    return [
      el('ellipse', { cx: 0, cy: 33, rx: 20, ry: 6, fill: BW ? '#BEBEBE' : '#1E1B2E33' }),
      el('path', { d: 'M0 33 C-9 19 -29 3 -29 -12 A29 29 0 1 1 29 -12 C29 3 9 19 0 33 Z', fill: BW ? P.paper : P.red, stroke: P.ink, 'stroke-width': 5, 'stroke-linejoin': 'round' }),
      el('circle', { cx: 0, cy: -12, r: 11, fill: P.paper, stroke: P.ink, 'stroke-width': 4 }),
    ].join('');
  }
  function iconSmile() {
    return [
      el('circle', { r: 33, fill: BW ? P.paper : P.yellow, stroke: P.ink, 'stroke-width': 5 }),
      el('ellipse', { cx: -11, cy: -8, rx: 4.5, ry: 6.5, fill: P.ink }),
      el('ellipse', { cx: 11, cy: -8, rx: 4.5, ry: 6.5, fill: P.ink }),
      BW ? '' : el('circle', { cx: -20, cy: 7, r: 5.5, fill: '#FF8FB8' }),
      BW ? '' : el('circle', { cx: 20, cy: 7, r: 5.5, fill: '#FF8FB8' }),
      el('path', { d: 'M-16 5 Q0 24 16 5', fill: 'none', stroke: P.ink, 'stroke-width': 5, 'stroke-linecap': 'round' }),
    ].join('');
  }

  function partyHat(x, y, rot) {
    const cone = 'M-62 0 L0 -168 L62 0 Z';
    const cid = id('hatClip');
    defs.push(el('clipPath', { id: cid }, el('path', { d: cone })));
    const stripes = [];
    for (let k = -4; k <= 4; k++) {
      stripes.push(el('path', { d: `M${-120 + k * 44} 0 L${-60 + k * 44} -180 L${-40 + k * 44} -180 L${-100 + k * 44} 0 Z`, fill: BW ? '#D0D0D0' : (k % 2 ? P.yellow : P.pink) }));
    }
    return g({ transform: `translate(${x} ${y}) rotate(${rot})` }, [
      el('path', { d: cone, fill: P.shadow, transform: 'translate(7 9)', stroke: P.shadow, 'stroke-width': SW, 'stroke-linejoin': 'round' }),
      el('path', { d: cone, fill: BW ? P.paper : P.blue }),
      g({ 'clip-path': `url(#${cid})` }, stripes),
      el('path', { d: cone, fill: 'none', stroke: P.ink, 'stroke-width': SW, 'stroke-linejoin': 'round' }),
      el('path', { d: 'M-70 2 Q-35 -14 0 2 Q35 18 70 2 L70 -12 Q35 4 0 -12 Q-35 -28 -70 -12 Z', fill: BW ? P.paper : P.orange, stroke: P.ink, 'stroke-width': 5, 'stroke-linejoin': 'round' }),
      el('circle', { cx: 0, cy: -174, r: 22, fill: BW ? P.paper : P.magenta, stroke: P.ink, 'stroke-width': SW }),
    ]);
  }

  // наклейка-звёздочка с возрастом: цифра в стиле заголовка + «ЛЕТ»
  function ageSticker(cx, cy, rot) {
    const R = 98, r = 86, n = 18;
    let star = '';
    for (let i = 0; i < 2 * n; i++) {
      const a = (Math.PI * i) / n - Math.PI / 2;
      const rr = i % 2 ? r : R;
      star += `${i ? 'L' : 'M'}${r1(Math.cos(a) * rr)} ${r1(Math.sin(a) * rr)}`;
    }
    star += 'Z';
    const S8 = 142, y8 = 30;
    const w8 = runWidth('Rubik', 900, S8, AGE);
    const h8 = S8 * capRatio;
    const t8 = { x: -w8 / 2, y: y8, 'font-family': 'Rubik', 'font-weight': 900, 'font-size': S8 };
    defs.push(el('clipPath', { id: id('age') }, text(t8, AGE)));
    const top = y8 - h8, wy = top + h8 * 0.52, amp = h8 * 0.1;
    const wave = `M${r1(-w8)} ${r1(wy)} C${r1(-w8 * 0.55)} ${r1(wy - amp)} ${r1(-w8 * 0.2)} ${r1(wy - amp)} 0 ${r1(wy)} C${r1(w8 * 0.2)} ${r1(wy + amp)} ${r1(w8 * 0.55)} ${r1(wy + amp)} ${r1(w8)} ${r1(wy)}`;
    const fill = [
      el('rect', { x: -w8, y: top - 30, width: 2 * w8, height: h8 + 70, fill: P.paper }),
      el('path', { d: `${wave} L${r1(w8)} ${y8 + 40} L${r1(-w8)} ${y8 + 40} Z`, fill: BW ? '#C8C8C8' : url('ageGrad') }),
    ];
    if (BW) fill.push(el('path', { d: wave, fill: 'none', stroke: P.ink, 'stroke-width': 4 }));
    return g({ transform: `translate(${r1(cx)} ${r1(cy)}) rotate(${rot})` }, [
      el('path', { d: star, fill: P.shadow, stroke: P.shadow, 'stroke-width': SW, 'stroke-linejoin': 'round', transform: 'translate(7 9)' }),
      el('path', { d: star, fill: BW ? '#E6E6E6' : P.yellow, stroke: P.ink, 'stroke-width': SW, 'stroke-linejoin': 'round' }),
      text({ ...t8, fill: P.shadow, stroke: P.shadow, 'stroke-width': 16, 'stroke-linejoin': 'round', transform: 'translate(5 6)' }, AGE),
      text({ ...t8, fill: P.ink, stroke: P.ink, 'stroke-width': 16, 'stroke-linejoin': 'round' }, AGE),
      g({ 'clip-path': url('age') }, fill),
      text({ x: 0, y: 70, 'text-anchor': 'middle', 'font-family': 'Rubik', 'font-weight': 800, 'font-size': 30, 'letter-spacing': 5, fill: P.ink }, 'ЛЕТ'),
    ]);
  }

  function confetti(yMax) {
    const rnd = rng(20261010);
    const pieces = [];
    const placed = [];
    const edge = 30; // не заходить на рамку
    let tries = 0;
    while (pieces.length < 70 && tries < 20000) {
      tries++;
      const x = FR.x + rnd() * FR.w;
      const y = FR.y + rnd() * (yMax - FR.y);
      const kind = rnd();
      const col = P.confetti[Math.floor(rnd() * P.confetti.length)];
      const rot = Math.floor(rnd() * 180);
      let size, piece;
      if (kind < 0.6) {
        const len = 26 + rnd() * 18;
        size = len / 2 + 5;
        piece = el('line', { x1: -len / 2, y1: 0, x2: len / 2, y2: 0, stroke: col, 'stroke-width': 10, 'stroke-linecap': 'round', transform: `translate(${r1(x)} ${r1(y)}) rotate(${rot})` });
      } else if (kind < 0.83) {
        const rr = 6 + rnd() * 4;
        size = rr;
        piece = el('circle', { cx: r1(x), cy: r1(y), r: r1(rr), fill: col });
      } else {
        const rr = 14 + rnd() * 8;
        size = rr;
        piece = el('path', { d: sparkle(r1(rr)), transform: `translate(${r1(x)} ${r1(y)})`, fill: BW ? P.paper : col, stroke: BW ? P.ink : 'none', 'stroke-width': 3, 'stroke-linejoin': 'round' });
      }
      const pad = size + 10;
      if (x - size < FR.x + edge || x + size > FR.x + FR.w - edge || y - size < FR.y + edge || y + size > yMax) continue;
      if (x < FR.x + FR.rx && y < FR.y + FR.rx && Math.hypot(FR.x + FR.rx - x, FR.y + FR.rx - y) > FR.rx - edge - size) continue;
      if (x > FR.x + FR.w - FR.rx && y < FR.y + FR.rx && Math.hypot(x - (FR.x + FR.w - FR.rx), FR.y + FR.rx - y) > FR.rx - edge - size) continue;
      if (AV.some(([a, b, c, d]) => x > a - pad && x < c + pad && y > b - pad && y < d + pad)) continue;
      if (placed.some(([px, py, ps]) => Math.hypot(px - x, py - y) < 40 + ps + size)) continue;
      placed.push([x, y, size]);
      pieces.push(piece);
    }
    return g({}, pieces);
  }

  function clouds() {
    const c = (x, y, s) => el('path', {
      d: 'M-64 0 A24 24 0 0 1 -40 -28 A32 32 0 0 1 12 -40 A28 28 0 0 1 56 -18 A20 20 0 0 1 66 0 Z',
      transform: `translate(${x} ${y}) scale(${s})`,
      fill: P.paper, stroke: BW ? P.ink : 'none', 'stroke-width': BW ? 4 / s : 0, 'stroke-linejoin': 'round',
    });
    return g({}, [c(520, 1590, 1.1), c(1060, 1600, 0.9), c(700, 1720, 0.7)]);
  }

  function balloons() {
    const body = 'M0 -58 C34 -58 50 -30 46 -2 C42 26 18 46 0 48 C-18 46 -42 26 -46 -2 C-50 -30 -34 -58 0 -58 Z';
    const [nx, ny] = [176, 1702]; // узелок, где сходятся нитки
    const list = [
      [138, 1510, BW ? '#D6D6D6' : P.red, -10],
      [268, 1486, BW ? P.paper : P.yellow, 9],
      [206, 1588, BW ? '#B4B4B4' : P.blue, -3],
    ];
    const parts = [el('line', { x1: nx, y1: ny, x2: nx, y2: 1760, stroke: P.ink, 'stroke-width': 3 })];
    list.forEach(([x, y, , rot]) => {
      const a = (rot * Math.PI) / 180;
      const kx = x - 57 * Math.sin(a), ky = y + 57 * Math.cos(a);
      parts.push(el('path', { d: `M${r1(kx)} ${r1(ky)} C${r1(kx - 6)} ${r1(ky + 50)} ${nx + 10} ${ny - 50} ${nx} ${ny}`, fill: 'none', stroke: P.ink, 'stroke-width': 3 }));
    });
    list.forEach(([x, y, col, rot]) => {
      parts.push(g({ transform: `translate(${x} ${y}) rotate(${rot})` }, [
        el('path', { d: body, fill: col, stroke: P.ink, 'stroke-width': SW }),
        el('path', { d: 'M-8 46 L8 46 L5 57 L-5 57 Z', fill: col, stroke: P.ink, 'stroke-width': 4, 'stroke-linejoin': 'round' }),
        el('path', { d: 'M-26 -26 C-24 -38 -14 -44 -6 -45', fill: 'none', stroke: '#FFFFFF', 'stroke-width': 7, 'stroke-linecap': 'round', opacity: BW ? 1 : 0.85 }),
      ]));
    });
    // бантик
    parts.push(g({ transform: `translate(${nx} ${ny})` }, [
      el('path', { d: 'M0 0 C-10 -14 -26 -10 -24 0 C-26 10 -10 14 0 0 Z', fill: BW ? P.paper : P.magenta, stroke: P.ink, 'stroke-width': 3.5, 'stroke-linejoin': 'round' }),
      el('path', { d: 'M0 0 C10 -14 26 -10 24 0 C26 10 10 14 0 0 Z', fill: BW ? P.paper : P.magenta, stroke: P.ink, 'stroke-width': 3.5, 'stroke-linejoin': 'round' }),
      el('circle', { r: 5, fill: BW ? P.ink : P.magenta, stroke: P.ink, 'stroke-width': 3 }),
    ]));
    return g({}, parts);
  }

  function cityBlock() {
    const parts = [];
    // пожарная часть
    {
      const x0 = 70, w = 250, h = 222, top = G - h;
      parts.push(el('rect', { x: x0 + 176, y: top - 104, width: 62, height: 120, fill: BW ? '#DADADA' : P.red, stroke: P.ink, 'stroke-width': SW }));
      parts.push(el('rect', { x: x0 + 193, y: top - 84, width: 28, height: 36, rx: 6, fill: P.glass, stroke: P.ink, 'stroke-width': 4 }));
      parts.push(el('path', { d: `M${x0 + 166} ${top - 102} L${x0 + 207} ${top - 150} L${x0 + 248} ${top - 102} Z`, fill: BW ? P.ink : P.purple, stroke: P.ink, 'stroke-width': SW, 'stroke-linejoin': 'round' }));
      parts.push(el('line', { x1: x0 + 207, y1: top - 150, x2: x0 + 207, y2: top - 196, stroke: P.ink, 'stroke-width': 5, 'stroke-linecap': 'round' }));
      parts.push(el('path', { d: `M${x0 + 207} ${top - 196} L${x0 + 252} ${top - 184} L${x0 + 207} ${top - 170} Z`, fill: BW ? P.paper : P.yellow, stroke: P.ink, 'stroke-width': 4, 'stroke-linejoin': 'round' }));
      parts.push(el('rect', { x: x0, y: top, width: w, height: h + 20, rx: 8, fill: BW ? P.paper : P.red, stroke: P.ink, 'stroke-width': SW }));
      parts.push(el('rect', { x: x0 - 12, y: top - 12, width: w + 24, height: 34, rx: 10, fill: BW ? P.ink : P.yellow, stroke: P.ink, 'stroke-width': SW }));
      // знак с огоньком
      const fx = x0 + w / 2, fy = top + 64;
      parts.push(el('circle', { cx: fx, cy: fy, r: 32, fill: BW ? P.paper : P.yellow, stroke: P.ink, 'stroke-width': 5 }));
      const flame = 'M2 -24 C10 -12 20 -6 18 8 C16 18 8 22 0 22 C-10 22 -18 16 -18 6 C-18 -4 -12 -8 -10 -16 C-6 -10 -5 -6 -3 -4 C-4 -12 -3 -18 2 -24 Z';
      const core = 'M1 -4 C6 2 9 6 8 11 C7 15 3 17 0 17 C-4 17 -7 14 -7 10 C-7 5 -3 3 -2 -2 C-1 1 0 2 1 4 C1 1 0 -1 1 -4 Z';
      parts.push(el('path', { d: flame, transform: `translate(${fx} ${fy})`, fill: BW ? '#BEBEBE' : P.red, stroke: P.ink, 'stroke-width': 4, 'stroke-linejoin': 'round' }));
      parts.push(el('path', { d: core, transform: `translate(${fx} ${fy})`, fill: BW ? P.paper : P.orange }));
      // ворота гаража
      [x0 + 22, x0 + 136].forEach((dx) => {
        const dw = 92, dh = 118;
        parts.push(el('path', { d: `M${dx} ${G + 4} V${G - dh + 44} A46 46 0 0 1 ${dx + dw} ${G - dh + 44} V${G + 4} Z`, fill: BW ? '#E6E6E6' : P.light, stroke: P.ink, 'stroke-width': SW }));
        for (let k = 1; k <= 4; k++) parts.push(el('line', { x1: dx + 6, y1: G - k * 20, x2: dx + dw - 6, y2: G - k * 20, stroke: P.ink, 'stroke-width': 3 }));
      });
    }
    // больница
    {
      const x0 = 352, w = 184, h = 330, top = G - h;
      parts.push(el('rect', { x: x0, y: top, width: w, height: h + 20, rx: 8, fill: BW ? P.paper : P.light, stroke: P.ink, 'stroke-width': SW }));
      parts.push(el('rect', { x: x0 - 10, y: top - 10, width: w + 20, height: 30, rx: 10, fill: BW ? '#BEBEBE' : P.teal, stroke: P.ink, 'stroke-width': SW }));
      const hx = x0 + w / 2, hy = top + 70;
      parts.push(el('circle', { cx: hx, cy: hy, r: 38, fill: P.paper, stroke: P.ink, 'stroke-width': 5 }));
      parts.push(el('path', { d: `M${hx - 9} ${hy - 26} h18 v17 h17 v18 h-17 v17 h-18 v-17 h-17 v-18 h17 Z`, fill: BW ? P.ink : P.red, stroke: P.ink, 'stroke-width': 3, 'stroke-linejoin': 'round' }));
      for (let row = 0; row < 3; row++) for (let col = 0; col < 2; col++) {
        parts.push(el('rect', { x: x0 + 26 + col * 82, y: top + 122 + row * 50, width: 50, height: 34, rx: 6, fill: P.glass, stroke: P.ink, 'stroke-width': 4 }));
      }
      parts.push(el('rect', { x: x0 + w / 2 - 30, y: G - 54, width: 60, height: 62, rx: 6, fill: BW ? '#DADADA' : P.teal, stroke: P.ink, 'stroke-width': 5 }));
      parts.push(el('line', { x1: x0 + w / 2, y1: G - 54, x2: x0 + w / 2, y2: G, stroke: P.ink, 'stroke-width': 4 }));
      parts.push(el('rect', { x: x0 + w / 2 - 46, y: G - 66, width: 92, height: 15, rx: 6, fill: BW ? P.ink : P.red, stroke: P.ink, 'stroke-width': 4 }));
    }
    // банк
    {
      const x0 = 568, w = 176, top = G - 236;
      parts.push(el('path', { d: `M${x0 - 16} ${top} L${x0 + w / 2} ${top - 72} L${x0 + w + 16} ${top} Z`, fill: BW ? '#DADADA' : P.orange, stroke: P.ink, 'stroke-width': SW, 'stroke-linejoin': 'round' }));
      parts.push(el('circle', { cx: x0 + w / 2, cy: top - 26, r: 17, fill: BW ? P.paper : P.yellow, stroke: P.ink, 'stroke-width': 4 }));
      parts.push(el('rect', { x: x0, y: top + 20, width: w, height: 236, fill: BW ? P.paper : P.yellow, stroke: P.ink, 'stroke-width': SW }));
      parts.push(el('rect', { x: x0 - 16, y: top - 4, width: w + 32, height: 28, rx: 4, fill: P.paper, stroke: P.ink, 'stroke-width': SW }));
      for (let k = 0; k < 4; k++) parts.push(el('rect', { x: x0 + 17 + k * 42, y: top + 34, width: 22, height: 170, rx: 3, fill: P.paper, stroke: P.ink, 'stroke-width': 4 }));
      parts.push(el('rect', { x: x0 - 10, y: G - 30, width: w + 20, height: 16, rx: 3, fill: P.paper, stroke: P.ink, 'stroke-width': 4 }));
      parts.push(el('rect', { x: x0 - 22, y: G - 15, width: w + 44, height: 20, rx: 3, fill: P.paper, stroke: P.ink, 'stroke-width': 4 }));
    }
    // башня аэропорта
    {
      const cx = 842, top = 1700;
      parts.push(el('rect', { x: cx - 76, y: G - 70, width: 152, height: 80, rx: 8, fill: BW ? '#DADADA' : P.lime, stroke: P.ink, 'stroke-width': SW }));
      parts.push(el('path', { d: `M${cx - 34} ${G - 70} L${cx - 28} ${top} L${cx + 28} ${top} L${cx + 34} ${G - 70} Z`, fill: BW ? P.paper : P.blue, stroke: P.ink, 'stroke-width': SW, 'stroke-linejoin': 'round' }));
      for (let k = 0; k < 3; k++) parts.push(el('circle', { cx, cy: top + 50 + k * 72, r: 11, fill: P.glass, stroke: P.ink, 'stroke-width': 4 }));
      parts.push(el('path', { d: `M${cx - 70} ${top} L${cx + 70} ${top} L${cx + 90} ${top - 70} L${cx - 90} ${top - 70} Z`, fill: P.glass, stroke: P.ink, 'stroke-width': SW, 'stroke-linejoin': 'round' }));
      [-45, 0, 45].forEach((k) => parts.push(el('line', { x1: cx + k * 1.0, y1: top - 4, x2: cx + k * 1.28, y2: top - 66, stroke: P.ink, 'stroke-width': 4 })));
      parts.push(el('rect', { x: cx - 100, y: top - 92, width: 200, height: 24, rx: 10, fill: BW ? P.ink : P.blue, stroke: P.ink, 'stroke-width': SW }));
      parts.push(el('line', { x1: cx, y1: top - 92, x2: cx, y2: top - 136, stroke: P.ink, 'stroke-width': 5, 'stroke-linecap': 'round' }));
      parts.push(el('circle', { cx, cy: top - 140, r: 9, fill: BW ? P.paper : P.red, stroke: P.ink, 'stroke-width': 4 }));
    }
    // кафе
    {
      const x0 = 955, w = 205, h = 196, top = G - h;
      parts.push(el('rect', { x: x0, y: top, width: w, height: h + 20, rx: 8, fill: BW ? P.paper : P.pink, stroke: P.ink, 'stroke-width': SW }));
      parts.push(el('rect', { x: x0 - 10, y: top - 12, width: w + 20, height: 30, rx: 10, fill: BW ? P.ink : P.purple, stroke: P.ink, 'stroke-width': SW }));
      // поварской колпак на крыше
      const hx = x0 + w / 2, hy = top - 12;
      parts.push(el('path', { d: `M${hx - 30} ${hy} V${hy - 34} C${hx - 58} ${hy - 36} ${hx - 60} ${hy - 72} ${hx - 34} ${hy - 76} C${hx - 30} ${hy - 100} ${hx - 2} ${hy - 104} ${hx + 6} ${hy - 88} C${hx + 18} ${hy - 104} ${hx + 48} ${hy - 96} ${hx + 44} ${hy - 72} C${hx + 66} ${hy - 66} ${hx + 60} ${hy - 34} ${hx + 30} ${hy - 34} V${hy} Z`, fill: P.paper, stroke: P.ink, 'stroke-width': SW, 'stroke-linejoin': 'round' }));
      parts.push(el('line', { x1: hx - 30, y1: hy - 14, x2: hx + 30, y2: hy - 14, stroke: P.ink, 'stroke-width': 4 }));
      // полосатый навес
      const ax = x0 + 16, aw = w - 32, ay = top + 40, ah = 38, n = 6, sw = aw / n;
      for (let k = 0; k < n; k++) {
        parts.push(el('path', { d: `M${ax + k * sw} ${ay} h${sw} v${ah} a${sw / 2} ${sw / 2} 0 0 1 ${-sw} 0 Z`, fill: k % 2 ? P.paper : (BW ? '#BEBEBE' : P.red), stroke: P.ink, 'stroke-width': 4, 'stroke-linejoin': 'round' }));
      }
      parts.push(el('rect', { x: x0 + 26, y: top + 96, width: 104, height: 70, rx: 6, fill: P.glass, stroke: P.ink, 'stroke-width': 4 }));
      parts.push(el('line', { x1: x0 + 78, y1: top + 96, x2: x0 + 78, y2: top + 166, stroke: P.ink, 'stroke-width': 4 }));
      parts.push(el('rect', { x: x0 + 146, y: G - 92, width: 42, height: 100, rx: 6, fill: BW ? '#DADADA' : P.orange, stroke: P.ink, 'stroke-width': 5 }));
    }
    // стройка: кран с подарком
    {
      const mx = 1352, mw = 38, jy = 1486;
      // недостроенный дом
      const bx = 1196, bw = 150, bh = 112;
      parts.push(el('rect', { x: bx, y: G - bh, width: bw, height: bh + 20, fill: BW ? P.paper : P.orange, stroke: P.ink, 'stroke-width': SW }));
      for (let row = 1; row < 5; row++) {
        parts.push(el('line', { x1: bx, y1: G - row * 26, x2: bx + bw, y2: G - row * 26, stroke: P.ink, 'stroke-width': 3 }));
        for (let k = 0; k < 4; k++) {
          const xx = bx + ((row % 2) * 19) + k * 38;
          if (xx > bx && xx < bx + bw) parts.push(el('line', { x1: xx, y1: G - row * 26, x2: xx, y2: G - row * 26 + 26, stroke: P.ink, 'stroke-width': 3 }));
        }
      }
      // мачта крана
      parts.push(el('rect', { x: mx, y: jy, width: mw, height: G - jy + 10, fill: BW ? P.paper : P.yellow, stroke: P.ink, 'stroke-width': SW }));
      for (let yy = jy + 30; yy < G; yy += 38) {
        parts.push(el('path', { d: `M${mx} ${yy} L${mx + mw} ${yy + 38} M${mx + mw} ${yy} L${mx} ${yy + 38}`, stroke: P.ink, 'stroke-width': 3 }));
      }
      // стрела
      parts.push(el('rect', { x: 1110, y: jy, width: 320, height: 30, fill: BW ? P.paper : P.yellow, stroke: P.ink, 'stroke-width': SW }));
      for (let xx = 1110; xx < 1400; xx += 32) parts.push(el('path', { d: `M${xx} ${jy + 30} L${xx + 16} ${jy} L${xx + 32} ${jy + 30}`, fill: 'none', stroke: P.ink, 'stroke-width': 3 }));
      parts.push(el('rect', { x: mx - 8, y: jy + 30, width: mw + 16, height: 48, rx: 6, fill: BW ? '#DADADA' : P.orange, stroke: P.ink, 'stroke-width': 5 }));
      parts.push(el('rect', { x: mx + 4, y: jy + 40, width: 30, height: 20, rx: 4, fill: P.glass, stroke: P.ink, 'stroke-width': 3 }));
      parts.push(el('path', { d: `M${mx + mw / 2} ${jy} L${mx + mw / 2} ${jy - 40} M${mx + mw / 2} ${jy - 40} L1130 ${jy} M${mx + mw / 2} ${jy - 40} L1418 ${jy}`, stroke: P.ink, 'stroke-width': 3, fill: 'none' }));
      // трос и подарок
      const hx = 1222, by = 1668, bs = 86;
      parts.push(el('line', { x1: hx, y1: jy + 30, x2: hx, y2: by - 30, stroke: P.ink, 'stroke-width': 3 }));
      parts.push(el('path', { d: `M${hx} ${by - 30} L${hx - 40} ${by} M${hx} ${by - 30} L${hx + 40} ${by}`, stroke: P.ink, 'stroke-width': 3 }));
      parts.push(el('path', { d: `M${hx - 8} ${by - 40} a8 8 0 1 1 8 10`, fill: 'none', stroke: P.ink, 'stroke-width': 4 }));
      parts.push(el('rect', { x: hx - bs / 2 + 7, y: by + 9, width: bs, height: bs, rx: 6, fill: P.shadow }));
      parts.push(el('rect', { x: hx - bs / 2, y: by, width: bs, height: bs, rx: 6, fill: BW ? P.paper : P.magenta, stroke: P.ink, 'stroke-width': SW }));
      parts.push(el('rect', { x: hx - 9, y: by, width: 18, height: bs, fill: BW ? '#BEBEBE' : P.yellow, stroke: P.ink, 'stroke-width': 4 }));
      parts.push(el('rect', { x: hx - bs / 2, y: by + bs / 2 - 9, width: bs, height: 18, fill: BW ? '#BEBEBE' : P.yellow, stroke: P.ink, 'stroke-width': 4 }));
    }
    // кусты
    [[338, 38], [553, 30], [930, 34], [1180, 30]].forEach(([bx, r]) => {
      parts.push(el('circle', { cx: bx, cy: G - r * 0.55, r, fill: BW ? '#DADADA' : P.green, stroke: P.ink, 'stroke-width': SW }));
    });
    // земля
    parts.push(el('path', { d: `M${FR.x - 20} ${G} Q ${W * 0.25} ${G - 12} ${W * 0.5} ${G} T ${W + 20} ${G} V ${H} H ${FR.x - 20} Z`, fill: BW ? '#DADADA' : P.grass, stroke: P.ink, 'stroke-width': SW }));
    parts.push(el('path', { d: `M${FR.x} ${G + 34} H ${W}`, stroke: BW ? P.ink : '#FFFFFF', 'stroke-width': 5, 'stroke-dasharray': '26 22', 'stroke-linecap': 'round', opacity: BW ? 0.6 : 0.9 }));
    return g({}, parts);
  }
}

// ---------- измерение текстов ----------
function measure() {
  const res = {};
  for (const [key, it] of Object.entries(TXT)) res[key] = { w: runWidth(it.f, it.w, it.s, it.t) };
  const font = hbFont('Rubik', 900);
  const ext = font.glyphExtents(font.glyph('Н'.codePointAt(0)));
  res.capH = { ascent: (ext.yBearing * 100) / faces.Rubik.upem }; // высота прописной на кегль 100
  return res;
}

const page = (body, css = '') => `<!doctype html><html><head><meta charset="utf-8">
<title>Приглашение на день рождения Миши</title><style>
html,body{margin:0;padding:0;background:#fff}${css}</style></head><body>${body}</body></html>`;

// PNG: дописать чанк pHYs = 300 dpi, чтобы файл печатался в формате A5
function setDpi(file, dpi) {
  const buf = fs.readFileSync(file);
  const ppm = Math.round(dpi / 0.0254);
  const data = Buffer.alloc(9);
  data.writeUInt32BE(ppm, 0); data.writeUInt32BE(ppm, 4); data.writeUInt8(1, 8);
  const type = Buffer.from('pHYs');
  const len = Buffer.alloc(4); len.writeUInt32BE(9);
  const crc = Buffer.alloc(4); crc.writeUInt32BE(zlib.crc32(Buffer.concat([type, data])) >>> 0);
  const ihdrEnd = 8 + 4 + 4 + 13 + 4; // сигнатура + IHDR
  fs.writeFileSync(file, Buffer.concat([buf.subarray(0, ihdrEnd), len, type, data, crc, buf.subarray(ihdrEnd)]));
}

// линия разреза с ножницами посередине листа A4
const CUT = `<svg style="position:absolute;left:0;top:0" width="297mm" height="210mm" viewBox="0 0 2970 2100">
<line x1="1485" y1="0" x2="1485" y2="2100" stroke="#9A9A9A" stroke-width="3" stroke-dasharray="18 14"/>
<rect x="1462" y="26" width="46" height="84" fill="#fff"/>
<g transform="translate(1485 66) rotate(90)" fill="none" stroke="#8A8A8A" stroke-width="4" stroke-linecap="round">
<circle cx="-24" cy="-11" r="9"/><circle cx="-24" cy="11" r="9"/><path d="M-16 -6 L30 9 M-16 6 L30 -9"/></g></svg>`;

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch({ executablePath: CHROME });
  const ctx = await browser.newContext();
  const pg = await ctx.newPage();
  hb = await import('harfbuzzjs');
  const m = measure();

  for (const mode of ['color', 'bw']) {
    // PNG A5 @ 300 dpi = 1748 × 2480
    const svg = buildSVG(mode, m);
    const png = path.join(OUT, `${NAME}-${mode}.png`);
    const f1 = path.join(TMP, `png-${mode}.html`);
    fs.writeFileSync(f1, page(`<div style="width:1748px;height:2480px">${svg}</div>`));
    await pg.setViewportSize({ width: 1748, height: 2480 });
    await pg.goto('file://' + f1);
    await pg.screenshot({ path: png, clip: { x: 0, y: 0, width: 1748, height: 2480 } });
    setDpi(png, 300);

    // PDF: A4 альбомный, две открытки (масштаб 93 %, чтобы принтер ничего не обрезал) + линия разреза
    const s = 0.93, cw = 148 * s, ch = 210 * s;
    const card = (i) => `<div style="position:absolute;left:${(148.5 - cw) / 2 + i * 148.5}mm;top:${(210 - ch) / 2}mm;width:${cw}mm;height:${ch}mm">${buildSVG(mode, m, 'c' + i + '_')}</div>`;
    const f2 = path.join(TMP, `pdf-${mode}.html`);
    fs.writeFileSync(f2, page(`<div style="position:relative;width:297mm;height:209.9mm;overflow:hidden">${card(0)}${card(1)}${CUT}</div>`, '@page{size:297mm 210mm;margin:0}'));
    await pg.goto('file://' + f2);
    await pg.pdf({ path: path.join(OUT, `${NAME}-${mode}-A4.pdf`), width: '297mm', height: '210mm', printBackground: true, margin: { top: 0, right: 0, bottom: 0, left: 0 } });
  }
  await browser.close();
  fs.rmSync(TMP, { recursive: true, force: true });
  console.log('ok', OUT);
})().catch((e) => { console.error(e); process.exit(1); });
