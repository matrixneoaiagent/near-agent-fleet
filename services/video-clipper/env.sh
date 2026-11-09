#!/usr/bin/env bash
# Runtime library paths for the Python media stack (NixOS host).
export LD_LIBRARY_PATH="/nix/store/b6mjyiadysqlh7nps52faznnqmp32604-zlib-1.3.1/lib:/nix/store/hh698a2nnpqr47lh52n26wi8fiah3hid-gcc-13.3.0-lib/lib:/nix/store/mkhhjfg2isjbfx87dz191bzpnwx1bbr9-gcc-13.3.0-libgcc/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export FFMPEG_BIN="${FFMPEG_BIN:-/nix/store/42z02ipbn03xsbg07yih4983pbqhwgjw-ffmpeg-7.1.1-bin/bin/ffmpeg}"
export FFPROBE_BIN="${FFPROBE_BIN:-/nix/store/42z02ipbn03xsbg07yih4983pbqhwgjw-ffmpeg-7.1.1-bin/bin/ffprobe}"
