# 90-second demo

**Video:** [`../evidence/demo.mp4`](../evidence/demo.mp4) — 1280×720, 89 s, H.264 + AAC.

The video is cut from six terminal frames captured on 2026-11-09 while the
commands ran against the live agents. Captions and a synthesized voiceover were
added in post; no output was staged or edited. The raw frames are in
`evidence/shots/` and the status JSON is in `evidence/fleet-status.json`.

## Timeline

| Time | Visual | Voiceover | What is real |
|---|---|---|---|
| 0:00–0:12 | Title card | Three paid agents, one operator, zero employees. They live on NEAR Agent Market, where buyers hire them by the job. | — |
| 0:12–0:28 | `fleet.py status` | One command checks every public listing, health endpoint, agent card and public test artifact. Twelve of twelve checks pass. | Live HTTP calls, 2026-11-09T13:28:36Z |
| 0:28–0:41 | `fleet.py route` (video) | Describe the job in plain language. The router scores the brief, picks the specialist, and returns the price, SLA and public hire link. | Deterministic router output |
| 0:41–0:51 | `fleet.py route` (brand) | A branding brief goes to the five dollar checker. | Same router |
| 0:51–1:07 | `fleet.py brand vellumforge` | This is a real public job, not a mock. Two domains free, the X handle free, and a report with a timestamp and source for every row. | Real `/v1/check` job; report URL in the frame |
| 1:07–1:19 | `unittest` | Seventeen unit tests pass. Open source, reproducible, honest limits. | 17 tests, offline |
| 1:19–1:29 | End card | Built at NEAR Hackathon. Repo and live listings in the post. | — |

## Reproduce the demo

```bash
cd near-agent-fleet
./demo.sh          # paced, about 75 seconds of live output
./demo.sh --fast   # no pauses, for CI
```

`demo.sh` performs, in order:

```bash
python3 fleet.py status
python3 fleet.py route "I need five vertical clips with captions from a 40-minute podcast"
python3 fleet.py route "Check the brand name Vellumforge on .studio, .dev and the X handle"
python3 fleet.py brand vellumforge
python3 -m unittest discover -s tests
```

Expected final lines:

```text
12/12 public checks · 3/3 agents live · 17 tests pass
Hire: market.near.ai/a/brandnamechecker
      market.near.ai/a/media-studio-agent
      market.near.ai/a/clipper-agent
Source: github.com/matrixneoaiagent/near-agent-fleet
```

## If a tunnel is down

The market listings are stable. The agent health and report URLs are Cloudflare
quick tunnels and can rotate. Restart the affected worker (`./start.sh`), or run
the control plane locally:

```bash
python3 fleet.py serve --port 8780
# open http://127.0.0.1:8780
```

The verifier will show `degraded` for an agent whose health endpoint is
unreachable; it will not pretend the agent is live.

## Recording notes

The recording environment could not run a continuous X11 capture (the available
ffmpeg build has no `x11grab`, and the Chromium profile could not reach the local
server). The six frames were captured with `xterm` + ImageMagick `import`, then
assembled with ffmpeg. This is why the video is a cut of real terminal states
rather than a single take. The commands, timestamps and reports in the frames
are real.

To rebuild the video from the included frames:

```bash
# requires ffmpeg with drawtext, ImageMagick, and the voiceover files
# see scripts/build_demo.sh for the exact filter graph
./scripts/build_demo.sh
```
