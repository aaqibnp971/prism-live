"""Compare the shared field renderer with the ten extracted design frames in Edge.

The last script block of field-frames.html is JSON containing HTML, not an application to
launch. This tool extracts just its original, pure DCLogic component methods, supplies an
inert props holder and uses those methods to recreate the original CSS panels. It NEVER
navigates to the bundle, loads its framework, or executes its other scripts. The generated
page is explicitly diagnostic and is not a simulation mode in either live screen.

Run: python -m tools.check_field_browser --output logs/field-acceptance
An already installed Edge/Chromium is required; nothing is downloaded or installed.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import html
import json
import os
import subprocess
import tempfile
import time
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from websockets.asyncio.client import connect

from tools.check_spectator_browser import Cdp, browser_path

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "docs/design/field-frames.html"


class _Scripts(HTMLParser):
    """Read script text as data, without interpreting any of it."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.scripts = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.current = (dict(attrs), [])

    def handle_data(self, data):
        if self.current is not None:
            self.current[1].append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.current is not None:
            attrs, chunks = self.current
            self.scripts.append((attrs, "".join(chunks)))
            self.current = None


def extract_component(path=REFERENCE):
    """Decode the LAST outer script, then select only the original DC component."""
    outer = _Scripts()
    outer.feed(path.read_text(encoding="utf-8-sig"))
    if not outer.scripts:
        raise ValueError("Design bundle contains no scripts")
    try:
        component_html = json.loads(outer.scripts[-1][1])
    except json.JSONDecodeError as error:
        raise ValueError("Last script must be the JSON-escaped HTML design") from error
    if not isinstance(component_html, str):
        raise ValueError("Last script must contain one JSON string, not a manifest")
    inner = _Scripts()
    inner.feed(component_html)
    components = [
        script
        for attrs, script in inner.scripts
        if attrs.get("type") == "text/x-dc" and "data-dc-script" in attrs
    ]
    if len(components) != 1 or "class Component extends DCLogic" not in components[0]:
        raise ValueError("Expected exactly one original DCLogic field component")
    return components[0]


