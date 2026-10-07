#!/usr/bin/env bash
# Full-screen title card for the demo video:  step.sh <n> <total> "<title>" "<description>"
clear
cols=$(tput cols 2>/dev/null || echo 70); rows=$(tput lines 2>/dev/null || echo 20)
pad=$(( rows / 2 - 4 )); for _ in $(seq 1 $pad); do echo; done
center() { local s="$1"; local w=${#s}; printf "%*s%s\n" $(( (cols - w) / 2 )) "" "$s"; }
if [ "$1" = "0" ]; then
  printf '\033[1;38;5;121m'; center "$3"; printf '\033[0m\n'
  printf '\033[38;5;250m'; center "$4"; printf '\033[0m'
else
  printf '\033[38;5;121m'; center "STEP $1 OF $2"; printf '\033[0m\n'
  printf '\033[1;97m'; center "$3"; printf '\033[0m\n'
  printf '\033[38;5;250m'
  echo "$4" | fold -s -w $(( cols - 10 )) | while IFS= read -r line; do center "$line"; done
  printf '\033[0m'
fi
