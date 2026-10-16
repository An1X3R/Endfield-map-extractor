function clamp(value: number): number { return Math.min(1, Math.max(0, value)); }
function phase(time: number, start: number, duration: number): number { return clamp((time - start) / duration); }
function damp(value: number): number { return 1 - Math.pow(1 - clamp(value), 4); }
function noise(seed: number): number { return (Math.sin(seed * 127.1 + 311.7) * 43758.5453) % 1 + .5; }
function random(seed: number): number { return Math.abs(noise(seed)) % 1; }

const palette = { color: '#d95338', stone: '#b9b8aa', floor: '#66685e', water: '#32776a' }

function contour(cx: number, cy: number, rx: number, ry: number, seed: number, scale: number): string {
  return Array.from({ length: 56 }, (_, index) => {
    const angle = index / 56 * Math.PI * 2;
    const radius = 1 + Math.sin(angle * 3 + seed) * .15 + Math.sin(angle * 7 - seed * 2) * .075 + Math.cos(angle * 11 + seed) * .03;
    return `${index ? 'L' : 'M'}${(cx + Math.cos(angle) * rx * radius * scale).toFixed(1)},${(cy + Math.sin(angle) * ry * radius * scale).toFixed(1)}`;
  }).join('') + 'Z';
}

function mapArtwork(): string {
  const crags = [
    [260, 133, 85, 83, 1], [432, 174, 76, 122, 4], [553, 295, 73, 69, 7], [385, 350, 100, 71, 12],
    [247, 385, 80, 133, 18], [167, 585, 82, 104, 13], [380, 600, 120, 98, 9], [580, 571, 76, 125, 17],
    [623, 447, 39, 46, 19], [471, 458, 35, 42, 23], [289, 711, 42, 36, 25], [609, 171, 29, 35, 27],
  ];
  const land = crags.map(([x, y, rx, ry, seed]) => `<g><path d="${contour(x, y + 12, rx + 8, ry + 8, seed, 1)}" fill="#101813"/>${Array.from({ length: 9 }, (_, k) => `<path d="${contour(x - k * .6, y - k * 1.9, rx, ry, seed, 1 - k * .066)}" fill="${k === 0 ? palette.floor : k % 3 === 0 ? palette.stone : palette.floor}" fill-opacity="${k === 0 ? 1 : .38}" stroke="${palette.stone}" stroke-opacity="${.17 + k * .035}" stroke-width="${k % 3 === 0 ? 1.2 : .5}"/>`).join('')}<path d="M${x - rx * .25} ${y - ry * .65}l${rx * .18} ${ry * .7}l${rx * .3} ${-ry * .25}l${rx * .15} ${ry * .6}" fill="none" stroke="#f1e9d3" stroke-opacity=".27" stroke-width="2"/></g>`).join('');
  const trees = crags.slice(0, 9).map(([x, y, rx, ry, seed]) => Array.from({ length: 26 }, (_, k) => {
    const px = x + (random(seed * 93 + k * 3) - .5) * rx * 1.7;
    const py = y + (random(seed * 64 + k * 7) - .5) * ry * 1.5;
    const size = 3 + random(k * 8 + seed) * 4;
    return `<path d="M${px} ${py - size}l${size * 1.2} ${size}l${-size * .4} ${size}l${-size * 1.4} ${-size * .1}l${-size * .3} ${-size}Z" fill="${k % 5 === 0 ? '#e4b979' : palette.color}" opacity="${.35 + random(k * 7) * .5}"/>`;
  }).join('')).join('');
  const structures = Array.from({ length: 52 }, (_, k) => {
    const x = 285 + (k % 9) * 13 + Math.sin(k * .8) * 14;
    const y = 535 + Math.floor(k / 9) * 15;
    const w = 8;
    return `<path d="M${x} ${y}h${w}v9h-${w}Z" fill="#b9bbab" stroke="#282f26" stroke-width="1.3"/><path d="M${x - 2} ${y - 1}h${w + 4}l-2 -4h-${w}Z" fill="#7d6550"/>`;
  }).join('');
  return `<g class="map-content"><g class="layer-water"><path d="M345 0Q233 91 350 267T412 429Q429 506 299 552T272 800" fill="none" stroke="${palette.water}" stroke-width="84" stroke-opacity=".23"/><path d="M345 0Q233 91 350 267T412 429Q429 506 299 552T272 800" fill="none" stroke="${palette.water}" stroke-width="26" stroke-opacity=".65"/></g><g class="layer-terrain">${land}</g><g class="layer-vegetation">${trees}</g><g class="layer-buildings">${structures}<path d="M277 582H440M333 523V654" fill="none" stroke="#c9bfa2" stroke-width="2" opacity=".5"/></g><g class="layer-effects" fill="none" stroke="${palette.color}"><path d="M154 404H660M407 114V734" opacity=".24"/><circle cx="407" cy="404" r="6"/><path d="M402 404h10m-5 -5v10" stroke="#f4ecd9"/></g><rect width="800" height="800" fill="url(#map-grid)"/><g font-family="Consolas,monospace" font-size="8" letter-spacing="1" fill="#b6bda9" opacity=".6"><text x="139" y="221">03—A / RIDGE STUDY</text><text x="473" y="689">03—B / SETTLEMENT</text><text x="568" y="386">128</text></g></g>`;
}

