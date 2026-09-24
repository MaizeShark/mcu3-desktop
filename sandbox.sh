#!/bin/bash
# Run QtCar and firmware services in the sudo-free podman sandbox (sandbox/rootfs, see CLAUDE.md),
# on Xvfb :97. For testing without the real chroot.
#
#   ./sandbox.sh [options] [SERVICE...]        e.g. ./sandbox.sh -t 90 -s "60 85" --touch qtcar-sim qtcar-vehicle
#
#   -t SECONDS   how long to run (default 60)
#   -s "T ..."   screenshots after T seconds -> sandbox/out/shot_T.png
#   --touch      tesla-touch on :97 (user in group input / uinput ACL), passed into the container;
#                then e.g. DISPLAY=:97 xdotool mousemove 858 203 click 1 taps the screen
#   --plain      run QtCar directly instead of under gdb
#   --browser    under gdb, don't skip the browser (CEF) either
#   -x FILE      extra gdb commands (copied into the sandbox)
#   --audio      pass /dev/snd in (the "model3" snd-aloop card, see start_all.sh AUDIO=1)
#   --video      pass /dev/video* in (backup camera, [bkcam] in settings.conf)
#   --music DIR  DIR as a USB stick (/home/tesla/media/usb-music, read-only)
#
# QtCar runs under the host's gdb (host / mounted at /host, run with the host's loader) with
# WebKitView::createBrowserClient skipped: without /usr/bin/escalator (root-only, not in the
# sandbox) CEF kills QtCar after ~20 s. A crash leaves a backtrace in qtcar.log. QtCar and the
# services are restarted when they exit. Logs: sandbox/out/. QtCar reads /root/.Tesla here.
set -u
cd "$(dirname "$(realpath "$0")")"
ROOT=$PWD/sandbox/rootfs
OUT=$PWD/sandbox/out
T=60 SHOTS="" TOUCH=0 GDB=1 BROWSER=0 GDBX="" AUDIODEV=0 VIDEODEV=0 MUSIC=""
while [ $# -gt 0 ]; do
    case "$1" in
        -t) T=$2; shift 2 ;;
        -s) SHOTS=$2; shift 2 ;;
        --touch) TOUCH=1; shift ;;
        --plain) GDB=0; shift ;;
        --browser) BROWSER=1; shift ;;
        -x) GDBX=$2; shift 2 ;;
        --audio) AUDIODEV=1; shift ;;
        --video) VIDEODEV=1; shift ;;
        --music) MUSIC=$(realpath "$2"); shift 2 ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) break ;;
    esac
