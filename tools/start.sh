# ./tesla start (sourced by ./tesla): QtCar in the chroot on a virtual X display, with VNC, touch,
# the firmware services and the optional features.

start_usage() {
    cat <<EOF
${B}./tesla start$R0 [options]     start everything; Ctrl+C or ./tesla stop stops it

Features (defaults from tesla.conf):
  --vehicle              fake vehicle data (simulator + tesla-can.py), web panel on :$PANEL_PORT
  --color C              car color: RedMulticoat SolidBlack SilverMetallic MidnightSilver
                         DeepBlue PearlWhite (implies --vehicle)
  --wheels W             Pinwheel18 Stiletto19 Stiletto20 Stiletto20Staggered Gemini19Square
                         Gemini19Staggered (implies --vehicle)
  --performance P        Base Performance Ludicrous BasePlus (implies --vehicle)
  --gps LAT,LON          fake GPS at a fixed position
  --gps "OPTIONS"        tesla-gps.py options: "--pos LAT,LON --to LAT,LON [--speedup 2]" drives a
                         route, "--uart /dev/ttyACM0 [--baud 9600]" passes a real receiver through
  --audio                sound: the car's amplifier -> this PC's speakers
  --bluetooth            the car's Bluetooth (Broadcom stack) on this PC's adapter: phone, contacts,
                         music with --audio (BT_ADAPTER=hci0; BlueZ is stopped while it runs)
  --music DIR            DIR as a USB stick with music (Media -> USB)
  --camera DEV           backup camera from a V4L2 device (e.g. scrcpy --v4l2-sink=/dev/video10)
  --no-nav               no offline navigation (on when maps are installed)
  --services "A B"       more firmware services (./tesla services lists them)
  --no-vehicle, --no-gps, --no-audio, --no-music, --no-camera   switch off what tesla.conf enables

Screen and access:
  --native               QtCar on this PC's own screen instead of a virtual one over VNC:
                         fullscreen, scaled to fit with black bars, the touchscreen passed through
                         (without one the mouse is a finger). Run it in the desktop session.
  --vnc                  the virtual screen over VNC (default)
  --touch-device DEV     native: the touchscreen (default: found automatically)
  --no-gpu, --gpu        native: OpenGL in software instead of on the GPU (default: the GPU when
                         it's an Intel one; --gpu also tries an AMD one, untested)
  --size WxH             the UI's size (default 1920x1200, the car's screen)
  --viewer CMD|none      VNC viewer to open (default: krdc, remote-viewer, remmina or vncviewer)
  --remote               VNC (password: x11vnc -storepasswd) and panel reachable from the network
  --no-restart           don't restart QtCar when it exits

Other:
  --config FILE          settings file instead of tesla.conf
  --qtcar-args "..."     more QtCar options (e.g. --prod)
  --verbose, -v          QtCar's output here too (it's always in logs/latest/qtcar.log)
  --strace "SVC ..."     run these services under strace (-f -tt); the traces end up in the
                         log folder as strace-SVC.txt (debugging)
  --dry-run              only run the checks and show what would start
  --force                start even when a check fails

Examples:
  ./tesla start --vehicle --gps 37.4419,-122.1430
  ./tesla start --vehicle --gps "--pos 37.4419,-122.1430 --to 37.4275,-122.1697 --speedup 2"
  ./tesla start --audio --music ~/Music --camera /dev/video10 --color DeepBlue
EOF
}

parse_start_opts() {
    DRY_RUN=0 FORCE=0 VERBOSE=0 STRACE=""
    local opt val
    while [ $# -gt 0 ]; do
        opt=$1
        case "$opt" in --*=*) val=${opt#*=}; opt=${opt%%=*}; set -- "$opt" "$val" "${@:2}" ;; esac
        case "$opt" in
            --color|--wheels|--performance|--gps|--music|--camera|--services|--size|--viewer|--config|--strace|--touch-device|--qtcar-args)
                [ $# -ge 2 ] || die "$opt needs a value (./tesla start --help)" ;;
        esac
        case "$opt" in
            --vehicle) set_opt VEHICLE 1 ;;
            --no-vehicle) set_opt VEHICLE 0 ;;
            --color) set_opt VEHICLE_COLOR "$2"; set_opt VEHICLE 1; shift ;;
            --wheels) set_opt VEHICLE_WHEELS "$2"; set_opt VEHICLE 1; shift ;;
            --performance) set_opt VEHICLE_PERFORMANCE "$2"; set_opt VEHICLE 1; shift ;;
            --gps) set_opt GPS "$2"; shift ;;
            --no-gps) set_opt GPS "" ;;
            --audio) set_opt AUDIO 1 ;;
            --bluetooth) set_opt BLUETOOTH 1 ;;
            --no-bluetooth) set_opt BLUETOOTH 0 ;;
            --no-audio) set_opt AUDIO 0 ;;
            --music) set_opt MUSIC "$2"; shift ;;
            --no-music) set_opt MUSIC "" ;;
            --camera) set_opt CAMERA "$2"; shift ;;
            --no-camera) set_opt CAMERA "" ;;
            --nav) set_opt NAV 1 ;;
            --no-nav) set_opt NAV 0 ;;
            --services) set_opt SERVICES "$2"; shift ;;
            --size) set_opt SIZE "$2"; shift ;;
            --viewer) set_opt VIEWER "$2"; shift ;;
            --remote) set_opt REMOTE 1 ;;
            --native) set_opt SCREEN native ;;
            --gpu) set_opt GPU on ;;
            --no-gpu) set_opt GPU off ;;
            --vnc) set_opt SCREEN vnc ;;
            --touch-device) set_opt TOUCH_DEVICE "$2"; shift ;;
            --qtcar-args) set_opt QTCAR_ARGS "$2"; shift ;;
            --no-restart) set_opt RESTART 0 ;;
            --config) shift ;;      # read before the config (./tesla)
            --dry-run|-n) DRY_RUN=1 ;;
            --verbose|-v) VERBOSE=1 ;;
            --strace) STRACE=$2; shift ;;
            --force|-f) FORCE=1 ;;
            -h|--help) start_usage; exit 0 ;;
            *) die "unknown option: $opt (./tesla start --help)" ;;
        esac
        shift
    done
    VEHICLE=$(on "$VEHICLE") AUDIO=$(on "$AUDIO") BLUETOOTH=$(on "$BLUETOOTH") REMOTE=$(on "$REMOTE") RESTART=$(on "$RESTART") NAV=$(on "$NAV")
}