function ambientArtwork(): string {
  const paths = Array.from({ length: 38 }, (_, k) => {
    const d = contour(250, 360, 160 + k * 29, 80 + k * 24, 2.1, 1);
    return `<path d="${d}" fill="none" stroke="${k === 15 ? '#d95338' : k === 28 ? '#2e9e85' : '#7d8975'}" stroke-opacity="${k === 15 || k === 28 ? .28 : .17}" stroke-width="${k % 5 === 0 ? 1 : .65}"/>`;
  }).join('');
  return `<defs><linearGradient id="contour-fade"><stop stop-color="white"/><stop offset=".35" stop-color="#777"/><stop offset=".65" stop-color="black"/><stop offset="1" stop-color="white"/></linearGradient><mask id="ambient-mask"><rect width="1600" height="1000" fill="url(#contour-fade)"/></mask><mask id="contour-gaps"><rect width="1600" height="1000" fill="white"/><g class="mist-strands" fill="black"><path d="M174 -20Q281 101 207 309T219 630L185 734Q162 595 184 434Q260 154 149 -20Z"/><path d="M1010 -20Q1134 126 1049 468T1109 1040H1211Q1058 784 1131 448T1082 -20Z"/><path d="M-20 645Q211 513 368 573T798 664L800 720Q489 637 350 612T-20 691Z"/></g></mask></defs><g mask="url(#ambient-mask)"><g mask="url(#contour-gaps)"><g class="flow">${paths}</g><g class="flow" transform="translate(1300 700) scale(.65)">${paths}</g></g></g><g opacity=".55"><g class="floating-mark"><path d="M21 460L36 450L43 460L32 479L24 471Z" fill="#a1a99b"/><path d="M21 460L36 450L32 467Z" fill="#ced0bd"/><path d="M31 443V403" stroke="#8a9681" stroke-dasharray="2 6"/></g><g class="floating-mark"><path d="M1564 533L1584 523L1593 543L1579 551Z" fill="#8e9d87"/><path d="M1564 533L1584 523L1579 543Z" fill="#d1d2c0"/><path d="M1579 516V477" stroke="#8a9681" stroke-dasharray="2 6"/></g><g class="floating-mark">${leaf(39, 665, 6, 22, '#d95338')}${leaf(1525, 355, 5, -18, '#bc7844')}${leaf(1545, 750, 4, 30, '#2e9e85')}</g></g>`;
}