PAGE = r"""<!doctype html><html lang="en"><meta charset="utf-8">
<title>DIAGNOSTIC — original field reference / shared renderer</title>
<link rel="stylesheet" href="__FONTS__">
<style>
*{box-sizing:border-box}body{margin:0;background:#262522;color:#eceae7;
font-family:'IBM Plex Sans',sans-serif}header{padding:32px 48px 20px}
.eyebrow{font:500 14px 'IBM Plex Mono',monospace;letter-spacing:.07em;color:#bab6ad}
h1{font-weight:500;font-size:32px;line-height:1.2;margin:10px 0}
#tokens{font:15px 'IBM Plex Mono',monospace;color:#bab6ad}
main{display:grid;grid-template-columns:1fr 1fr;gap:32px;padding:0 48px}
section{min-width:0;border-top:1px solid #57544c;padding-top:16px}
h2{font-size:18px;font-weight:500;margin:0 0 16px}
.panels{display:flex;justify-content:center;gap:12px}figure{margin:0}
figcaption{font:13px 'IBM Plex Mono',monospace;margin-top:10px;color:#bab6ad}
canvas{display:block;border-radius:2px}footer{padding:24px 48px;color:#bab6ad;
font:14px/1.5 'IBM Plex Sans',sans-serif;position:absolute;bottom:0}
</style>
<header><div class="eyebrow">DIAGNOSTIC ONLY · TEN EXACT DESIGN ENDPOINTS · NO LIVE FEED</div>
<h1 id="name"></h1><div id="tokens"></div></header>
<main><section><h2>REFERENCE — extracted original CSS / formulas</h2>
<div class="panels" id="reference"></div></section>
<section><h2>IMPLEMENTATION — shared plain-JavaScript renderer</h2>
<div class="panels" id="implementation"></div></section></main>
<footer>Rest / peak are frozen comparisons, not an animated heartbeat or a live session.<br>
Both sides use the reference's exact seven values. Local fonts; original bundle never run.</footer>
<script src="__RENDERER__"></script><script src="__MAPPING__"></script><script>
class DCLogic { constructor() { this.props = {}; } }
__COMPONENT__
const reference = new Component();
const tokenKeys = ['fog','light','hue','sat','horizon','pulse','motion'];
window.showFrame = index => {
  reference.props = {panelSize:index >= 8 ? 400 : 700,peakAll:false,surround:'dark'};
  const frame = reference.renderVals().frames[index];
  if (!frame) throw new Error('Missing reference frame '+index);
  const tokens = Object.fromEntries(tokenKeys.map(k=>[k,parseFloat(frame.v[k])]));
  document.getElementById('name').textContent = frame.num+' · '+frame.name;
  document.getElementById('tokens').textContent = tokenKeys.map(k=>
    k+' '+frame.v[k]).join('   ·   ');
  const left = document.getElementById('reference');
  const right = document.getElementById('implementation');
  left.replaceChildren(); right.replaceChildren();
  window.renderers?.forEach(renderer=>renderer.dispose());
  window.renderers = [];
  frame.panels.forEach((panel,i)=> {
    const original = document.createElement('div');
    original.id = 'reference-'+i;
    Object.assign(original.style,panel.frameStyle);
    for (const key of ['baseStyle','hazeStyle','streakStyle','fogStyle','lightStyle']) {
      const layer = document.createElement('div');
      Object.assign(layer.style,panel[key]); original.append(layer);
    }
    const canvas = document.createElement('canvas');
    canvas.id = 'implementation-'+i;
    canvas.width = canvas.height = reference.props.panelSize;
    canvas.style.width = canvas.style.height = reference.props.panelSize+'px';
    for (const [holder,node] of [[left,original],[right,canvas]]) {
      const figure = document.createElement('figure');
      const caption = document.createElement('figcaption');
      caption.textContent = panel.caption || 'REST · static design endpoint';
      figure.append(node,caption);holder.append(figure);
    }
    const renderer = new PrismFieldRenderer.Renderer(canvas);
    renderer.render([{tokens,weight:1}],{pulse:i === 1 ? 1 : 0,drift:0});
    window.renderers.push(renderer);
  });
  window.currentFrame = {index:index+1,name:frame.name,tokens,panels:frame.panels.map(p=>
    p.caption || 'Rest')};
  return window.currentFrame;
};
window.showFrame(0);
</script></html>"""


def comparison_page(component):
    return (
        PAGE.replace("__FONTS__", (ROOT / "web/spectator/fonts/fonts.css").as_uri())
        .replace("__RENDERER__", (ROOT / "web/shared/field_renderer.js").as_uri())
        .replace("__MAPPING__", (ROOT / "web/shared/field_mapping.js").as_uri())
        .replace("__COMPONENT__", component)
    )