PIDS=()
# add_pid PID NAME: a host-side helper, stopped at the end (and by ./tesla stop if this is gone)
add_pid() {
    PIDS+=("$1")
    echo "$1 $(proc_start "$1") $2" >>"$LOG_DIR/pids"
}
event() {
    printf '%s %s %s\n' "$(date +%s)" "$(date +%T)" "$*" >>"$LOG_DIR/events.log"
}
add_service() {
    local s
    for s in "$@"; do case " $SERVICES " in *" $s "*) ;; *) SERVICES="${SERVICES:+$SERVICES }$s" ;; esac; done
}

cleanup() {
    trap - EXIT INT TERM HUP
    echo
    step "Stopping"
    event "stop"
    local svc
    [ -n "$STRACE" ] && stop_chroot_procs quiet     # strace has finished writing then
    for svc in $STRACE; do     # --strace: the traces from the chroot's /tmp
        sudo mv "$CHROOT/tmp/strace-$svc.txt" "$LOG_DIR/" 2>/dev/null && sudo chown "$(id -u):$(id -g)" "$LOG_DIR/strace-$svc.txt"
    done
    teardown "$LOG_DIR" quiet
    say "    Logs: ${LOG_DIR#"$TESLA_DIR/"}"
    echo
    python3 tools/logsummary.py "$LOG_DIR" --short 2>/dev/null | sed 's/^/    /'
}

# the car's look: the sim sends it in the car config (GTW_carConfig); QtCar keeps its own copy in
# its settings DB and only reads it at startup, so set both.
# car_opt <SIM/VAPI name> <value> <label0 label1 ...>   ("-" = an unused index)
CAR_SIM=()
car_opt() {
    local name=$1 value=$2 i=0 label; shift 2
    [ -n "$value" ] || return 0
    for label in "$@"; do
        if [ "$label" = "$value" ]; then
            CAR_SIM+=("SIM_$name=$label")
            sudo python3 vehicle/qtcar-settings.py "$CHROOT/home/tesla/.Tesla/data/QtCarSettings.db" "VAPI_$name=$i" >/dev/null
            return 0
        fi
        i=$((i + 1))
    done
}

# settings_conf SECTION KEY=VALUE...: merge keys into the car's settings.conf
settings_conf() {
    sudo python3 - "$CHROOT/home/tesla/.Tesla/car/settings.conf" "$@" <<'PY'
import configparser, sys
path, section, *kv = sys.argv[1:]
c = configparser.ConfigParser(interpolation=None)
c.optionxform = str
c.read(path)
if not c.has_section(section):
    c.add_section(section)
c[section].update(dict(x.split("=", 1) for x in kv))
with open(path, "w") as f:
    c.write(f, space_around_delimiters=False)
PY
}

print_summary() {
    local what=$1 viewer_note="" off=""
    [ -n "${VIEWER_USED:-}" ] && viewer_note=" (viewer: $VIEWER_USED)"
    if [ "$what" = Running ]; then step "$what$R0  ${DIM}(stop: Ctrl+C here, or ./tesla stop)$R0"; else step "$what"; fi
    if [ "$SCREEN" = native ]; then
        info "Screen    native $NATIVE_DISPLAY${NATIVE_OUT:+ ($NATIVE_OUT $PANEL)}, touch: ${TOUCH_FROM:-mouse}, OpenGL: ${QT_GL:-software}"
    elif [ "$REMOTE" = 1 ]; then
        info "Screen    $SIZE, VNC on port $VNC_PORT of this machine, password protected$viewer_note"
    else
        info "Screen    $SIZE, VNC localhost:$VNC_PORT$viewer_note"
    fi
    local f=""
    if [ "$VEHICLE" = 1 ]; then
        f="vehicle"
        local look="$VEHICLE_COLOR $VEHICLE_WHEELS $VEHICLE_PERFORMANCE"
        look=$(echo $look)
        [ -n "$look" ] && f="$f ($look)"
    else off="$off vehicle"; fi
    if [ -n "$GPS" ]; then f="$f, GPS $GPS"; else off="$off gps"; fi
    if [ "$AUDIO" = 1 ]; then f="$f, audio"; else off="$off audio"; fi
    if [ "$BLUETOOTH" = 1 ]; then f="$f, bluetooth ($BT_ADAPTER)"; else off="$off bluetooth"; fi
    if [ -n "$MUSIC" ]; then f="$f, music ${MUSIC/#$HOME/\~}"; else off="$off music"; fi
    if [ -n "$CAMERA" ]; then f="$f, camera $CAMERA"; else off="$off camera"; fi
    case " $SERVICES " in *" valhalla "*) f="$f, navigation" ;; *) off="$off nav" ;; esac
    info "Features  ${f#, }"
    [ -z "$off" ] || info "Off      $off  ${DIM}(./tesla start --help)$R0"
    [ "$VEHICLE" = 1 ] && info "Panel     http://localhost:$PANEL_PORT/  (doors, charging, lights, services, logs)"
    info "Services  ${SERVICES:-none}"
    [ -n "${LOG_DIR:-}" ] && info "Logs      ${LOG_DIR#"$TESLA_DIR/"}/  (./tesla logs: summary)"
    return 0
}

