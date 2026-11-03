// Render Cozy from the site's existing Three.js model; no browser runs in the shipped app.
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const root = path.resolve(__dirname, '..');
const frontend = path.join(root, 'cozy-markets/frontend');
const ts = require(path.join(frontend, 'node_modules/typescript'));
const { chromium } = require(path.join(frontend, 'node_modules/playwright-core'));

(async () => {
  const out = path.join(root, 'dist/splash-preview');
  fs.mkdirSync(out, { recursive: true });
  const source = fs.readFileSync(path.join(frontend, 'src/components/pet3d.tsx'), 'utf8');
  const imports = source.slice(source.indexOf('import {\n  BackSide'), source.indexOf('} from "three";') + 15)
    .replace('"three"', JSON.stringify(pathToFileURL(path.join(frontend, 'node_modules/three/build/three.module.js')).href));
  const model = source.slice(source.indexOf('const INK ='), source.indexOf('// ---------------------------------------------------------------- the engine'));
  const code = `${imports}\n${model}\n
    const model = buildModel();
    for (const eye of model.eyes) for (const key of ['joy', 'shut', 'squeeze', 'swirl']) eye[key].visible = false;
    for (const [key, mouth] of Object.entries(model.mouths)) mouth.visible = key === 'smile';
    model.glass.visible = true;
    model.root.rotation.y = -0.13;
    const canvas = document.querySelector('canvas');
    const renderer = new WebGLRenderer({canvas, alpha:true, antialias:true});
    renderer.setPixelRatio(2);
    renderer.setSize(250,250,false);
    const scene = new Scene();
    const camera = new PerspectiveCamera(30,1,10,2000);
    camera.position.set(0,12,300);
    camera.lookAt(0,0,0);
    scene.add(new HemisphereLight(0xfff6ea,0x9a86d8,2.2));
    const key = new DirectionalLight(0xffffff,2.4);
    key.position.set(-0.55,1,0.9);
    scene.add(key,model.root);
    renderer.render(scene,camera);
    document.documentElement.dataset.ready='true';`;
  fs.writeFileSync(path.join(out, 'pet.js'), ts.transpileModule(code, {
    compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ES2020 }
  }).outputText);
  const font = name => pathToFileURL(path.join(root, 'mprice_decoder/assets/fonts', name)).href;
  fs.writeFileSync(path.join(out, 'index.html'), `<!doctype html><html><meta charset="utf-8"><style>
  @font-face{font-family:Fredoka;src:url('${font('Fredoka-SemiBold.ttf')}')}
  @font-face{font-family:Nunito;src:url('${font('Nunito-Bold.ttf')}')}
  *{box-sizing:border-box}html,body{margin:0;width:560px;height:350px;overflow:hidden;background:transparent}
  .card{width:560px;height:350px;background:#fff8ec;border:2px solid #9b633a;border-radius:20px;position:relative;overflow:hidden;font-family:Nunito;color:#4b3025}
  .halo{position:absolute;width:270px;height:270px;right:-38px;top:12px;border-radius:50%;background:radial-gradient(circle,#ffe0ae 0,#fff0da 53%,#fff8ec 72%)}
  .brand{position:absolute;left:34px;top:30px;font-size:12px;letter-spacing:2.5px;color:#9a6543}
  h1{position:absolute;left:32px;top:70px;margin:0;font:43px Fredoka;letter-spacing:-.9px;color:#603a22}
  .version{display:inline-block;font:12px Nunito;padding:3px 9px;border:1px solid #ead1ae;border-radius:8px;background:#fff0d9;color:#9a6543;vertical-align:middle;margin-left:8px;letter-spacing:0}
  .tagline{position:absolute;left:34px;top:137px;font-size:16px;line-height:1.6;color:#8a674e}
  .welcome{position:absolute;left:34px;top:204px;font-size:13px;color:#9b755c}
  canvas{position:absolute;right:-1px;top:42px;width:250px;height:250px}
  .shadow{position:absolute;right:59px;top:252px;width:130px;height:13px;border-radius:50%;background:#704e4c18;filter:blur(2px)}
  .divider{position:absolute;left:34px;right:34px;top:273px;height:1px;background:#eedfc9}
  .footer{position:absolute;left:34px;bottom:18px;font-size:10px;letter-spacing:.6px;color:#ae8d70}
  </style><div class="card"><div class="halo"></div><div class="brand">COZY MARKETS</div>
  <h1>MaplePrice<span class="version">2.5</span></h1><div class="tagline">A little help for<br>your next great find.</div>
  <div class="welcome">Cozy is getting things ready.</div><div class="shadow"></div><canvas></canvas>
  <div class="divider"></div><div class="footer">YOUR MARKET COMPANION</div></div><script type="module" src="./pet.js"></script></html>`);
  const browser = await chromium.launch({
    executablePath: process.env.SPLASH_CHROMIUM || '/home/cheng/.cache/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell',
    headless: true, args: ['--no-sandbox','--allow-file-access-from-files','--use-angle=swiftshader','--enable-unsafe-swiftshader']
  });
  try {
    const page = await browser.newPage({viewport:{width:560,height:350},deviceScaleFactor:1});
    page.on('pageerror', error => process.stderr.write(String(error)+'\n'));
    await page.goto(pathToFileURL(path.join(out,'index.html')).href);
    await page.waitForFunction(() => document.documentElement.dataset.ready === 'true');
    await page.evaluate(() => document.fonts.ready);
    const target = path.join(root,'mprice_decoder/assets/splash.png');
    await page.screenshot({path:target,omitBackground:true});
    console.log(target);
  } finally { await browser.close(); }
})().catch(error => {console.error(error);process.exitCode=1});