METRICS = r"""async data => {
  const img = new Image(); img.src = 'data:image/png;base64,'+data;
  await img.decode();
  const shot = document.createElement('canvas'); shot.width=img.width;shot.height=img.height;
  const context = shot.getContext('2d',{willReadFrequently:true});context.drawImage(img,0,0);
  const srgb = v => v <= .04045 ? v / 12.92 : ((v+.055)/1.055)**2.4;
  const luminance = (data,i) => .2126*srgb(data[i]/255)+.7152*srgb(data[i+1]/255)+
    .0722*srgb(data[i+2]/255);
  return window.currentFrame.panels.map((caption,index)=> {
    const pixels = id => {
      const rect = document.getElementById(id+'-'+index).getBoundingClientRect();
      if(rect.left < 0 || rect.top < 0 || rect.right > innerWidth || rect.bottom > innerHeight)
        throw new Error('Comparison panel outside screenshot '+id);
      // Exclude the two-pixel rounded reference/implementation corner treatment.
      return context.getImageData(Math.round(rect.left)+2,Math.round(rect.top)+2,
        Math.round(rect.width)-4,Math.round(rect.height)-4);
    };
    const ref=pixels('reference'),actual=pixels('implementation');
    if(ref.width!==actual.width||ref.height!==actual.height)throw new Error('Unequal panel sizes');
    let absolute=0,squared=0,maximum=0,refY=0,actualY=0,yAbsolute=0;
    const count=ref.width*ref.height;
    for(let i=0;i<ref.data.length;i+=4){
      for(let channel=0;channel<3;channel++){
        const difference=Math.abs(ref.data[i+channel]-actual.data[i+channel])/255;
        absolute+=difference;squared+=difference*difference;maximum=Math.max(maximum,difference);
      }
      const a=luminance(ref.data,i),b=luminance(actual.data,i);
      refY+=a;actualY+=b;yAbsolute+=Math.abs(a-b);
    }
    return {caption,size:[ref.width,ref.height],rgb_mae:absolute/(3*count),
      rgb_rmse:Math.sqrt(squared/(3*count)),rgb_max_difference:maximum,
      reference_mean_luminance:refY/count,rendered_mean_luminance:actualY/count,
      luminance_mae:yAbsolute/count};
  });
}"""

# Temporal scheduling/rise across 45..180 bpm is covered separately in the pulse tests.
# This check observes real shader framebuffer bytes, rather than trusting the CPU oracle
# as the actual rendered output. The maximum (peak) remains independent of repetition rate.
GPU_SETUP = r"""(() => {
  const tokens = reference.renderVals().frames.map(frame=>
    Object.fromEntries(tokenKeys.map(k=>[k,parseFloat(frame.v[k])])));
  const cases=tokens.map((t,i)=>({name:'frame '+(i+1),layers:[{tokens:t,weight:1}]}));
  tokens.forEach((t,i)=>cases.push({name:'frame '+(i+1)+' at .22 rail',
    layers:[{tokens:{...t,pulse:.22},weight:1}]}));
  for(let tenth=0;tenth<=10;tenth++) cases.push({name:'cool/warm blend '+tenth+'/10',
    layers:[{tokens:{...tokens[1],pulse:.22},weight:1-tenth/10},
      {tokens:{...tokens[2],pulse:.22},weight:tenth/10}]});
  const canvas=document.createElement('canvas');canvas.width=64;canvas.height=48;
  const renderer=new PrismFieldRenderer.Renderer(canvas);
  const Y=(data,i)=>PrismFieldRenderer.luminance([data[i],data[i+1],data[i+2]].map(v=>
    PrismFieldRenderer.linear(v/255)));
  window.gpuSafetyCase=index=> {
    const test=cases[index],width=canvas.width,height=canvas.height;
    renderer.render(test.layers,{pulse:0,drift:0});const rest=renderer.readPixels();
    const expected=[];
    for(let row=0;row<height;row++)for(let col=0;col<width;col++){
      expected.push(PrismFieldRenderer.sampleLayers(test.layers,(col+.5)/width,
        1-(row+.5)/height,{pulse:0,width,height}));
    }
    let maxRelative=0,maxAbsolute=0,maxBudgetExcess=0,maxOracleError=0;
    let unchangedOutside=0,checkedPixels=0;
    for(const pulse of [.25,.5,.75,1]) {
      renderer.render(test.layers,{pulse,drift:0});const actual=renderer.readPixels();
      for(let pixel=0;pixel<expected.length;pixel++){
        const i=pixel*4,base=expected[pixel],delta=Y(actual,i)-Y(rest,i);
        maxRelative=Math.max(maxRelative,delta/base.baseLuminance);
        maxAbsolute=Math.max(maxAbsolute,delta);
        maxBudgetExcess=Math.max(maxBudgetExcess,delta-base.budget);
        // CPU/GPU float arithmetic may differ near an 8-bit rounding threshold;
        // the cap uses linear-light actual output, not a tolerated byte overshoot.
        if(delta < -1e-8 || delta > .22*base.baseLuminance+1e-6 ||
          delta > base.budget+1e-6 || delta > .09+1e-6)
          throw new Error(JSON.stringify({test:test.name,pulse,pixel,delta,
            base:base.baseLuminance,budget:base.budget}));
        if(base.layers.every(layer=>layer.rest.fogAlpha===0 && layer.rest.streakAlpha===0 &&
          layer.rest.lightAlpha===0)) {
          if([0,1,2].some(c=>actual[i+c]!==rest[i+c]))
            throw new Error('Pulse changed pixels outside fog/light '+test.name);
          unchangedOutside++;
        }
        const row=Math.floor(pixel/width),col=pixel%width;
        const oracle=PrismFieldRenderer.sampleLayers(test.layers,(col+.5)/width,
          1-(row+.5)/height,{pulse,width,height});
        [0,1,2].forEach(c=>maxOracleError=Math.max(maxOracleError,
          Math.abs(actual[i+c]/255-oracle.rgb[c])));
        checkedPixels++;
      }
    }
    if(maxOracleError>1/255+1e-6)throw new Error('GPU/CPU divergence '+test.name);
    if(unchangedOutside===0)throw new Error('No outside-mask pixels checked');
    return {name:test.name,max_relative_delta:maxRelative,max_absolute_delta:maxAbsolute,
      max_budget_excess:maxBudgetExcess,max_oracle_channel_error:maxOracleError,
      unchanged_outside_pixels:unchangedOutside,checked_pixels:checkedPixels};
  };
  window.disposeSafetyRenderer=()=>renderer.dispose();
  return cases.length;
})()"""