cmd_start() {
    parse_start_opts "$@"
    if [ -n "${CONF_CREATED:-}" ]; then
        info "Created tesla.conf with the default settings (all commented out); edit it to change them."
    fi
    local pid
    if pid=$(running_pid); then
        die "already running (pid $pid, logs $(readlink "$LOGS/current")). Stop it first: ./tesla stop"
    fi

    step "Checking"
    run_checks start
    if [ "$CHECK_FAIL" -gt 0 ]; then
        [ "$FORCE" = 1 ] || die "$CHECK_FAIL check(s) failed, see above. ./tesla check shows all; --force starts anyway"
    elif [ "$CHECK_WARN" = 0 ]; then
        info "all ok"
    fi
    [ "$NAV" = 1 ] && [ -n "$(image_file opt/navigon/VERSION)" ] && add_service valhalla qtcar-tmserver
    if [ "$DRY_RUN" = 1 ]; then
        [ "$VEHICLE" = 1 ] && add_service qtcar-sim qtcar-vehicle
        [ "$AUDIO" = 1 ] && add_service audioweaver audiod qtcar-audiod qtcar-mediaserver
        [ "$BLUETOOTH" = 1 ] && add_service dbus bsa_server btd qtcar-bluetooth
        [ -n "$MUSIC" ] && add_service qtcar-mediaserver
        [ -n "$GPS" ] && add_service qtcar-gpsmanager
        print_summary "Would start (--dry-run)"
        exit 0
    fi

    sudo_auth || exit 1
    LOG_DIR=$LOGS/$(date +%Y%m%d-%H%M%S)
    mkdir -p "$LOG_DIR"
    ln -sfn "$(basename "$LOG_DIR")" "$LOGS/latest"
    ln -sfn "$(basename "$LOG_DIR")" "$LOGS/current"
    cat >"$LOG_DIR/state" <<EOF
START_PID=$$
CHROOT=$CHROOT
IMAGE=$IMAGE
VNC_PORT=$VNC_PORT
PANEL_PORT=$PANEL_PORT
VEHICLE=$VEHICLE
SCREEN=$SCREEN
MOUNTED_IMAGE=0
EOF
    : >"$LOG_DIR/pids"
    event "start: $(features_on)"
    # the effective settings, for problem reports
    for v in "${CONF_VARS[@]}"; do printf '%s=%q  # %s\n' "$v" "${!v}" "${CONF_SRC[$v]}"; done >"$LOG_DIR/config"
    trap cleanup EXIT
    trap 'exit 130' INT TERM HUP
    # keep sudo's timestamp fresh: the cleanup at the end (also after ./tesla stop from another
    # terminal, hours later) must not ask for a password
    ( while sleep 60; do sudo -n -v 2>/dev/null || sudo -n true 2>/dev/null || exit; done ) </dev/null >/dev/null 2>&1 &
    add_pid $! sudo-keepalive

    # leftovers of an earlier run (e.g. a closed terminal) would hold the services' ports
    if [ -n "$(chroot_pids)" ]; then
        info "Stopping processes left over in $CHROOT from an earlier run:"
        stop_chroot_procs
    fi
    local m
    for m in $(mounts_below); do sudo umount "$m" 2>/dev/null || sudo umount -l "$m"; done
    CHECK_ALL=0 CHECK_FAIL=0
    check_ports
    [ "$CHECK_FAIL" -gt 0 ] && [ "$FORCE" = 0 ] && die "ports in use, see above (--force starts anyway)"

    if ! image_mounted >/dev/null; then
        [ -z "$(image_mounted_elsewhere)" ] || die "$IMAGE is mounted at $(image_mounted_elsewhere | head -1) already"
        sudo mkdir -p "$CHROOT"
        sudo mount -o loop "$IMAGE" "$CHROOT" || die "couldn't mount $IMAGE"
        sed -i 's/^MOUNTED_IMAGE=.*/MOUNTED_IMAGE=1/' "$LOG_DIR/state"
    fi

    if [ "$SCREEN" = native ]; then start_screen_native; else start_screen_vnc; fi

    step "Mounts and touch"
    sudo mkdir -p "$CHROOT/dev/input"
    sudo mount --bind /proc "$CHROOT/proc"
    # /dev/input: a tmpfs (touch is bound in below; the arcade game's virtual input devices get
    # their nodes there); /dev/uinput and /sys/class + /sys/devices (read-only) for the game's
    # input_to_virtual and its device scan. Not all of /sys: it would cover the net_cls cgroup.
    sudo mount -t tmpfs -o mode=755,size=64k tmpfs "$CHROOT/dev/input"
    sudo touch "$CHROOT/dev/uinput" && sudo mount --bind /dev/uinput "$CHROOT/dev/uinput"
    # QtCarVehicle (user tesla, uid 1111) makes the arcade's steering wheel and scroll wheel
    # devices through it when a game starts ("game-scroll-left Unable to open input device")
    sudo setfacl -m u:1111:rw /dev/uinput && echo "ACL=/dev/uinput" >>"$LOG_DIR/state"
    for d in class devices; do
        sudo mkdir -p "$CHROOT/sys/$d" && sudo mount --bind -o ro /sys/$d "$CHROOT/sys/$d"
    done
    # The chroot's own /dev/shm, not the host's: its shared memory (audio tplug channels, DVSM data
    # values, ...) is shared between processes of different users. In the host's sticky /dev/shm,
    # fs.protected_regular (Linux >= 4.19, not on the car) refuses O_CREAT on a file another user
    # owns ("Cannot open ring buffer shared memory", no sound), and leftovers of earlier runs got
    # stuck. Not sticky, fresh every start.
    sudo mount -t tmpfs -o mode=0777,nosuid,nodev,size=512m tmpfs "$CHROOT/dev/shm"
    for dev in null zero random urandom; do
        sudo touch "$CHROOT/dev/$dev"
        sudo mount --bind "/dev/$dev" "$CHROOT/dev/$dev"
    done
    sudo mount -t cgroup -o net_cls none "$CHROOT/sys/fs/cgroup/net_cls"
    # QtCar's touchscreen: tesla-touch turns the X mouse (VNC) or a real touchscreen (native,
    # mapped to the letterboxed UI) into a virtual multitouch device, bound as /dev/input/touch
    local touch_args=(--display "$QT_DISPLAY")
    [ -n "${TOUCH_FROM:-}" ] && touch_args=(--from "$TOUCH_FROM" --panel "$PANEL")
    sudo "$TOUCH_BIN" "${touch_args[@]}" --bind "$CHROOT/dev/input/touch" \
        >"$LOG_DIR/touch.out" 2> >(tee "$LOG_DIR/touch.err" >&2) &
    add_pid $! tesla-touch
    for _ in $(seq 100); do grep -q '^ready' "$LOG_DIR/touch.out" 2>/dev/null && break; sleep 0.1; done
    grep -q '^ready' "$LOG_DIR/touch.out" || die "tesla-touch failed to start, see $LOG_DIR/touch.err"

    [ "$SCREEN" = native ] || start_viewer

    step "Services"
    SERVICES=$(echo $SERVICES)
    start_features
    local args svc wrap
    for svc in $SERVICES; do
        args="" wrap=""
        [ "$svc" = qtcar-sim ] && [ -n "$SIM_PORT" ] && args="--udp :$SIM_PORT"
        case " $STRACE " in *" $svc "*) wrap="strace -f -tt -s 200 -o /tmp/strace-$svc.txt" ;; esac
        # restarted when they exit, like runit does on the car (qtcar-vehicle exits on purpose
        # when the car configuration changes). t= is the time, for ./tesla logs.
        sudo chroot "$CHROOT" /bin/sh -c 'while :; do $2 /usr/local/bin/qtcar-service "$0" $1
            echo "tesla: $0 exited ($?), restarting in 2 s [t=$(date +%s)]"; sleep 2; done' "$svc" "$args" "$wrap" \
            </dev/null >"$LOG_DIR/$svc.log" 2>&1 &
    done
    sleep 1
    start_helpers

    print_summary "Running"
    event "running: $SERVICES"
    # sudo runs commands in its own pty and puts the terminal in raw mode, so a Ctrl+C would only
    # reach QtCar, never this script. Hence: no terminal input for sudo (</dev/null), run it in the
    # background and `wait` (Ctrl+C interrupts that), and treat QtCar's own Ctrl+C exit as "stop".
    touch "$LOG_DIR/qtcar.log"
    local start_line status
    while true; do
        start_line=$(wc -l <"$LOG_DIR/qtcar.log")
        event "qtcar start"
        if [ "$VERBOSE" = 1 ]; then
            run_startup </dev/null > >(tee -a "$LOG_DIR/qtcar.log") 2>&1 &
        else
            run_startup </dev/null >>"$LOG_DIR/qtcar.log" 2>&1 &
        fi
        wait $!
        status=$?
        sleep 0.5   # let tee catch up
        # QtCar restarts itself to apply some settings ("CID UI is restarting reason: ...") and
        # exits with 137 then, same as on Ctrl+C. Only treat 130/137 as "stop" without that.
        if tail -n +$((start_line + 1)) "$LOG_DIR/qtcar.log" | grep -q 'CID UI is restarting'; then
            event "qtcar exit $status: $(tail -n +$((start_line + 1)) "$LOG_DIR/qtcar.log" |
                grep -o 'CID UI is restarting reason: .*' | head -1 | cut -c1-150)"
        else
            event "qtcar exit $status"
            # 130 = SIGINT; 137 = QtCar kills itself with SIGKILL when it gets SIGINT ("killing app")
            [ "$status" = 130 ] || [ "$status" = 137 ] && break
        fi
        [ "$RESTART" = 1 ] || break
        # QtCar exits on purpose to apply some settings (e.g. on first boot); the car restarts it too
        info "QtCar exited ($status), restarting in 2 s"
        sleep 2 & wait $!
    done
}