done
[ -d "$ROOT/usr/tesla" ] || { echo "no sandbox rootfs at $ROOT"; exit 1; }
mkdir -p "$OUT"
rm -f "$OUT"/*.log "$OUT"/shot_*.png

cat > "$ROOT/sandbox-qtcar.sh" <<'EOF'
#!/bin/sh
# written by sandbox.sh: QtCar as container root, optionally under the host's gdb
cd /usr/tesla/UI/bin
export PATH=/bin:/usr/bin:/sbin:/usr/sbin:/usr/local/bin DISPLAY=:97 \
    LD_LIBRARY_PATH=/usr/tesla/UI/lib:/usr/cid-lib:/usr/lib64:/lib64:/lib:/usr/lib \
    LC_ALL=C LANG=C LANGUAGE=C QT_X11_NO_MITSHM=1 LIBGL_ALWAYS_SOFTWARE=1 \
    EGL_PIXMAP_SHIM_DEBUG=1 CEF_SHIM_DEBUG=1 CEF_LOG_SEVERITY=${CEF_LOG_SEVERITY:-info} CEF_EXTRA_ARGS=$CEF_EXTRA_ARGS CEF_REMOTE_DEBUGGING_PORT=$CEF_REMOTE_DEBUGGING_PORT
PRELOAD=/usr/lib/icu_preload.so
for so in egl_pixmap_shim cef_nosandbox sandbox_escalator_stub; do
    [ -f /usr/lib/$so.so ] && PRELOAD=$PRELOAD:/usr/lib/$so.so
done
[ "$1" = gdb ] || LD_PRELOAD=$PRELOAD exec ./QtCar --touch evdev,autorange
H=/host
unset LD_PRELOAD    # only for QtCar, not for the host's gdb
exec $H/lib64/ld-linux-x86-64.so.2 --library-path $H/lib/x86_64-linux-gnu:$H/usr/lib/x86_64-linux-gnu \
    $H/usr/bin/gdb -nx -batch -ex 'set startup-with-shell off' -ex 'set pagination off' -ex 'set auto-load off' \
    -ex 'handle SIGPIPE nostop noprint pass' -ex 'handle SIGUSR1 nostop noprint pass' \
    -ex 'handle SIG32 nostop noprint pass' -ex 'handle SIG33 nostop noprint pass' \
    -ex "set environment LD_PRELOAD=$PRELOAD" -ex 'set follow-fork-mode parent' -ex 'set detach-on-fork on' \
    ${NOBROWSER:+-x /sandbox-nobrowser.gdb} ${GDBX:+-x /sandbox-extra.gdb} -ex run -ex 'bt 30' --args ./QtCar --touch evdev,autorange
EOF
chmod +x "$ROOT/sandbox-qtcar.sh"
cat > "$ROOT/sandbox-nobrowser.gdb" <<'EOF'
break WebKitView::createBrowserClient
commands
silent
printf "gdb: skipping WebKitView::createBrowserClient\n"
return
continue
end
EOF

# sandbox only: no /usr/bin/escalator and no cgroups here. QtCar's ChromiumManager::initializeCef
# dies when Escalator::runCommand() (cgroup setup) can't reach it, and tesla-cef-launcher insists
# on being in the net_cls:/nonet cgroup. Pretend both worked.
cat > "$OUT/stub.c" <<'EOF'
#define _GNU_SOURCE
#include <stdio.h>
#include <string.h>
// the real fopen under its other name: no dlsym, which on the firmware's glibc lives in libdl and
// tesla-cef-launcher doesn't load that
FILE *_IO_fopen(const char *path, const char *mode);
int _ZN9Escalator10runCommandEv(void *self) { return 0; }
FILE *fopen64(const char *path, const char *mode)
{
    if (path && strcmp(path, "/proc/self/cgroup") == 0) {
        FILE *f = _IO_fopen("/tmp/sandbox-cgroup", "w");
        if (f) {
            fputs("4:net_cls:/nonet\n", f);
            fclose(f);
            return _IO_fopen("/tmp/sandbox-cgroup", mode);
        }
    }
    return _IO_fopen(path, mode);
}
// crash backtraces from CEF's child processes (no core dumps, no Chromium handler with
// --disable-in-process-stack-traces): frames + load address of libcef.so to stderr
#define _GNU_SOURCE
#include <execinfo.h>
#include <link.h>
#include <signal.h>
#include <unistd.h>
static int print_base(struct dl_phdr_info *info, size_t size, void *data)
{
    if (strstr(info->dlpi_name, "libcef.so")) {
        char buf[128];
        int n = snprintf(buf, sizeof buf, "sandbox-segv: libcef.so base 0x%lx\n", (unsigned long)info->dlpi_addr);
        write(2, buf, n);
    }
    return 0;
}
static void on_segv(int sig, siginfo_t *si, void *uc)
{
    char buf[128];
    int n = snprintf(buf, sizeof buf, "sandbox-segv: pid %d signal %d addr %p\n", getpid(), sig, si->si_addr);
    write(2, buf, n);
    dl_iterate_phdr(print_base, NULL);
    void *frames[64];
    int k = backtrace(frames, 64);
    backtrace_symbols_fd(frames, k, 2);
    signal(sig, SIG_DFL);
    raise(sig);
}
__attribute__((constructor)) static void init(void)
{
    char exe[256] = {0};
    readlink("/proc/self/exe", exe, sizeof exe - 1);
    if (!strstr(exe, "tesla-cef"))
        return;
    struct sigaction sa = {0};
    sa.sa_sigaction = on_segv;
    sa.sa_flags = SA_SIGINFO;
    sigaction(SIGSEGV, &sa, NULL);
}
EOF
if ! cmp -s "$OUT/stub.c" "$ROOT/usr/lib/sandbox_escalator_stub.c"; then
    gcc -shared -fPIC -O2 -o "$ROOT/usr/lib/sandbox_escalator_stub.so" "$OUT/stub.c" &&
        cp "$OUT/stub.c" "$ROOT/usr/lib/sandbox_escalator_stub.c"
fi

xdpyinfo -display :97 >/dev/null 2>&1 || { Xvfb :97 -screen 0 1920x1200x24 -nolisten tcp >/dev/null 2>&1 & sleep 1; }

DEV=()
TPID=""
if [ "$TOUCH" = 1 ]; then
    tesla-touch/target/release/tesla-touch --display :97 --no-cursor >"$OUT/touch.log" 2>&1 &
    TPID=$!
    for _ in $(seq 50); do grep -q '^ready' "$OUT/touch.log" && break; sleep 0.1; done
    EV=$(grep -o '/dev/input/event[0-9]*' "$OUT/touch.log" | head -1)
    [ -n "$EV" ] && DEV=(--device "$EV:/dev/input/touch") || echo "tesla-touch didn't start, see $OUT/touch.log"
fi

[ "$AUDIODEV" = 1 ] && DEV+=(--device /dev/snd)
if [ "$VIDEODEV" = 1 ]; then for v in /dev/video*; do DEV+=(--device "$v"); done; fi
[ -n "$MUSIC" ] && DEV+=(-v "$MUSIC:/home/tesla/media/usb-music:ro")
SVC=""
for s in "$@"; do    # a SERVICE can have options: "qtcar-sim --udp :1235"
    SVC="$SVC (while :; do /usr/local/bin/qtcar-service $s; echo sandbox: $s exited; sleep 2; done) >/out/${s%% *}.log 2>&1 &"
done
MODE=plain; [ "$GDB" = 1 ] && MODE=gdb
NOBROWSER=1; [ "$BROWSER" = 1 ] && NOBROWSER=
[ -n "$GDBX" ] && cp "$GDBX" "$ROOT/sandbox-extra.gdb"
for t in $SHOTS; do ( sleep "$t"; DISPLAY=:97 import -window root "$OUT/shot_$t.png" ) & done

podman run --rm --timeout $((T + 20)) --network host -e QCSVC_NOUSER=1 -e CEF_EXTRA_ARGS="${CEF_EXTRA_ARGS:-}" -e CEF_REMOTE_DEBUGGING_PORT="${CEF_REMOTE_DEBUGGING_PORT:-}" -v /:/host:ro "${DEV[@]}" \
    -v /tmp/.X11-unix:/tmp/.X11-unix -v "$OUT":/out --rootfs "$ROOT" /bin/sh -c "
    $SVC
    sleep 3
    (while :; do NOBROWSER=$NOBROWSER GDBX=$GDBX /sandbox-qtcar.sh $MODE; echo '=== sandbox: QtCar exited, restarting'; sleep 2; done) >/out/qtcar.log 2>&1 &
    sleep $T"
[ -n "$TPID" ] && kill "$TPID"
wait
echo "logs and screenshots: $OUT"