# Separate from the exact reference endpoints/31 existing safety cases: exercise the
# production mapping at EVERY integer rate, then read actual displayed framebuffer bytes.
# Unit tests establish the 90 ms envelope peak; this checks its maximum rendered contrast.
GPU_TAPER_SETUP = r"""(() => {
  const segments=['baseline','load','regulate','resolve'];
  const canvas=document.createElement('canvas');canvas.width=32;canvas.height=24;
  const renderer=new PrismFieldRenderer.Renderer(canvas);
  const width=canvas.width,height=canvas.height;
  const Y=(data,i)=>PrismFieldRenderer.luminance([data[i],data[i+1],data[i+2]].map(v=>
    PrismFieldRenderer.linear(v/255)));
  window.gpuTaperSegment=index=> {
    const segment=segments[index];
    const state={type:'state',v:1,session:'S-20260922-0001',seq:1,t_engine:20000,
      t_session:20000,segment,segment_elapsed_ms:20000,
      segment_nominal_ms:segment==='load'||segment==='regulate'?75000:45000,
      psv:{arousal:.8,cognitive_load:.6,readiness:.5,valence:.5},
      confidence:{arousal:.393,cognitive_load:.393,readiness:.393,valence:0},
      authority:{arousal:segment==='baseline'?0:segment==='load'?.2:1,
        cognitive_load:segment==='baseline'?0:segment==='load'?.2:1,readiness:0,valence:0}};
    let maxRelative=0,maxAbsolute=0,unchangedOutside=0,checkedPixels=0;
    let zeroCases=0,zeroPixels=0,lowRateVisibleCases=0;
    for(let bpm=45;bpm<=180;bpm++) {
      const tokens=PrismFieldMapping.mapState({...state,hr_bpm:bpm});
      const layers=[{tokens,weight:1}];
      renderer.render(layers,{pulse:0,drift:0});const rest=renderer.readPixels();
      renderer.render(layers,{pulse:1,drift:0});const peak=renderer.readPixels();
      let changedPixels=0;
      for(let row=0;row<height;row++)for(let col=0;col<width;col++) {
        const i=(row*width+col)*4;
        const base=PrismFieldRenderer.sampleLayers(layers,(col+.5)/width,
          1-(row+.5)/height,{pulse:0,width,height});
        const delta=Y(peak,i)-Y(rest,i);
        const changed=[0,1,2,3].some(channel=>peak[i+channel]!==rest[i+channel]);
        if(changed)changedPixels++;
        maxRelative=Math.max(maxRelative,delta/base.baseLuminance);
        maxAbsolute=Math.max(maxAbsolute,delta);
        if(delta < -1e-8 || delta > .22*base.baseLuminance+1e-6 ||
          delta > base.budget+1e-6 || delta > .09+1e-6)
          throw new Error(JSON.stringify({segment,bpm,pixel:i/4,delta,
            base:base.baseLuminance,budget:base.budget}));
        if(base.layers.every(layer=>layer.rest.fogAlpha===0 &&
          layer.rest.streakAlpha===0 && layer.rest.lightAlpha===0)) {
          if(changed)throw new Error('Rate taper changed outside fog/light '+segment+' '+bpm);
          unchangedOutside++;
        }
        if(bpm>=120) {
          if(changed)throw new Error('Visual pulse survived cutoff '+segment+' '+bpm);
          zeroPixels++;
        }
        checkedPixels++;
      }
      if(bpm<=95) {
        if(changedPixels===0)
          throw new Error('Authored low-rate pulse disappeared '+segment+' '+bpm);
        lowRateVisibleCases++;
      }
      if(bpm>=120)zeroCases++;
    }
    if(unchangedOutside===0)throw new Error('No outside-mask pixels checked '+segment);
    return {segment,rate_count:136,zero_amplitude_cases:zeroCases,
      zero_amplitude_pixels:zeroPixels,low_rate_visible_cases:lowRateVisibleCases,
      max_relative_delta:maxRelative,max_absolute_delta:maxAbsolute,
      unchanged_outside_pixels:unchangedOutside,checked_pixels:checkedPixels};
  };
  window.disposeTaperRenderer=()=>renderer.dispose();
  return {segment_count:segments.length,rate_count:136,min_bpm:45,max_bpm:180,
    cutoff_bpm:120,peak_age_ms:90,framebuffer:[width,height]};
})()"""