# before the services start: which services, the car's settings
start_features() {
    SIM_PORT=""
    if [ "$VEHICLE" = 1 ]; then
        add_service qtcar-sim qtcar-vehicle
        # the firmware's CAN signal database, for tesla-can.py (once; needs unicorn + pyelftools)
        if [ ! -s vehicle/work/can-db.json ]; then
            info "Extracting the CAN signal database from the firmware (once)..."
            python3 vehicle/extract-can-db.py "$CHROOT" >"$LOG_DIR/can-db.log" 2>&1 ||
                warn "failed, see can-db.log (needs: pip install --user unicorn pyelftools)"
        fi
        car_opt exteriorColor "$VEHICLE_COLOR" RedMulticoat SolidBlack SilverMetallic MidnightSilver - DeepBlue PearlWhite
        car_opt wheelType "$VEHICLE_WHEELS" Pinwheel18 Stiletto19 Stiletto20 Stiletto20Staggered Gemini19Square Gemini19Staggered
        car_opt performancePackage "$VEHICLE_PERFORMANCE" Base Performance Ludicrous BasePlus
        # with tesla-can.py running, the sim sends to it (:1235) and it forwards to qtcar-vehicle
        # (:1234), with its own signals written into the sim's frames
        [ -s vehicle/work/can-db.json ] && SIM_PORT=1235
    fi
    if [ "$AUDIO" = 1 ]; then
        # the car's sound card ("model3") is a snd-aloop card here (see the kit's audio group);
        # AudioWeaver plays into loopback device 0, the host plays loopback device 1
        if ! grep -q '^ *[0-9]* \[model3 *\]' /proc/asound/cards; then
            # two cards like the car: "model3" (the amplifier) and "virtual" (Bluetooth audio loops)
            sudo modprobe snd-aloop id=model3,virtual enable=1,1 pcm_substreams=4,8 ||
                warn "couldn't load snd-aloop, no audio"
        fi
        sudo mkdir -p "$CHROOT/dev/snd" && sudo mount --bind /dev/snd "$CHROOT/dev/snd"
        add_service audioweaver audiod qtcar-audiod qtcar-mediaserver
        audio_clock
    fi
    [ "$BLUETOOTH" = 1 ] && start_bluetooth
    if [ -n "$CAMERA" ]; then
        # QtCar's backup camera reads V4L2 XR24 (32 bit BGRX) from [bkcam] deviceId. ffmpeg
        # converts $CAMERA (any format, e.g. scrcpy's YU12 on v4l2loopback) into CAMERA_DEV.
        if [ ! -e "$CAMERA_DEV" ]; then
            warn "$CAMERA_DEV doesn't exist, no backup camera"
            CAMERA=""
        else
            sudo chmod 666 "$CAMERA_DEV"      # the chroot's video group has another gid
            sudo touch "$CHROOT$CAMERA_DEV" && sudo mount --bind "$CAMERA_DEV" "$CHROOT$CAMERA_DEV"
            # forceFeedGood: on the car the camera module reports VAPI_backupCameraFeedGood
            settings_conf bkcam deviceId="$CAMERA_DEV" deviceFmt=XR24 textureFmt=bgra \
                width="${CAMERA_SIZE%x*}" height="${CAMERA_SIZE#*x}" forceFeedGood=true
        fi
    fi
    if [ -n "$MUSIC" ]; then
        # a folder as a USB stick: the car's udev rule mounts sticks at /home/tesla/media/usb-<dev>,
        # the media server finds them there ([usb] media_path, kit) and indexes them
        sudo mkdir -p "$CHROOT/home/tesla/media/usb-music" &&
            sudo mount --bind -o ro "$MUSIC" "$CHROOT/home/tesla/media/usb-music"
        add_service qtcar-mediaserver
    fi
    if [ -n "$GPS" ]; then
        add_service qtcar-gpsmanager
        # QtCar shows the car at GpsManager's estimated position by default, and the estimator
        # only follows GPS while the car reports wheel speed. Without vehicle data use raw GPS
        # instead (GUI_smoothGPSUpdates, a data value QtCar keeps in its settings DB).
        local ids
        ids=$(awk -F: '$1=="tesla"{print $3":"$4}' "$CHROOT/etc/passwd")
        sudo python3 vehicle/qtcar-settings.py --owner "$ids" \
            "$CHROOT/home/tesla/.Tesla/data/QtCarSettings.db" GUI_smoothGPSUpdates=false >/dev/null
    fi
}