function ditherDefinition(): string {
  const thresholds = [0,8,2,10,12,4,14,6,3,11,1,9,15,7,13,5];
  const cell = `<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4">${thresholds.map((value, i) => `<rect x="${i % 4}" y="${Math.floor(i / 4)}" width="1" height="1" fill="rgb(${value * 17},${value * 17},${value * 17})"/>`).join('')}</svg>`;
  return `<filter id="si-dither" x="0" y="0" width="100%" height="100%" color-interpolation-filters="sRGB"><feColorMatrix in="SourceGraphic" type="saturate" values="0" result="luma"/><feImage href="data:image/svg+xml,${encodeURIComponent(cell)}" x="0" y="0" width="4" height="4" result="cell"/><feTile in="cell" result="threshold"/><feComposite in="luma" in2="threshold" operator="arithmetic" k1="0" k2="1.6" k3=".32" k4="-.16" result="screen"/><feComponentTransfer in="screen" result="levels"><feFuncR type="discrete" tableValues="0 .333 .667 1"/><feFuncG type="discrete" tableValues="0 .333 .667 1"/><feFuncB type="discrete" tableValues="0 .333 .667 1"/></feComponentTransfer><feComponentTransfer in="levels" result="palette"><feFuncR type="linear" slope=".68" intercept=".18"/><feFuncG type="linear" slope=".67" intercept=".20"/><feFuncB type="linear" slope=".57" intercept=".16"/></feComponentTransfer><feComposite in="palette" in2="SourceGraphic" operator="in" result="dithered"/><feComposite in="SourceGraphic" in2="dithered" operator="arithmetic" k1="0" k2=".78" k3=".22" k4="0"/></filter>`;
}

function leaf(cx: number, cy: number, size: number, angle: number, color: string): string {
  return `<path d="M0 ${-size}L${size * .3} ${-size * .3}L${size * .9} ${-size * .65}L${size * .7} 0L${size} ${size * .25}L${size * .25} ${size * .6}L0 ${size}L${-size * .25} ${size * .5}L${-size * .9} ${size * .3}L${-size * .6} ${-size * .1}L${-size * .8} ${-size * .5}L${-size * .2} ${-size * .3}Z" transform="translate(${cx} ${cy}) rotate(${angle})" fill="${color}"/>`;
}

function paintedMountains(): string {
  const silhouettes = [
    'M-20 482Q25 456 48 394L63 366L72 369L80 331L89 326L97 342L108 349Q123 399 146 417L177 473Z',
    'M103 479L129 422L142 393L152 327L169 303L175 253L188 232L197 237L203 222L212 232L216 277L231 295L238 358L255 380Q280 419 301 478Z',
    'M226 476Q262 419 271 358L287 326L294 252L306 229L309 184L320 152L331 147L337 167L335 200L346 216L349 286L365 316L372 368Q402 419 417 478Z',
    'M363 471L393 402L405 350L421 326L427 269L439 247L443 202L455 181L465 168L473 178L476 213L487 239L493 313L506 339L517 389Q545 431 577 475Z',
    'M494 476Q558 429 583 390L596 354L610 347L621 305L638 295L649 311L654 345L670 360Q680 409 721 438L751 481Z',
    'M641 481L682 426L701 391L705 358L719 341L726 296L738 283L744 261L759 255L767 277L770 318L785 339L794 394L829 423L871 477Z',
    'M787 483Q839 454 872 395L889 383L897 334L913 322L916 286L928 269L939 276L941 311L952 330L969 396Q994 441 1047 479Z',
    'M959 483L1013 433L1034 409L1044 374L1061 356L1067 330L1083 321L1092 333L1101 375L1120 391L1138 424L1180 446V483Z',
  ];
  const anchors = [[85,326,125,46],[197,223,255,67],[329,147,330,64],[464,168,310,57],[633,296,183,50],[757,255,225,57],[934,270,208,59],[1084,322,155,55]];
  const folds = anchors.map(([x,y,height,width], index) => Array.from({length:13}, (_, k) => {
    const spread = (k - 6) / 7;
    return `<path d="M${x + spread * 7} ${y + 12 + Math.abs(k - 6) * 9}Q${x + spread * width * .22} ${y + height * .25} ${x + spread * width * .44} ${y + height * .44}L${x + spread * width * .39 + Math.sin(k + index) * 5} ${y + height * .59}Q${x + spread * width * .67} ${y + height * .78} ${x + spread * width} ${y + height}" fill="none" stroke="${k % 3 === 0 ? '#f3efe2' : '#465c4b'}" stroke-width="${k % 3 === 0 ? 2.2 : .55}" opacity="${index < 4 ? .32 : .09}"/>`;
  }).join('')).join('');
  return `<g id="mountain-scene"><g filter="url(#ink-fiber)">${silhouettes.map(d=>`<path d="${d}" fill="url(#mountain-fade)"/>`).join('')}${folds}</g><path d="M0 452Q83 406 176 439T358 427T563 417T824 426T1180 438" fill="none" stroke="#537d68" stroke-width="18" opacity=".08"/><rect y="355" width="1180" height="110" fill="url(#mist)"/><g fill="#406a54" opacity=".16"><path d="M68 403L78 391L81 408L75 415Z M389 221L400 209L406 228L395 233Z M425 252L431 244L440 257L434 265Z M353 159L362 145L368 158L360 165Z"/></g></g>`;
}

