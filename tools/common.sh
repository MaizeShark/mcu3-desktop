# Shared by the ./tesla commands (sourced; the working directory is the repo).

TESLA_DIR=$PWD
LOGS=$TESLA_DIR/logs
DISPLAY_NUM=:1
TOUCH_BIN=$TESLA_DIR/tesla-touch/target/release/tesla-touch
KIT=$TESLA_DIR/mcu2-patchkit/mcu2_patch.py
# ports the firmware services listen on (TCP data value servers, UDP CAN/GPS/control)
SERVICE_TCP_PORTS="4220 4160 4030 4190 4060 8002"
SERVICE_UDP_PORTS="1234 1235 4321 20100 63277"

if [ -t 1 ]; then
    B=$'\e[1m' DIM=$'\e[2m' RED=$'\e[31m' GRN=$'\e[32m' YEL=$'\e[33m' CYN=$'\e[36m' R0=$'\e[0m'
else
    B="" DIM="" RED="" GRN="" YEL="" CYN="" R0=""
fi
say()  { printf '%s\n' "$*"; }
step() { printf '%s==>%s %s\n' "$B$CYN" "$R0$B" "$*$R0"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '    %sWARNING:%s %s\n' "$YEL" "$R0" "$*"; }
die()  { printf '%sError:%s %s\n' "$RED" "$R0" "$*" >&2; exit 1; }

# --- Configuration ---------------------------------------------------------------------------
# defaults < tesla.conf < environment < command line. CONF_SRC[var] says where a value came from.
CONF_VARS=(VEHICLE VEHICLE_COLOR VEHICLE_WHEELS VEHICLE_PERFORMANCE GPS AUDIO AUDIO_REMIX MUSIC
           CAMERA CAMERA_DEV CAMERA_SIZE NAV SERVICES SIZE VIEWER REMOTE VNC_PORT PANEL_PORT RESTART
           IMAGE CHROOT SCREEN NATIVE_DISPLAY TOUCH_DEVICE QTCAR_ARGS GPU BLUETOOTH BT_ADAPTER)
PATH_VARS=" MUSIC IMAGE CHROOT "    # relative paths: to the caller's directory (env, command line)
declare -A CONF_SRC

set_defaults() {
    VEHICLE=0 VEHICLE_COLOR="" VEHICLE_WHEELS="" VEHICLE_PERFORMANCE="" GPS=""
    AUDIO=0 AUDIO_REMIX="1v0.5,4v0.5,5v0.5,7v0.5 1v0.5,2v0.5,6v0.5,8v0.5"
    MUSIC="" CAMERA="" CAMERA_DEV=/dev/video32 CAMERA_SIZE=1280x960 NAV=1 SERVICES=""
    SIZE=1920x1200 VIEWER="" REMOTE=0 VNC_PORT=5900 PANEL_PORT=8099 RESTART=1
    IMAGE=./mcu2.ext4 CHROOT=./chroot
    SCREEN=vnc NATIVE_DISPLAY=:0 TOUCH_DEVICE=auto QTCAR_ARGS="" GPU=auto BLUETOOTH=0 BT_ADAPTER=hci0
}