# after the services: the host-side helpers
start_helpers() {
    if [ "$VEHICLE" = 1 ] && [ -s vehicle/work/can-db.json ]; then
        local bind=127.0.0.1
        [ "$REMOTE" = 1 ] && bind=0.0.0.0
        python3 vehicle/tesla-can.py --preset parked --sim-port "$SIM_PORT" --http "$PANEL_PORT" --http-bind "$bind" \
            --log-dir "$LOG_DIR" --respond "${CAR_SIM[@]}" </dev/null >"$LOG_DIR/can.log" 2>&1 &
        add_pid $! tesla-can
    fi
    if [ "$AUDIO" = 1 ]; then
        # QtCar ignores volume changes while SPEECH_voiceRecGUIState is invalid ("Delaying volume
        # change"); the speech recognizer, not running here, would set it. Keep it at Inactive.
        ( while :; do
            v=$(curl -s --max-time 2 --http1.0 "http://127.0.0.1:4220/_data_get_value_request_?name=SPEECH_voiceRecGUIState")
            [ "$v" = "<invalid>" ] && curl -s --max-time 2 --http1.0 \
                "http://127.0.0.1:4220/_data_set_value_request_?name=SPEECH_voiceRecGUIState&value=Inactive" >/dev/null
            sleep 5
          done ) </dev/null >/dev/null 2>&1 &
        add_pid $! speech-state
        # what AudioWeaver sends to the base amp (8 channels, 32 bit) -> stereo -> the host's sound;
        # summed at half level (AUDIO_REMIX): at full level the sum clipped
        ( while :; do
            # 192-frame (4 ms) periods like audiod's: with the model3 card clocked by a sound timer
            # (low tick rate kernels, see start_features) both ends of a cable must match
            arecord -q -D hw:CARD=model3,DEV=1,SUBDEV=0 -f S32_LE -c 8 -r 48000 -t raw \
                --period-size=192 --buffer-size=3072 |
                sox -q -t raw -e signed -b 32 -c 8 -r 48000 - -t raw -e signed -b 16 -c 2 - remix -m $AUDIO_REMIX |
                play_stereo
            echo "tesla: audio pipeline exited, restarting in 2 s [t=$(date +%s)]"
            sleep 2
          done ) </dev/null >"$LOG_DIR/audio.log" 2>&1 &
        add_pid $! audio
    fi
    if [ -n "$CAMERA" ]; then
        local cw=${CAMERA_SIZE%x*} ch=${CAMERA_SIZE#*x}
        # off while an arcade game runs (the car's game-mode.sh stops its dashcam too): with the game
        # and the camera together audiod fell behind (pump errors, audible); without the camera
        # not (A/B in a race on the T480: 0,4,3,3 vs 0,0,0,0 errors per 15 s). Stopped, not paused:
        # after SIGCONT ffmpeg made up for the pause with 1000+ duplicated frames, and that burst
        # caused errors of its own.
        game() { pgrep -x CobaltLinux >/dev/null || pgrep -x mametesla >/dev/null; }
        ( off=0
          while :; do
            if game; then
                [ $off = 0 ] && echo "tesla: camera off while a game runs [t=$(date +%s)]"
                off=1; sleep 1; continue
            fi
            [ $off = 1 ] && echo "tesla: camera on again [t=$(date +%s)]"
            off=0
            # cropped to QtCar's aspect ratio (4:3), not stretched: a 16:9 webcam looked distorted
            ffmpeg -nostdin -loglevel warning -f v4l2 -i "$CAMERA" \
                -vf "crop='min(iw,ih*$cw/$ch)':'min(ih,iw*$ch/$cw)',scale=$cw:$ch,format=bgr0" -f v4l2 "$CAMERA_DEV" &
            ff=$!
            while kill -0 $ff 2>/dev/null; do
                game && { kill $ff; break; }
                sleep 1
            done
            wait $ff 2>/dev/null
            game || sleep 2
          done ) </dev/null >"$LOG_DIR/camera.log" 2>&1 &
        add_pid $! camera
    fi
    # the arcade's game windows: the car has no window manager, a desktop has one (native screen);
    # on Xvfb it only gives MAME the keyboard focus for QtCar's XTest keys
    # (and hides the desktop's mouse cursor, as on the car)
    local hide=()
    [ "$SCREEN" = native ] && hide=(--hide-cursor)
    DISPLAY=$QT_DISPLAY python3 tools/game-windows.py --tidk "$CHROOT/tmp/games/cobalt/tidk" "${hide[@]}" \
        </dev/null >"$LOG_DIR/game-windows.log" 2>&1 &
    add_pid $! game-windows
    if [ -n "$GPS" ]; then
        local gps_args
        case "$GPS" in -*) read -ra gps_args <<<"$GPS" ;; *) gps_args=(--pos "$GPS") ;; esac
        [ "$VEHICLE" = 1 ] && gps_args+=(--can)
        python3 vehicle/tesla-gps.py -q --rootfs "$CHROOT" "${gps_args[@]}" </dev/null >"$LOG_DIR/gps.log" 2>&1 &
        add_pid $! tesla-gps
    fi
}