function completionArtwork(): string {
  const branches: Array<[string, number, number]> = [
    ['M274 474Q298 429 266 371Q240 331 222 297Q199 271 172 250', 20, 380],
    ['M271 401Q298 354 275 312Q263 287 266 247Q264 217 250 188', 14, 470],
    ['M270 360Q224 350 182 330Q137 313 103 311Q72 309 51 295', 11, 520],
    ['M278 336Q313 310 355 309Q399 308 427 283', 10, 550],
    ['M241 326Q231 288 208 268L182 218L160 210', 8, 560],
    ['M268 292Q303 268 319 240Q337 220 365 215', 7, 580],
    ['M182 330Q162 300 132 284L111 259', 6, 620],
    ['M315 314Q331 286 348 279L374 269', 6, 650],
    ['M207 269Q185 270 144 253Q126 244 105 245', 5, 650],
    ['M267 248Q286 225 289 205L309 186', 5, 680],
    ['M254 208L233 181L213 174', 4, 680],
    ['M361 308L394 323L423 312', 4, 720],
    ['M137 316L104 332L75 326', 4, 720],
  ];
  const clusters = [[108,247,48,24],[174,213,43,26],[214,176,43,20],[253,181,41,27],[304,185,38,20],[359,211,42,23],[375,270,51,24],[421,283,39,20],[424,316,40,18],[55,294,38,21],[78,329,40,18],[140,280,43,22],[211,269,39,24],[290,241,37,20],[323,286,40,22]];
  const branchOutlines = [
    'M258 374Q235 332 218 300Q198 270 172 250Q205 267 228 293Q245 328 277 370Z',
    'M273 399Q287 354 271 315Q258 286 264 249Q266 217 250 188Q272 216 269 247Q269 285 281 310Q301 354 284 401Z',
    'M274 369Q219 353 180 335Q142 319 103 314Q70 312 51 295Q76 307 103 308Q146 309 187 326Q224 342 268 350Z',
    'M271 344Q313 305 356 306Q400 305 427 283Q404 313 356 313Q317 314 284 344Z',
    'M235 328Q226 289 205 270L181 221L160 210L184 217L212 265Q236 287 246 324Z',
    'M264 297Q300 270 317 240Q337 218 365 215Q340 222 322 243Q309 273 274 297Z',
    'M178 334Q158 301 130 287L111 259L136 282Q167 300 187 329Z',
    'M311 316Q329 283 348 277L374 269L350 281Q331 289 320 315Z',
    'M210 273Q181 272 143 255Q121 246 105 245Q125 240 146 251Q180 264 207 266Z',
    'M264 251Q284 227 287 204L309 186L292 207Q287 234 271 251Z',
    'M251 211L231 184L213 174L235 180L257 208Z',
    'M359 306L395 321L423 312L395 325L361 310Z',
    'M139 318L105 334L75 326L104 330L135 314Z',
  ];
  const leaves = clusters.map(([x, y, width, height], index) => `<g data-crown="${index}"><path d="${contour(x, y, width * .68, height * .6, index, 1)}" fill="${index % 3 === 0 ? '#a54431' : '#bc5435'}" opacity=".86"/>${Array.from({ length: 105 }, (_, k) => {
    const angle = random(index * 721 + k * 8) * Math.PI * 2;
    const radius = Math.sqrt(random(index * 37 + k * 17));
    const px = x + Math.cos(angle) * width * radius;
    const py = y + Math.sin(angle) * height * radius;
    const colors = ['#ac4531','#cb4b31','#db6040','#e08a4d','#ca673c','#b33c2c'];
    return leaf(px, py, 1.8 + random(k * 25 + index) * 3.6, random(k + index * 8) * 130, colors[(k + index) % colors.length]);
  }).join('')}</g>`).join('');
  const bark = Array.from({ length: 15 }, (_, k) => `<path d="M${266 + k * 1.6} 472Q${296 + k * .1} 429 ${266 + k * .8} 375" stroke="${k % 3 === 0 ? '#d2c9ad' : '#807e65'}" stroke-width="${k % 3 === 0 ? .8 : .4}" opacity=".6" fill="none"/>`).join('');
  return `<svg viewBox="0 0 1180 610" preserveAspectRatio="none" aria-hidden="true"><defs>
   ${ditherDefinition()}
   <filter id="ink-fiber" x="-4%" y="-4%" width="108%" height="108%"><feTurbulence type="fractalNoise" baseFrequency=".08" numOctaves="2" seed="12" result="fiber"/><feDisplacementMap in="SourceGraphic" in2="fiber" scale="2.3" xChannelSelector="R" yChannelSelector="G"/></filter>
   ${branches.map(([d, w, start], index) => `<mask id="branch-mask-${index}"><path class="tree-branch" data-start="${start}" d="${d}" fill="none" stroke="white" stroke-width="50" pathLength="1"/></mask>`).join('')}
   <linearGradient id="mountain-fade" gradientUnits="userSpaceOnUse" x1="0" x2="1180"><stop stop-color="#344c3d" stop-opacity=".46"/><stop offset=".41" stop-color="#597e69" stop-opacity=".19"/><stop offset=".57" stop-color="#6b8f74" stop-opacity=".09"/><stop offset="1" stop-color="#496d58" stop-opacity=".08"/></linearGradient>
   <linearGradient id="ground-haze" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#f4f1ea" stop-opacity="0"/><stop offset="1" stop-color="#f4f1ea"/></linearGradient>
   <linearGradient id="cliff-fade" x1="0" y1="0" x2=".1" y2="1"><stop stop-color="#a5ad97"/><stop offset="1" stop-color="#e7e7da" stop-opacity="0"/></linearGradient>
   <linearGradient id="mist" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#f4f1ea" stop-opacity="0"/><stop offset=".58" stop-color="#f4f1ea"/><stop offset="1" stop-color="#f4f1ea" stop-opacity="0"/></linearGradient>
   <pattern id="fine-dither" width="4" height="4" patternUnits="userSpaceOnUse"><rect width="1" height="1" fill="#f4f1ea"/><rect x="2" y="2" width="1" height="1" fill="#f4f1ea"/><rect x="0" y="2" width=".6" height=".6" fill="#232c22" opacity=".6"/></pattern>
   <mask id="trunk-reveal"><path id="trunk-mask-path" d="M277 490Q302 428 266 369Q237 327 223 295Q199 267 169 245" fill="none" stroke="white" stroke-width="60" stroke-linecap="round" pathLength="1"/></mask>
   <clipPath id="rock-clip"><path d="M-30 497L37 473L92 482L126 462L193 469L225 449L283 459L312 441L336 455L377 446L398 466L456 474L430 494L376 516L339 550L321 615H-30Z"/></clipPath>
   <clipPath id="paint-aperture"><rect id="paint-aperture-rect" x="590" y="0" width="0" height="610"/></clipPath>
   </defs><g clip-path="url(#paint-aperture)">
   ${paintedMountains()}
   <g id="rock-scene"><g clip-path="url(#rock-clip)"><rect x="-30" y="440" width="500" height="190" fill="url(#cliff-fade)"/><path d="M-30 497L37 473L92 482L126 462L193 469L225 449L283 459L312 441L336 455L377 446L398 466L456 474L430 494L376 516L339 550L321 615L306 544L278 528L248 521L197 544L149 566L106 605L84 543L25 522Z" fill="#7f8c76" opacity=".7"/><path d="M-30 497L37 473L92 482L126 462L193 469L225 449L283 459L312 441L336 455L377 446L398 466L456 474L390 481L344 471L301 490L252 481L213 487L153 478L103 501L51 492Z" fill="#d8d8c5"/><path d="M104 501L153 478L139 507L120 527L106 605L84 543L25 522L51 492Z M301 490L344 471L334 518L321 615L306 544L278 528Z" fill="#5a6c59" opacity=".8"/>${Array.from({ length: 25 }, (_, k) => `<path d="M${k * 18 - 10} ${488 + Math.sin(k) * 10}l-7 22l4 17l-13 43" fill="none" stroke="#e4e1cd" stroke-width="${k % 3 === 0 ? 2 : .7}" opacity=".4"/>`).join('')}<rect x="-30" y="440" width="500" height="180" fill="url(#fine-dither)" class="dither-layer"/></g><path class="rock-line" d="M-30 497L37 473L92 482L126 462L193 469L225 449L283 459L312 441L336 455L377 446L398 466L456 474" fill="none" stroke="#64735c" stroke-width="2" pathLength="1"/></g>
   <rect x="0" y="518" width="470" height="93" fill="url(#ground-haze)"/><g id="tree-scene"><g mask="url(#trunk-reveal)"><path d="M247 485L261 464Q277 436 267 399L246 359L223 318L211 289L184 265L164 250L174 246L203 267L232 288L250 321L278 361Q302 394 299 431L291 458L307 472L291 472L277 486L267 478Z" fill="#394738"/><path d="M267 474Q289 431 274 392L248 345L228 302L203 276L225 289L242 310L254 339L282 383Q302 433 282 472Z" fill="#8b8b6c"/>${bark}<path d="M254 481L234 485L245 479L265 469M284 470L316 477L335 472" fill="none" stroke="#56634c" stroke-width="3"/></g>${branchOutlines.map((d, index) => `<path d="${d}" fill="#46503c" mask="url(#branch-mask-${index})"/>`).join('')}<g id="leaf-crowns">${leaves}</g><g class="dither-layer" id="canopy-grain" style="mix-blend-mode:soft-light">${clusters.map(([x, y, width, height]) => `<ellipse cx="${x}" cy="${y}" rx="${width}" ry="${height}" fill="url(#fine-dither)"/>`).join('')}</g></g>
   <g id="falling-leaves">${leaf(470, 331, 5, 35, '#c85333')}${leaf(508, 380, 3.5, 74, '#d48642')}${leaf(802, 469, 4, 120, '#c75337')}${leaf(852, 480, 3, 75, '#d49752')}</g><g id="brush-traces" fill="none" stroke="#8a937f" opacity=".17"><path d="M756 494Q875 508 1052 472" stroke-width="3"/><path d="M781 499Q920 507 1022 480" stroke-width=".7"/><path d="M860 505L907 502M918 498L988 487" stroke-width=".5"/></g>
   </g></svg>`;
}