# abspath <path> <base dir>: ~ and relative paths resolved against the base dir
abspath() {
    local p=$1
    case "$p" in "~") p=$HOME ;; "~/"*) p=$HOME/${p#"~/"} ;; esac
    case "$p" in ""|/*) ;; *) p=$2/$p ;; esac
    [ -n "$p" ] && p=$(realpath -m "$p")
    printf '%s' "$p"
}

# load_config [FILE]: CONF_FILE is tesla.conf unless given; created from the example if missing
load_config() {
    local v line
    local -A env_val
    for v in "${CONF_VARS[@]}"; do
        [ -n "${!v+x}" ] && env_val[$v]=${!v}
    done
    set_defaults
    for v in "${CONF_VARS[@]}"; do CONF_SRC[$v]=default; done
    CONF_FILE=${1:-$TESLA_DIR/tesla.conf}
    if [ "${CREATE_CONF:-0}" = 1 ] && [ ! -e "$CONF_FILE" ] && [ "$CONF_FILE" = "$TESLA_DIR/tesla.conf" ] &&
        [ -f tesla.conf.example ]; then
        cp tesla.conf.example "$CONF_FILE" && CONF_CREATED=1
    fi
    if [ -f "$CONF_FILE" ]; then
        local -A before
        for v in "${CONF_VARS[@]}"; do before[$v]=${!v}; done
        # shellcheck disable=SC1090
        . "$CONF_FILE" || die "error in $CONF_FILE"
        for v in "${CONF_VARS[@]}"; do [ "${!v}" != "${before[$v]}" ] && CONF_SRC[$v]=tesla.conf; done
        # typos: assignments to names nobody reads
        CONF_UNKNOWN=""
        while read -r line; do
            case " ${CONF_VARS[*]} " in *" $line "*) ;; *) CONF_UNKNOWN="$CONF_UNKNOWN $line" ;; esac
        done < <(sed -n 's/^[[:space:]]*\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p' "$CONF_FILE" | sort -u)
    fi
    for v in "${!env_val[@]}"; do
        printf -v "$v" '%s' "${env_val[$v]}"
        CONF_SRC[$v]=environment
    done
    for v in IMAGE CHROOT; do
        case "${CONF_SRC[$v]}" in environment) ;; *) printf -v "$v" '%s' "$(abspath "${!v}" "$TESLA_DIR")" ;; esac
    done
    # until 2026-09-25 the default image was mcu3-new.ext4 (the unit was called MCU3 by mistake)
    if [ "${CONF_SRC[IMAGE]}" = default ] && [ ! -e "$IMAGE" ] && [ -e "$TESLA_DIR/mcu3-new.ext4" ]; then
        IMAGE=$TESLA_DIR/mcu3-new.ext4
    fi
    for v in $PATH_VARS; do
        [ "${CONF_SRC[$v]}" = environment ] && printf -v "$v" '%s' "$(abspath "${!v}" "$CALLER_PWD")"
    done
    [ "${CONF_SRC[MUSIC]}" = tesla.conf ] && MUSIC=$(abspath "$MUSIC" "$TESLA_DIR")
    return 0
}

# set_opt VAR VALUE: a command line option
set_opt() {
    local v=$1 val=$2
    case "$PATH_VARS" in *" $v "*) val=$(abspath "$val" "$CALLER_PWD") ;; esac
    printf -v "$v" '%s' "$val"
    CONF_SRC[$v]="command line"
}

# sudo_auth: make sure sudo works now. With NOPASSWD (e.g. a live system) `sudo -v` can still ask
# for a password (it checks all of the user's rules), so try without one first.
sudo_auth() {
    sudo -n true 2>/dev/null || sudo -v
}

# --- The running instance --------------------------------------------------------------------
# logs/current -> the log folder of the running `./tesla start`, which has:
#   state   START_PID, CHROOT, IMAGE, MOUNTED_IMAGE, ... (shell assignments)
#   pids    "pid name" of the host-side helpers it started
#   events.log   what happened when (start, QtCar restarts, stop)
# logs/latest -> the last run's folder, running or not.

# running_pid: the pid of a running `./tesla start`, or nothing
running_pid() {
    [ -f "$LOGS/current/state" ] || return 1
    local START_PID
    START_PID=$(sed -n 's/^START_PID=//p' "$LOGS/current/state")
    [ -n "$START_PID" ] && [ -d "/proc/$START_PID" ] &&
        tr '\0' ' ' <"/proc/$START_PID/cmdline" 2>/dev/null | grep -q 'tesla\|start_all' || return 1
    echo "$START_PID"
}

# chroot_pids: pids of processes whose root is $CHROOT (needs sudo: they belong to other users)
chroot_pids() {
    sudo sh -c 'for p in /proc/[0-9]*; do [ "$(readlink "$p/root" 2>/dev/null)" = "$1" ] && echo "${p#/proc/}"; done' _ "$CHROOT"
}

# stop_chroot_procs [quiet]: SIGTERM everything whose root is the chroot (QtCar, services,
# escalator, tesla-cef, ...), SIGKILL whatever is still there after 3 s. Some services hang on
# SIGTERM (a QtCarGpsManager once survived for hours, kept ports 4160/63277 and the next runs'
# GpsManager couldn't start). Prints the names of what it stopped unless quiet.
stop_chroot_procs() {
    sudo sh -c '
        list() { for p in /proc/[0-9]*; do [ "$(readlink "$p/root" 2>/dev/null)" = "$1" ] && echo "${p#/proc/}"; done; }
        pids=$(list "$1"); [ -n "$pids" ] || exit 0
        [ -n "$2" ] || for p in $pids; do tr "\0" " " <"/proc/$p/cmdline" 2>/dev/null | cut -c1-70; echo; done |
            sort -u | grep . | sed "s/^/      /"
        kill $pids 2>/dev/null
        for _ in 1 2 3 4 5 6; do sleep 0.5; [ -n "$(list "$1")" ] || exit 0; done
        pids=$(list "$1"); echo "      still running after SIGTERM, killed: $pids"; kill -9 $pids 2>/dev/null' _ "$CHROOT" "${1:-}"
}

# mounts_below: mount points inside $CHROOT, deepest first
mounts_below() {
    awk -v r="$CHROOT/" 'index($2, r) == 1 {print $2}' /proc/mounts | sed 's/\\040/ /g' | sort -r
}

# image_mounted: is something mounted at $CHROOT (and prints the source)
image_mounted() {
    awk -v r="$CHROOT" '$2 == r {print $1; f=1} END {exit !f}' /proc/mounts
}

# image_mounted_elsewhere: mount points of $IMAGE other than $CHROOT. Mounting an ext4 image a
# second time (another loop device) would corrupt it.
image_mounted_elsewhere() {
    local dev
    for dev in $(losetup -j "$IMAGE" -O NAME -n 2>/dev/null); do
        awk -v d="$dev" -v r="$CHROOT" '$1 == d && $2 != r {print $2}' /proc/mounts
    done
}

# unmount_image: unmount $CHROOT; if it's busy, show who holds it
unmount_image() {
    sudo umount "$CHROOT" 2>/dev/null && return 0
    sleep 1
    sudo umount "$CHROOT" 2>/dev/null && return 0
    warn "couldn't unmount $CHROOT, it's busy. Held by:"
    sudo fuser -vm "$CHROOT" 2>&1 | sed 's/^/      /'
    info "Stop those, then: sudo umount $CHROOT"
    return 1
}

# proc_start PID: the process's start time (clock ticks since boot), to tell a reused pid apart
proc_start() {
    local s
    s=$(cat "/proc/$1/stat" 2>/dev/null) || return 1
    s=${s##*) }
    set -- $s
    echo "${20}"
}

# helper_pids FILE: pids from a pids file ("pid starttime name") that still run
helper_pids() {
    local pid start name
    [ -f "$1" ] || return 0
    while read -r pid start name; do
        [ "$(proc_start "$pid")" = "$start" ] && echo "$pid"
    done <"$1"
}

# chroot_procs_maybe: are firmware processes running (without sudo: by name; may be the sandbox)
chroot_procs_maybe() {
    pgrep -f '(^|/)(QtCar[A-Za-z0-9]*|valhalla_server|AWE_command_line_tesla|audiod|escalator|tesla-cef[a-z-]*)( |$)' >/dev/null
}

# teardown STATE_DIR [quiet]: stop everything a run left (processes, mounts). Used by the trap of
# `./tesla start` and by `./tesla stop` when that instance is gone.
teardown() {
    local dir=$1 quiet=${2:-} m pids
    local MOUNTED_IMAGE=0
    [ -f "$dir/state" ] && MOUNTED_IMAGE=$(sed -n 's/^MOUNTED_IMAGE=//p' "$dir/state")
    stop_chroot_procs "$quiet"
    pids=$(helper_pids "$dir/pids")
    if [ -n "$pids" ]; then
        [ -n "$quiet" ] || info "helpers: $(for p in $pids; do ps -o comm= -p "$p"; done | sort -u | tr '\n' ' ')"
        # root helpers (tesla-touch) need sudo; kill both ways
        kill $pids 2>/dev/null; sudo kill $pids 2>/dev/null
    fi
    # native screen: the touchscreen's access for QtCar's user, the screensaver
    local touch_acl native_display
    touch_acl=$(sed -n 's/^\(TOUCH_\)\?ACL=//p' "$dir/state" 2>/dev/null)
    native_display=$(sed -n 's/^NATIVE=//p' "$dir/state" 2>/dev/null)
    for f in $touch_acl; do
        sudo setfacl -x u:1111 "$f" 2>/dev/null
        case "$f" in /dev/dri/*) sudo setfacl -x u:1226 "$f"; sudo setfacl -x u:1980 "$f" ;; esac 2>/dev/null
    done
    if [ -n "$native_display" ]; then
        sudo rm -f /run/tesla-native
        DISPLAY=$native_display xset s default +dpms 2>/dev/null
        local restore
        restore=$(sed -n 's/^NATIVE_RESTORE=//p' "$dir/state" 2>/dev/null)
        [ -n "$restore" ] && DISPLAY=$native_display xrandr $restore 2>/dev/null
    fi
    # audio clock (low tick kernels): back to jiffies; Bluetooth: give the adapter back to BlueZ
    local tcard
    tcard=$(sed -n 's/^TIMER_CARD=//p' "$dir/state" 2>/dev/null)
    [ -n "$tcard" ] && echo "" | sudo tee "/proc/asound/card$tcard/timer_source" >/dev/null 2>&1
    sudo pkill -f "aplay -q -D hw:CARD=hrcloc[k]" 2>/dev/null
    local bt
    bt=$(sed -n 's/^BT_RESTORE=//p' "$dir/state" 2>/dev/null)
    [ -n "$bt" ] && restore_bluetooth "$bt" "$(sed -n 's/^BT_USBPORT=//p' "$dir/state" 2>/dev/null)"
    # children of the helper loops (their loop may be gone already). The [x] keeps the pattern
    # from matching the command line of pkill's own sudo.
    pkill -f 'arecord -q -D hw:CARD=model[3]' 2>/dev/null
    pkill -f 'ffmpeg -nostdin -loglevel warning -f v4l[2]' 2>/dev/null
    sudo pkill -f -- "--bind $CHROOT/dev/input/touc[h]" 2>/dev/null
    sleep 1
    # unmount everything below the chroot, deepest first
    for m in $(mounts_below); do
        [ -n "$quiet" ] || info "unmount $m"
        sudo umount "$m" 2>/dev/null || sudo umount -l "$m" 2>/dev/null
    done
    if [ "$MOUNTED_IMAGE" = 1 ] && image_mounted >/dev/null; then
        [ -n "$quiet" ] || info "unmount the image from $CHROOT"
        unmount_image
    fi
    rm -f "$LOGS/current"
}

# image_file PATH: read a (small) file from the image, mounted or not
image_file() {
    if image_mounted >/dev/null; then
        cat "$CHROOT/$1" 2>/dev/null
    elif [ -r "$IMAGE" ]; then
        # debugfs prints nothing (and still exits 0) for a missing file
        PATH=$PATH:/sbin debugfs -R "cat /$1" "$IMAGE" 2>/dev/null | grep . || return 1
    fi
}

# the kit's version stamp in the image (etc/mcu3-patchkit from kits before the rename)
kit_stamp() { image_file etc/mcu2-patchkit || image_file etc/mcu3-patchkit; }

# native_output: "OUTPUT<TAB>WxH" of the first connected output of $NATIVE_DISPLAY with a mode.
# The size is the panel's own mode, not a scaled framebuffer.
native_output() {
    DISPLAY=$NATIVE_DISPLAY xrandr --current 2>/dev/null |
        awk '/ connected/ {out=$1} out && /\*/ {print out "\t" $1; exit}'
}