# /startup.sh in the chroot, with the display QtCar uses (QTCAR_* are read by the kit's startup.sh)
run_startup() {
    sudo chroot "$CHROOT" /bin/sh -c 'QTCAR_DISPLAY=$1 QTCAR_XAUTHORITY=$2 QTCAR_ARGS=$3 QTCAR_GL=$4 QTCAR_GL_DRIVER=$5
        export QTCAR_DISPLAY QTCAR_XAUTHORITY QTCAR_ARGS QTCAR_GL QTCAR_GL_DRIVER; exec /bin/sh /startup.sh' \
        _ "$QT_DISPLAY" "${QT_XAUTH:-/root/.Xauthority}" "$QTCAR_ARGS" "${QT_GL:-software}" "${QT_GL_DRIVER:-}"
}

# the virtual screen (Xvfb :1) and x11vnc
start_screen_vnc() {
    step "Screen and VNC"
    QT_DISPLAY=$DISPLAY_NUM
    if xdpyinfo -display $DISPLAY_NUM >/dev/null 2>&1; then
        info "X display $DISPLAY_NUM already running, using it"
    else
        Xvfb $DISPLAY_NUM -screen 0 "${SIZE}x24" +extension RECORD -nolisten tcp 2>"$LOG_DIR/xvfb.log" &
        add_pid $! Xvfb
        for _ in $(seq 50); do xdpyinfo -display $DISPLAY_NUM >/dev/null 2>&1 && break; sleep 0.1; done
    fi
    local vnc_access=(-localhost -nopw)
    [ "$REMOTE" = 1 ] && vnc_access=(-usepw)
    x11vnc -display $DISPLAY_NUM "${vnc_access[@]}" -rfbport "$VNC_PORT" -forever -shared -noxdamage -quiet \
        >"$LOG_DIR/x11vnc.log" 2>&1 &
    add_pid $! x11vnc
}

