#!/usr/bin/env bash
# Rebuild evidence/demo.mp4 from the six real terminal frames.
# Requires ffmpeg with drawtext, and the voiceover/caption files in evidence/.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
S="$ROOT/evidence/shots"
C="$ROOT/evidence/captions"
V="$ROOT/evidence/vo"
OUT="$ROOT/evidence/demo.mp4"

FF="${FFMPEG_BIN:-}"
if [ -z "$FF" ]; then
  FF="$(command -v ffmpeg || true)"
fi
if [ -z "$FF" ]; then
  FF="$(ls /nix/store/*ffmpeg*/bin/ffmpeg 2>/dev/null | head -1)"
fi
if [ -z "$FF" ]; then
  echo "ffmpeg not found; set FFMPEG_BIN" >&2
  exit 1
fi

FONT="${FONT_BOLD:-$(fc-match -f '%{file}' 'DejaVu Sans:style=Bold' 2>/dev/null || true)}"
MONO="${FONT_MONO:-$(fc-match -f '%{file}' 'DejaVu Sans Mono:style=Bold' 2>/dev/null || true)}"
[ -f "$FONT" ] || { echo "bold font not found; set FONT_BOLD" >&2; exit 1; }
[ -f "$MONO" ] || { echo "mono font not found; set FONT_MONO" >&2; exit 1; }

FILTER="$(mktemp)"
trap 'rm -f "$FILTER"' EXIT

cat > "$FILTER" <<EOF
[0:v]scale=1280:720,format=yuv420p,
 drawtext=fontfile=$FONT:text='NEAR AGENT FLEET':fontcolor=0xb8ff5a:fontsize=78:x=80:y=210,
 drawtext=fontfile=$FONT:text='Three paid agents. One operator.':fontcolor=0xf2ead8:fontsize=40:x=82:y=330,
 drawtext=fontfile=$FONT:text='Live on NEAR Agent Market':fontcolor=0x9c9484:fontsize=30:x=82:y=390,
 drawtext=fontfile=$MONO:text='Built at NEAR HACKATHON  ·  2026-11-09':fontcolor=0x9c9484:fontsize=20:x=82:y=600,
 fade=t=in:st=0:d=0.35,fade=t=out:st=11.65:d=0.35[v0];
[1:v]scale=1280:720,format=yuv420p,
 drawbox=x=0:y=632:w=1280:h=88:color=0x0b0b0b:t=fill,
 drawbox=x=0:y=632:w=1280:h=2:color=0xb8ff5a:t=fill,
 drawtext=fontfile=$MONO:textfile=$C/02.txt:fontcolor=0xf2ead8:fontsize=23:x=40:y=662,
 fade=t=in:st=0:d=0.35,fade=t=out:st=15.65:d=0.35[v1];
[2:v]scale=1280:720,format=yuv420p,
 drawbox=x=0:y=632:w=1280:h=88:color=0x0b0b0b:t=fill,
 drawbox=x=0:y=632:w=1280:h=2:color=0xb8ff5a:t=fill,
 drawtext=fontfile=$MONO:textfile=$C/03.txt:fontcolor=0xf2ead8:fontsize=23:x=40:y=662,
 fade=t=in:st=0:d=0.35,fade=t=out:st=12.65:d=0.35[v2];
[3:v]scale=1280:720,format=yuv420p,
 drawbox=x=0:y=632:w=1280:h=88:color=0x0b0b0b:t=fill,
 drawbox=x=0:y=632:w=1280:h=2:color=0xb8ff5a:t=fill,
 drawtext=fontfile=$MONO:textfile=$C/04.txt:fontcolor=0xf2ead8:fontsize=23:x=40:y=662,
 fade=t=in:st=0:d=0.35,fade=t=out:st=9.65:d=0.35[v3];
