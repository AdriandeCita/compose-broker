#!/bin/sh
# Emits one line per TICK_INTERVAL seconds on stdout, and a warning on stderr
# every WARN_EVERY ticks, so both log streams have content.
interval="${TICK_INTERVAL:-1}"
warn_every="${WARN_EVERY:-5}"
echo "ticker starting (interval=${interval}s)"
i=0
while true; do
  i=$((i + 1))
  echo "tick $i"
  if [ $((i % warn_every)) -eq 0 ]; then
    echo "warn: tick $i is a multiple of $warn_every" >&2
  fi
  sleep "$interval"
done