# the real screen: QtCar's UI (SIZE, 1920x1200) scaled to fit the panel with black bars. The X
# framebuffer becomes SIZE and the output gets a transform (scale + centering offset); xrandr
# complains that the offset output doesn't fit the framebuffer, but applies it.
start_screen_native() {
    step "Screen (native, $NATIVE_DISPLAY)"
    QT_DISPLAY=$NATIVE_DISPLAY
    local out ui_w=${SIZE%x*} ui_h=${SIZE#*x} pw ph tf
    out=$(native_output)
    NATIVE_OUT=${out%%$'\t'*} PANEL=${out#*$'\t'}
    [ -n "$NATIVE_OUT" ] || die "no connected output on $NATIVE_DISPLAY (xrandr)"
    pw=${PANEL%x*} ph=${PANEL#*x}
    echo "NATIVE=$NATIVE_DISPLAY" >>"$LOG_DIR/state"
    if [ "$PANEL" != "$SIZE" ]; then
        tf=$(python3 -c "
uw, uh, pw, ph = $ui_w, $ui_h, $pw, $ph
s = max(uw / pw, uh / ph)                      # framebuffer pixels per panel pixel
print('%f,0,%f,0,%f,%f,0,0,1' % (s, -(pw - uw / s) / 2 * s, s, -(ph - uh / s) / 2 * s))")
        DISPLAY=$NATIVE_DISPLAY xrandr --output "$NATIVE_OUT" --fb "$SIZE" --transform "$tf" 2>/dev/null
        echo "NATIVE_RESTORE=--output $NATIVE_OUT --transform none --fb $PANEL" >>"$LOG_DIR/state"
        info "$NATIVE_OUT is $PANEL: the UI ($SIZE) is scaled to fit, with black bars"
    fi
    DISPLAY=$NATIVE_DISPLAY xset s off -dpms 2>/dev/null
    # QtCar (user tesla in the chroot) needs the session's X cookie
    local cookie=$CHROOT/tmp/qtcar-xauth
    sudo rm -f "$cookie"
    xauth -f "${XAUTHORITY:-$HOME/.Xauthority}" nlist "$NATIVE_DISPLAY" 2>/dev/null | sed 's/^..../ffff/' |
        sudo xauth -q -f "$cookie" nmerge - 2>/dev/null
    sudo touch "$cookie" && sudo chmod 644 "$cookie"
    QT_XAUTH=/tmp/qtcar-xauth
    # the touchscreen, passed through by tesla-touch (mouse mode if there is none)
    local ts
    if [ "$TOUCH_DEVICE" = auto ]; then ts=$(find_touchscreen); else ts=$TOUCH_DEVICE; fi
    TOUCH_FROM=${ts%%$'\t'*}
    if [ -n "$TOUCH_FROM" ]; then info "touchscreen: ${ts//$'\t'/ }"
    else info "no touchscreen found: the mouse works as one finger"; fi
    native_gpu
}

# OpenGL on the GPU (GPU=auto|on|off): the kit has Mesa 18's i965 for Intel GPUs. QtCar (user
# tesla, uid 1111 in the chroot) and the arcade games (cobalt 1226, mame 1980; in software they
# run at a few frames a second and miss short taps) get /dev/dri and access to its nodes; removed
# again at the end.
native_gpu() {
    QT_GL=software QT_GL_DRIVER=""
    [ "$GPU" = off ] && { info "OpenGL: software (--no-gpu)"; return; }
    local card vendor driver name
    card=$(ls -d /sys/class/drm/renderD* 2>/dev/null | head -1)
    vendor=$(cat "$card/device/vendor" 2>/dev/null)
    case "$vendor" in
        0x8086) driver=i965 name=Intel ;;
        0x1002)
            # AMD: radeonsi of the kit's Mesa 18.0.5 (a link to its Gallium megadriver) knows GCN
            # up to Vega/Raven, not the newer RDNA GPUs. Never tested: only when asked for (--gpu)
            if [ "$GPU" != on ]; then
                info "OpenGL: software (AMD GPU: --gpu tries radeonsi, untested; Mesa 18 knows GCN up to Vega)"
                return
            fi
            driver=radeonsi name=AMD ;;
        *)
            info "OpenGL: software (GPU ${vendor:-none}: the kit's Mesa has drivers for Intel and AMD only)"
            return ;;
    esac
    sudo mkdir -p "$CHROOT/dev/dri" && sudo mount --bind /dev/dri "$CHROOT/dev/dri" || return
    local f
    for f in /dev/dri/card* /dev/dri/renderD*; do
        sudo setfacl -m u:1111:rw,u:1226:rw,u:1980:rw "$f" && echo "ACL=$f" >>"$LOG_DIR/state"
    done
    QT_GL=hardware QT_GL_DRIVER=$driver
    info "OpenGL: GPU ($name, $driver)"
}

start_viewer() {
    if [ -z "$VIEWER" ]; then
        for v in krdc remote-viewer remmina vncviewer; do command -v $v >/dev/null && VIEWER=$v && break; done
    fi
    VIEWER_USED=${VIEWER:-}
    case "${VIEWER:-none}" in
        none) VIEWER_USED="" ;;
        vncviewer) vncviewer "localhost::$VNC_PORT" >/dev/null 2>&1 & add_pid $! viewer ;;
        remmina) remmina -c "vnc://localhost:$VNC_PORT" >/dev/null 2>&1 & add_pid $! viewer ;;
        *) $VIEWER "vnc://localhost:$VNC_PORT" >/dev/null 2>&1 & add_pid $! viewer ;;
    esac

}