# find_touchscreen: "/dev/input/eventN<TAB>name" of the first touchscreen: a multitouch device
# (ABS_MT_POSITION_X, bit 53) with INPUT_PROP_DIRECT (it's the screen; a touchpad is "pointer")
find_touchscreen() {
    python3 - <<'PY'
import re
for block in open("/proc/bus/input/devices").read().split("\n\n"):
    name = re.search(r'N: Name="(.*)"', block)
    ev = re.search(r"H: Handlers=.*\b(event\d+)", block)
    prop = re.search(r"B: PROP=(\w+)", block)
    absb = re.search(r"B: ABS=([\w ]+)", block)
    if not (name and ev and prop and absb):
        continue
    words = absb.group(1).split()
    if int(prop.group(1), 16) & 2 and int(words[-1], 16) >> 53 & 1:
        print("/dev/input/%s\t%s" % (ev.group(1), name.group(1)))
        break
PY
}

# restore_bluetooth hciN: give the adapter back to BlueZ after --bluetooth. The bridge took it down;
# after the Broadcom stack an Intel AX210 didn't answer anymore ("Connection timed out") until its
# driver was reloaded (which loads its firmware again).
restore_bluetooth() {
    local bt=$1 usbport=${2:-} drv i
    sudo pkill -f "bluetooth/hci-bridge.p[y]" 2>/dev/null
    for i in $(seq 20); do pgrep -f "bluetooth/hci-bridge.p[y]" >/dev/null || break; sleep 0.2; done
    sudo systemctl start bluetooth 2>/dev/null
    for i in $(seq 6); do hciconfig "$bt" 2>/dev/null | grep -q "UP RUNNING" && break; sudo hciconfig "$bt" up 2>/dev/null; sleep 0.5; done
    if ! hciconfig "$bt" 2>/dev/null | grep -q "UP RUNNING"; then
        drv=$(sed -n 's/^DRIVER=//p' "/sys/class/bluetooth/$bt/device/uevent" 2>/dev/null)
        if [ -n "$drv" ]; then
            sudo modprobe -r "$drv" 2>/dev/null; sleep 1; sudo modprobe "$drv" 2>/dev/null
            for i in $(seq 12); do hciconfig "$bt" 2>/dev/null | grep -q "UP RUNNING" && break; sleep 0.5; done
        fi
    fi
    # still gone (the chip dropped off USB): power-cycle its USB port, then the hub's empty internal
    # ports too (on a ThinkPad T480 the AX210 came back only after cycling port 6, it sits on 7)
    if ! hciconfig "$bt" 2>/dev/null | grep -q "UP RUNNING" && [ -n "$usbport" ] && [ -e "$usbport/disable" ]; then
        local ports=$usbport p
        for p in "$(dirname "$usbport")"/*-port*; do
            [ "$(cat "$p/connect_type" 2>/dev/null)" = "not used" ] && [ -z "$(ls "$p/device" 2>/dev/null)" ] &&
                ports="$ports $p"
        done
        for p in $ports; do echo 1 | sudo tee "$p/disable" >/dev/null; done
        sleep 3
        for p in $ports; do echo 0 | sudo tee "$p/disable" >/dev/null; done
        for i in $(seq 20); do hciconfig "$bt" 2>/dev/null | grep -q "UP RUNNING" && break; sleep 0.5; done
    fi
    bluetoothctl power on >/dev/null 2>&1
    if hciconfig "$bt" 2>/dev/null | grep -q "UP RUNNING"; then
        info "bluetooth: $bt is back with BlueZ"
    else
        warn "bluetooth: $bt didn't come back; try: sudo modprobe -r btusb && sudo modprobe btusb"
    fi
}