async def _compare(cdp, output, page):
    await cdp.call("Runtime.enable")
    await cdp.call("Network.enable")
    await cdp.call(
        "Emulation.setDeviceMetricsOverride",
        width=1920,
        height=1080,
        deviceScaleFactor=1,
        mobile=False,
    )
    await cdp.call("Page.navigate", url=page.resolve().as_uri())
    await cdp.until("!!window.currentFrame")
    await cdp.evaluate("document.fonts.ready.then(()=>true)")
    report = {
        "frames": [],
        "local_fonts_loaded": await cdp.evaluate(
            "document.fonts.check('500 18px \"IBM Plex Sans\"') && "
            "document.fonts.check('500 14px \"IBM Plex Mono\"')"
        ),
    }
    assert report["local_fonts_loaded"]
    for index in range(10):
        frame = await cdp.evaluate(f"window.showFrame({index})")
        await cdp.evaluate("new Promise(resolve=>requestAnimationFrame(()=>resolve(true)))")
        shot = await cdp.call("Page.captureScreenshot", format="png", captureBeyondViewport=False)
        frame["differences"] = await cdp.evaluate(f"({METRICS})({json.dumps(shot['data'])})")
        if output:
            filename = f"frame-{index + 1:02d}-1920x1080.png"
            (output / filename).write_bytes(base64.b64decode(shot["data"]))
            frame["screenshot"] = filename
        report["frames"].append(frame)
    safety_count = await cdp.evaluate(GPU_SETUP)
    report["rendered_pulse_safety"] = [
        await cdp.evaluate(f"window.gpuSafetyCase({index})") for index in range(safety_count)
    ]
    await cdp.evaluate("window.disposeSafetyRenderer()")
    taper = await cdp.evaluate(GPU_TAPER_SETUP)
    taper["segments"] = [
        await cdp.evaluate(f"window.gpuTaperSegment({index})")
        for index in range(taper["segment_count"])
    ]
    taper["zero_amplitude_cases"] = sum(
        segment["zero_amplitude_cases"] for segment in taper["segments"]
    )
    taper["checked_pixels"] = sum(segment["checked_pixels"] for segment in taper["segments"])
    report["rendered_rate_taper"] = taper
    await cdp.evaluate("window.disposeTaperRenderer()")
    report["external_requests"] = [u for u in cdp.requests if u.startswith(("http:", "https:"))]
    assert not report["external_requests"], report["external_requests"]
    assert not cdp.errors, cdp.errors
    if output:
        contact = output / "contact-sheet.html"
        contact.write_text(
            '<!doctype html><meta charset="utf-8"><title>Ten field comparisons</title>'
            '<style>body{margin:0;background:#262522}main{display:grid;'
            'grid-template-columns:1fr 1fr;width:1920px}img{display:block;width:960px;'
            'height:540px}</style><main>'
            + "".join(
                f'<img src="{html.escape(frame["screenshot"])}" alt="'
                f'{html.escape(frame["name"])}">'
                for frame in report["frames"]
            )
            + "</main>",
            encoding="utf-8",
        )
        await cdp.call(
            "Emulation.setDeviceMetricsOverride",
            width=1920,
            height=2700,
            deviceScaleFactor=1,
            mobile=False,
        )
        await cdp.call("Page.navigate", url=contact.resolve().as_uri())
        await cdp.until(
            "document.images.length===10 && [...document.images].every(i=>i.complete)"
        )
        await cdp.screenshot(output / "ten-frame-contact-sheet.png")
    return report