# 16 bit stereo 48 kHz from stdin to the PC's speakers: PipeWire, PulseAudio or plain ALSA
play_stereo() {
    if command -v pw-play >/dev/null; then pw-play --format s16 --channels 2 --rate 48000 -
    elif command -v pacat >/dev/null; then pacat --playback --format=s16le --channels=2 --rate=48000 --latency-msec=60
    else aplay -q -t raw -f S16_LE -c 2 -r 48000 -
    fi
}

# On kernels with a coarse tick (Debian: 250 Hz) snd-aloop moves in 4 ms jiffies steps and
# audiod's 4 ms periods underrun all the time (hundreds of "Pump error"s a minute, audio mostly
# silent). Then the model3 card gets a sound timer instead: snd-dummy's PCM, which runs on a
# high-resolution timer, playing with exactly audiod's period (192 frames). snd-aloop requires
# every stream of the card to use that period (the host's arecord does).
audio_clock() {
    local res card
    res=$(sed -n 's/^G0:.*: *\([0-9.]*\)us.*/\1/p' /proc/asound/timers 2>/dev/null | cut -d. -f1)
    [ -n "$res" ] && [ "$res" -gt 1000 ] || return 0
    card=$(awk '$2 == "[model3" {print $1}' /proc/asound/cards)
    [ -n "$card" ] || return 0
    grep -q '^ *[0-9]* \[hrclock' /proc/asound/cards ||
        sudo modprobe snd-dummy hrtimer=1 pcm_devs=1 pcm_substreams=1 id=hrclock ||
        { warn "couldn't load snd-dummy: audio will stutter (system timer ${res} us)"; return 0; }
    ( while :; do
        sudo aplay -q -D hw:CARD=hrclock,DEV=0 -f S16_LE -c 2 -r 48000 --period-size=192 --buffer-size=768 \
            -t raw /dev/zero
        sleep 1
      done ) </dev/null >"$LOG_DIR/audio-clock.log" 2>&1 &
    add_pid $! audio-clock
    echo hrclock | sudo tee "/proc/asound/card$card/timer_source" >/dev/null
    echo "TIMER_CARD=$card" >>"$LOG_DIR/state"
    info "audio: kernel tick ${res} us, the model3 card runs on a high-resolution clock (snd-dummy)"
}

# Bluetooth: the firmware's Broadcom stack (bsa_server) on this PC's adapter, through
# bluetooth/hci-bridge.py (HCI user channel <-> a pseudo terminal bound as the chroot's ttyS0).
start_bluetooth() {
    local dev=${BT_ADAPTER#hci} tty
    [ -d "/sys/class/bluetooth/hci$dev" ] || { warn "no Bluetooth adapter hci$dev, no Bluetooth"; BLUETOOTH=0; return; }
    if systemctl is-active -q bluetooth 2>/dev/null; then
        sudo systemctl stop bluetooth && echo "BT_RESTORE=hci$dev" >>"$LOG_DIR/state"
        info "bluetooth: BlueZ stopped while it runs (the adapter belongs to the car's stack)"
    fi
    # the adapter's USB port, to power-cycle it if it hangs (see restore_bluetooth)
    local port
    port=$(readlink -f "/sys/class/bluetooth/hci$dev/device/../port" 2>/dev/null)
    [ -e "$port/disable" ] && echo "BT_USBPORT=$port" >>"$LOG_DIR/state"
    sudo python3 bluetooth/hci-bridge.py --dev "$dev" --link "$LOG_DIR/bt-tty" >"$LOG_DIR/bt-bridge.log" 2>&1 &
    add_pid $! bt-bridge
    for _ in $(seq 50); do grep -q '^ready' "$LOG_DIR/bt-bridge.log" 2>/dev/null && break; sleep 0.1; done
    tty=$(sed -n 's/^ready: \([^ ]*\).*/\1/p' "$LOG_DIR/bt-bridge.log")
    [ -n "$tty" ] || { warn "the Bluetooth bridge didn't start, see bt-bridge.log"; BLUETOOTH=0; return; }
    sudo touch "$CHROOT/dev/ttyS0" && sudo mount --bind "$tty" "$CHROOT/dev/ttyS0"
    add_service dbus bsa_server btd qtcar-bluetooth
    [ "$AUDIO" = 1 ] && add_service a2dpbridge
}
