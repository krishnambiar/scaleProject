#!/bin/zsh
# Finder-friendly launcher: keep the local sensor server alive in this window.
cd -- "$(dirname -- "$0")" || exit 1
make gui
pebble_status=$?
if [ "$pebble_status" -ne 0 ] && [ "$pebble_status" -ne 130 ]; then
  printf '\nPebble could not start. See the details above. Press Return to close.\n'
  read -r
fi
exit "$pebble_status"
