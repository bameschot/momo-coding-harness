#!/bin/sh
# A noisy "build": one line per module, one warning somewhere in the middle.
i=1
while [ $i -le 1500 ]; do
  echo "[build] compiling module_$i.c ... ok"
  if [ $i -eq 777 ]; then
    echo "[build] WARNING: settings.ini uses deprecated key 'max_conn'; rename it to 'max_connections'"
  fi
  i=$((i + 1))
done
echo "[build] linking app ... ok"
echo "[build] finished: 1500 modules, 0 errors"