[4:v]scale=1280:720,format=yuv420p,
 drawbox=x=0:y=632:w=1280:h=88:color=0x0b0b0b:t=fill,
 drawbox=x=0:y=632:w=1280:h=2:color=0xb8ff5a:t=fill,
 drawtext=fontfile=$MONO:textfile=$C/05.txt:fontcolor=0xf2ead8:fontsize=23:x=40:y=662,
 fade=t=in:st=0:d=0.35,fade=t=out:st=15.65:d=0.35[v4];
[5:v]scale=1280:720,format=yuv420p,
 drawbox=x=0:y=632:w=1280:h=88:color=0x0b0b0b:t=fill,
 drawbox=x=0:y=632:w=1280:h=2:color=0xb8ff5a:t=fill,
 drawtext=fontfile=$MONO:textfile=$C/06.txt:fontcolor=0xf2ead8:fontsize=23:x=40:y=662,
 fade=t=in:st=0:d=0.35,fade=t=out:st=11.65:d=0.35[v5];
[6:v]scale=1280:720,format=yuv420p,
 drawtext=fontfile=$FONT:text='Built at NEAR HACKATHON':fontcolor=0xb8ff5a:fontsize=44:x=(w-text_w)/2:y=250,
 drawtext=fontfile=$FONT:text='github.com/matrixneoaiagent/near-agent-fleet':fontcolor=0xf2ead8:fontsize=28:x=(w-text_w)/2:y=340,
 drawtext=fontfile=$MONO:text='market.near.ai/a/brandnamechecker':fontcolor=0x9c9484:fontsize=20:x=(w-text_w)/2:y=420,
 drawtext=fontfile=$MONO:text='market.near.ai/a/media-studio-agent':fontcolor=0x9c9484:fontsize=20:x=(w-text_w)/2:y=452,
 drawtext=fontfile=$MONO:text='market.near.ai/a/clipper-agent':fontcolor=0x9c9484:fontsize=20:x=(w-text_w)/2:y=484,
 fade=t=in:st=0:d=0.35,fade=t=out:st=9.65:d=0.35[v6];
[v0][v1][v2][v3][v4][v5][v6]concat=n=7:v=1:a=0[vout];
[8:a]volume=0.95,adelay=1000|1000[a1];
[9:a]volume=0.95,adelay=13000|13000[a2];
[10:a]volume=0.95,adelay=29000|29000[a3];
[11:a]volume=0.95,adelay=42000|42000[a4];
[12:a]volume=0.95,adelay=52000|52000[a5];
[13:a]volume=0.95,adelay=68000|68000[a6];
[14:a]volume=0.95,adelay=80000|80000[a7];
[7:a][a1][a2][a3][a4][a5][a6][a7]amix=inputs=8:duration=first:normalize=0[aout]
EOF

"$FF" -y \
  -f lavfi -t 12 -i "color=c=0x0b0b0b:s=1280x720:r=30" \
  -loop 1 -t 16 -i "$S/02-check.png" \
  -loop 1 -t 13 -i "$S/03-route-video.png" \
  -loop 1 -t 10 -i "$S/04-route-brand.png" \
  -loop 1 -t 16 -i "$S/05-brand.png" \
  -loop 1 -t 12 -i "$S/06-tests.png" \
  -f lavfi -t 10 -i "color=c=0x0b0b0b:s=1280x720:r=30" \
  -f lavfi -t 89 -i "anullsrc=r=48000:cl=stereo" \
  -i "$V/01.mp3" -i "$V/02.mp3" -i "$V/03.mp3" -i "$V/04.mp3" \
  -i "$V/05.mp3" -i "$V/06.mp3" -i "$V/07.mp3" \
  -filter_complex_script "$FILTER" \
  -map "[vout]" -map "[aout]" -t 89 \
  -c:v libx264 -preset medium -crf 20 -pix_fmt yuv420p -r 30 \
  -c:a aac -b:a 160k -movflags +faststart "$OUT"

echo "wrote $OUT"
