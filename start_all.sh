#!/bin/bash
# The old name of `./tesla start`, kept so habits and scripts keep working. Environment variables
# (AUDIO=1 VEHICLE=1 GPS=... ./start_all.sh) still work; see ./tesla start --help.
exec "$(dirname "$(realpath "$0")")/tesla" start "$@"
