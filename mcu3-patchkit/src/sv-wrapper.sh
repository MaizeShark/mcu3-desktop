#!/bin/sh
# /sbin/sv in the chroot (mcu3-patchkit). On the car runit supervises the services and the
# escalator starts some of them with "sv start <name>" (fireplace, dog mode, HAL 9000 videos).
# There is no runsvdir in the chroot, so for those services this runs qtcar-service <name>
# in the background instead; everything else goes to runit's sv (/sbin/sv.runit).
#   sv start|up|u|stop|down|d|status <service>...

RUNDIR=/run/qtcar-sv
handled() {
    case "$1" in fireplace|dog-mode|hal-9000) return 0 ;; esac
    return 1
}

cmd=$1
[ $# -ge 2 ] || exec /sbin/sv.runit "$@"
shift
for s in "$@"; do handled "${s##*/}" || exec /sbin/sv.runit "$cmd" "$@"; done

mkdir -p "$RUNDIR"
rc=0
for s in "$@"; do
    s=${s##*/}
    pidfile=$RUNDIR/$s.pid
    pid=$(cat "$pidfile" 2>/dev/null)
    running=0
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && running=1
    case "$cmd" in
        start|up|u|o|once)
            if [ "$running" = 0 ]; then
                setsid /usr/local/bin/qtcar-service "$s" </dev/null >>"$RUNDIR/$s.log" 2>&1 &
                echo $! >"$pidfile"
            fi
            echo "ok: run: $s" ;;
        stop|down|d|exit|x|kill|k)
            if [ "$running" = 1 ]; then
                kill "$pid" 2>/dev/null
                sleep 1
                kill -9 "$pid" 2>/dev/null
            fi
            rm -f "$pidfile"
            echo "ok: down: $s" ;;
        status|s)
            if [ "$running" = 1 ]; then echo "run: $s: (pid $pid)"; else echo "down: $s"; rc=3; fi ;;
        *)
            echo "sv (chroot wrapper): '$cmd' not supported for $s" >&2; rc=1 ;;
    esac
done
exit $rc