function inkWedge(progress: number): string {
  const p = clamp(progress);
  const tip = { x: 340 + 500 * p, y: 820 - 2000 * Math.pow(p, 1.25) };
  const base = { x: 330 + 80 * p, y: 870 + 80 * p };
  const spread = 6 + 3100 * p * p;
  const edge = (side: number, t: number): string => {
    const wave = Math.sin(t * 31 + side) * 4 + Math.sin(t * 79) * 2.5;
    const taper = Math.sin(t * Math.PI);
    const x = tip.x + (base.x + side * spread - tip.x) * t + (wave + side * 37 * taper) * taper;
    const y = tip.y + (base.y - tip.y) * t + wave * .5;
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  };
  const left = Array.from({ length: 25 }, (_, i) => `L${edge(-1, (i + 1) / 25)}`).join('');
  const right = Array.from({ length: 25 }, (_, i) => `L${edge(1, 1 - i / 25)}`).join('');
  return `M${tip.x.toFixed(2)},${tip.y.toFixed(2)}${left}${right}Z`;
}


export { clamp, phase, damp, inkWedge }

export function namespaceArtwork(markup: string): string {
  return markup.replace(/id="([^"]+)"/g, 'id="si-$1"').replace(/url\(#([^)]+)\)/g, 'url(#si-$1)').replace(/class="([^"]+)"/g, (_match: string, classes: string) => `class="${classes.split(' ').map(name => `si-${name}`).join(' ')}"`)
}
export const PAINTING_ART = namespaceArtwork(mapArtwork())
export const AMBIENT_ART = namespaceArtwork(ambientArtwork())
export const COMPLETION_ART = namespaceArtwork(completionArtwork())
