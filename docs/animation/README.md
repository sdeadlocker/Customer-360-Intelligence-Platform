# Animated visuals for LinkedIn

Two self-contained, animated diagrams you can turn into a GIF or MP4 for a LinkedIn post.
LinkedIn does not render live HTML/SVG — it plays **video and GIF** natively — so the workflow is:
open the HTML, screen-record the animation, export a GIF/MP4, upload.

| File | Shows |
|---|---|
| `architecture.html` | A request flowing down the layered stack (presentation → edge → services → agents → data), with the "enforced end to end" guarantees. |
| `ask-ai-flow.html` | The Ask AI grounding pipeline lighting up step by step: question → tools → facts → validation → cited answer. |

Each is a fixed **1280×720** frame — a clean 16:9 for the feed.

## Turn it into a GIF / MP4

**Option A — screen record (simplest, works on any OS)**
1. Open the `.html` file in your browser (double-click it).
2. Record the animated area:
   - **Windows:** press `Win + G` (Xbox Game Bar) → record, or use ScreenToGif (free, exports GIF directly).
   - **macOS:** `Shift + Cmd + 5` → record selection.
3. Capture ~8–12 seconds (one full animation loop), then trim.
4. Upload the MP4/GIF to LinkedIn as the post's media.

**Option B — pixel-perfect capture with a headless browser (optional)**
If you have Node + Playwright, you can record a clean MP4 without desktop clutter:

```sh
npm i -D playwright
npx playwright install chromium
```

```js
// record.mjs — run: node record.mjs
import { chromium } from 'playwright';
const browser = await chromium.launch();
const ctx = await browser.newContext({
  viewport: { width: 1280, height: 720 },
  recordVideo: { dir: '.', size: { width: 1280, height: 720 } },
});
const page = await ctx.newPage();
await page.goto('file://' + process.cwd() + '/architecture.html');
await page.waitForTimeout(9000); // capture one loop
await ctx.close();               // writes a .webm next to this script
await browser.close();
```

Convert the `.webm` to GIF/MP4 with ffmpeg if needed:

```sh
ffmpeg -i input.webm -vf "fps=15,scale=1280:-1:flags=lanczos" output.gif
ffmpeg -i input.webm -c:v libx264 -pix_fmt yuv420p output.mp4
```

## Tips

- 8–12 seconds is plenty for the feed; keep the file small (GIF < 5 MB, MP4 < ~30 MB).
- Even better than a diagram: a **screen recording of the real running app** answering an Ask AI
  question. Nothing proves "it works" like the live product.
- The diagrams honour `prefers-reduced-motion`, so they render static if your OS has motion reduced.
