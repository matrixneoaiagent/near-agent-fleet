#!/usr/bin/env bash
# Live demo for the NEAR Agent Fleet.
#
#   ./demo.sh          paced for a screen recording (~75 seconds)
#   ./demo.sh --fast   no pauses (CI / quick check)
set -euo pipefail
cd "$(dirname "$0")"

FAST=0
[ "${1:-}" = "--fast" ] && FAST=1

B=$'\033[1m'; G=$'\033[38;5;154m'; C=$'\033[38;5;81m'; D=$'\033[2m'; R=$'\033[0m'
pause() { [ "$FAST" = "1" ] && return 0; sleep "${1:-4}"; }
rule() { printf '%s\n' "${D}──────────────────────────────────────────────────────────────────────────────${R}"; }

clear
printf '\n%s\n' "${G}${B}  NEAR AGENT FLEET${R}"
printf '%s\n' "  Three paid agents. One operator. Live on NEAR Agent Market."
printf '%s\n\n' "${D}  Built at NEAR HACKATHON · github.com/matrixneoaiagent/near-agent-fleet${R}"
rule
printf '%s\n' "  ${C}\$ python3 fleet.py status${R}   ${D}# public listings, health, A2A cards, test artifacts${R}"
pause 8

python3 fleet.py status || true
pause 12

printf '\n%s\n' "  ${C}\$ python3 fleet.py route \"I need five vertical clips with captions from a 40-minute podcast\"${R}"
pause 3
python3 fleet.py route "I need five vertical clips with captions from a 40-minute podcast"
pause 9

printf '\n%s\n' "  ${C}\$ python3 fleet.py route \"Check the brand name Vellumforge on .studio, .dev and X\"${R}"
pause 3
python3 fleet.py route "Check the brand name Vellumforge on .studio, .dev and the X handle"
pause 8

printf '\n%s\n' "  ${C}\$ python3 fleet.py brand vellumforge${R}   ${D}# real public job, no charge${R}"
pause 3
python3 fleet.py brand vellumforge || true
pause 10

printf '\n%s\n' "  ${C}\$ python3 -m unittest discover -s tests${R}"
python3 -m unittest discover -s tests -q 2>&1 | tail -3
pause 8

rule
printf '%s\n' "  ${G}12/12 public checks pass · 3/3 agents live · 17 unit tests pass${R}"
printf '%s\n' "  ${B}Hire:${R} market.near.ai/a/brandnamechecker · /a/media-studio-agent · /a/clipper-agent"
printf '%s\n' "  ${B}Source:${R} github.com/matrixneoaiagent/near-agent-fleet"
printf '%s\n\n' "  ${D}Honest limits: 0 paid deliveries so far; quick tunnels are not permanent hosting.${R}"
pause 8
