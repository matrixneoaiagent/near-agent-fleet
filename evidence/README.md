# Evidence

All files here were produced on 2026-11-09 from live public services. No
metrics, users, revenue or URLs are invented.

| File | What it is | How it was produced |
|---|---|---|
| `fleet-status.json` | Machine-readable status of all three agents | `python3 fleet.py check --json evidence/fleet-status.json` |
| `fleet-status.md` | Human-readable version of the same run | `--md evidence/fleet-status.md` |
| `test-run.txt` | Full unit test output | `python3 -m unittest discover -s tests -v` |
| `brand-check-vellumforge.json` | Real public brand-check job result | `POST /v1/check` on the live brand agent, 2026-11-09 |
| `shots/01-title.png` | Terminal title frame | Captured with xterm + ImageMagick `import` |
| `shots/02-check.png` | Live `fleet.py status` output | Same |
| `shots/03-route-video.png` | Router output for a video brief | Same |
| `shots/04-route-brand.png` | Router output for a brand brief | Same |
| `shots/05-brand.png` | Real brand-check result | Same |
| `shots/06-tests.png` | Unit tests + summary | Same |
| `demo.mp4` | 89-second narrated demo | Assembled from the six frames; captions + TTS in post |
| `poster.png` | 1280×720 title frame | `ffmpeg -ss 2` from `demo.mp4` |
| `screenshot-check.png` | 1280×720 status frame | `ffmpeg -ss 15` from `demo.mp4` |
| `vo/`, `captions/` | Voiceover and caption source files | edge-tts + text files |

## Key facts in the evidence

- 3/3 agents live, 12/12 public checks passed.
- All three listings show `Delivered jobs: 0`. This is a live prototype with
  public test hires and real reports, not a revenue-generating business yet.
- Brand check: `vellumforge` — 2/2 domains available (`.studio`, `.dev`),
  1/1 X handle available, 0 exact-name apps, 0 possible trademark conflicts.
  The report URL is a Cloudflare quick tunnel and is live only while the brand
  service runs. The stable mirror is <https://x0.at/nmZT.md>.
- The brand-check result is a point-in-time registry/platform observation, not
  legal advice and not a guarantee that a registrar will sell the domain.

## Reproduce

```bash
python3 fleet.py check --json evidence/fleet-status.json --md evidence/fleet-status.md
python3 -m unittest discover -s tests -v > evidence/test-run.txt 2>&1
./demo.sh
```

The status timestamp and latency will differ on each run; the assertions are
the same four public checks per agent.