async def check(browser, output=None, reference=REFERENCE):
    component = extract_component(reference)
    if output:
        output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="prism-field-", ignore_cleanup_errors=True) as temp:
        working = Path(temp)
        page = (output or working) / "reference-comparison.html"
        page.write_text(comparison_page(component), encoding="utf-8")
        process = subprocess.Popen(
            [
                browser,
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                "--disable-background-networking",
                "--disable-extensions",
                "--remote-debugging-port=0",
                f"--user-data-dir={working / 'profile'}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            port_file = working / "profile/DevToolsActivePort"
            deadline = time.monotonic() + 20
            while not port_file.exists():
                if time.monotonic() > deadline:
                    raise RuntimeError("Browser failed to open its local debugging port")
                await asyncio.sleep(0.1)
            port = int(port_file.read_text().splitlines()[0])

            def targets():
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5) as reply:
                    return json.load(reply)

            target = next(t for t in await asyncio.to_thread(targets) if t["type"] == "page")
            async with connect(target["webSocketDebuggerUrl"], max_size=None) as socket:
                cdp = Cdp(socket)
                report = await _compare(cdp, output, page)
                await cdp.call("Browser.close")
        finally:
            with contextlib.suppress(subprocess.TimeoutExpired):
                await asyncio.to_thread(process.wait, 5)
            if process.poll() is None:
                process.terminate()
                await asyncio.to_thread(process.wait, 5)
    report.update(
        reference=str(reference),
        component_sha256=hashlib.sha256(component.encode()).hexdigest(),
        renderer_sha256=hashlib.sha256(
            (ROOT / "web/shared/field_renderer.js").read_bytes()
        ).hexdigest(),
        extraction="Last outer script JSON decoded; original pure DC component only; no bundle run",
        viewport=[1920, 1080],
        metric="RGB differences in 0..1 sRGB; luminance is linear-light Rec.709 relative Y",
    )
    if output:
        (output / "checks.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--reference", type=Path, default=REFERENCE)
    args = parser.parse_args()
    browser = browser_path()
    if browser is None:
        raise SystemExit("An installed Edge/Chromium browser is required. Nothing was installed.")
    print(json.dumps(asyncio.run(check(browser, args.output, args.reference)), indent=2))


if __name__ == "__main__":
    main()
