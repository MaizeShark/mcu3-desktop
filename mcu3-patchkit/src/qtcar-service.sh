#!/bin/sh
# Start one of the firmware's QtCar-framework services inside the chroot, the way
# /etc/sv/<job>/run does it on the car, minus minijail, AppArmor, cgroups and firewall rules.
#
#   qtcar-service <runit job> [extra args]     e.g.  qtcar-service qtcar-vehicle
#   qtcar-service valhalla                     the routing engine (see /etc/sv/valhalla/run)
#   qtcar-service qtcar-sim                    QtCarSimService, Tesla's vehicle simulator: acts as
#                                              the gateway and sends a simulated car's CAN frames
#   qtcar-service audioweaver | audiod         the audio DSP (AudioWeaver) and its control daemon;
#                                              need the "model3" sound card (snd-aloop, start_all AUDIO=1)
#   qtcar-service --list
#
# Everything runs on loopback (like QtCar in this setup), not on the car's 192.168.90.x net.
# QCSVC_NOUSER=1 runs the service as the calling user instead of its own (e.g. in a rootless
# container, where switching users doesn't work).

if [ "$1" = "--list" ]; then
    for j in $(sed -n "s/^\([a-z0-9-]*\))$/\1/p" /etc/RunQtCar.vars); do [ -d "/etc/sv/$j" ] && echo "$j"; done
    echo valhalla
    echo qtcar-sim
    echo audioweaver
    echo audiod
    echo a2dpbridge
    echo dbus; echo bsa_server; echo btd
    echo fireplace; echo dog-mode; echo hal-9000; echo mame; echo cobalt
    exit 0
fi
JOB=$1
[ -n "$JOB" ] || { echo "usage: qtcar-service <job> | --list" >&2; exit 2; }
shift
[ -d "/etc/sv/$JOB" ] || [ "$JOB" = qtcar-sim ] || { echo "qtcar-service: no service $JOB" >&2; exit 1; }

export PATH=/bin:/usr/bin:/sbin:/usr/sbin:/usr/local/bin

# The audio type (base/premium) as /usr/bin/audio-type gives it, but without asking the gateway
# first: audio-type runs `gw-diag GET_CONFIG_DATA 0 31`, which sends UDP to the car's gateway
# (192.168.90.102:3500, which here goes out to the LAN) and waits 5 x 10 s for an answer before
# it uses [audiod] audio_type from settings.conf anyway (the kit sets it). That held up audiod and
# QtCarAudiod for 50 s, and QtCar's media sources wait for the audio system ("Loading..." on USB).
audio_type() {
    t=$(sed -r -n -e '/\[audiod\]/,/\[/s/^audio_type=(.*)/\1/p' /home/tesla/.Tesla/car/settings.conf 2>/dev/null)
    [ -n "$t" ] && echo "$t" || audio-type
}

as_user() {  # as_user <user[:groups]> <cmd...>
    u=$1; shift
    if [ -n "$QCSVC_NOUSER" ] || [ "$(id -u)" != 0 ]; then exec "$@"; fi
    exec chpst -u "$u" "$@"
}

if [ "$JOB" = valhalla ]; then
    # same as /etc/sv/valhalla/run: tiles in /opt/navigon/tm/<2-letter region>/valhalla,
    # overridable with force_maps_path= in settings.conf
    . /etc/tesla.env
    SETTINGSCONF=$TESLA_HOME/.Tesla/car/settings.conf
    REGION=$(ls /opt/navigon/tm/ 2>/dev/null | grep '^..$' | head -1)
    MAPPATH=$(grep -i -E '^force_maps_path *=' "$SETTINGSCONF" 2>/dev/null | sed 's/=/ /' | awk '{print $2}')
    [ -z "$MAPPATH" ] && MAPPATH=/opt/navigon/tm/$REGION/valhalla
    [ -d "$MAPPATH" ] || { echo "qtcar-service: no map tiles in $MAPPATH" >&2; exit 1; }
    OVERRIDE=$TESLA_HOME/.Tesla/data/map_override.pbf.gz
    echo "qtcar-service: valhalla, region ${REGION:-?}, tiles $MAPPATH" >&2
    # Clean environment like runit on the car: valhalla builds a C++ locale from LANG/LC_*, and
    # the host's values (e.g. de_DE.UTF-8, passed through sudo) don't exist in the firmware, so
    # every route failed with "locale::facet::_S_create_c_locale name not valid".
    as_user valhalla env -i PATH="$PATH" LD_LIBRARY_PATH=/usr/proto2/lib:/usr/lib:/lib \
        valhalla_server --config "$TESLA_UI/assets/tesla_maps/valhalla.json" \
        --tile_dir "$MAPPATH" --override_file "$OVERRIDE"
fi

# Audio, as /etc/sv/{audioweaver,audiod}/run: AudioWeaver (AWE) mixes the apps' tplug channels
# (shared memory, /dev/shm/tplug-*) and plays them on the car's card "model3"; audiod configures
# it (volumes, routing) over AWE's TCP port and creates the tplugs. Without minijail/sandbox,
# iptables, apparmor, RT tuning and the amplifier GPIO/I2C setup (/opt/audio/tesla/init.sh).
if [ "$JOB" = audioweaver ]; then
    # AWE must not open the sound card itself (on the car its minijail has no /dev/snd):
    # audiod does the I/O. /etc/asound-awe.conf points its hardware PCMs at a missing card.
    mkdir -p /var/log/audioweaver
    cd / || exit 1
    SCRIPT=/opt/audioweaver/$(audio-type --product).aws
    echo "qtcar-service: audioweaver $SCRIPT" >&2
    exec env -i PATH="$PATH" LD_LIBRARY_PATH=/opt/audioweaver LC_ALL=C \
        ALSA_CONFIG_PATH=/usr/share/alsa/alsa.conf:/etc/asound-awe.conf \
        /opt/audioweaver/AWE_command_line_tesla -nocmd -norate "-script:$SCRIPT"
fi
if [ "$JOB" = audiod ]; then
    mkdir -p /home/audiod/audiologs
    chown -h audiod:audio /home/audiod/audiologs 2>/dev/null
    TYPE=$(audio_type 2>/dev/null)
    echo "qtcar-service: audiod, audio type $TYPE" >&2
    # as root: its pump threads need real-time priority (4 ms periods). On the car minijail gives
    # the audiod user CAP_SYS_NICE; without it the output underran now and then (stutter).
    as_user root env -i PATH="$PATH" HOME=/home/audiod LD_LIBRARY_PATH=/opt/audioweaver LC_ALL=C \
        TPLUG_LOG_XRUN=1 /usr/bin/audiod --audio-type="$TYPE" --audio-api=/opt/audioweaver/dsp-control.csv "$@"
fi

# Bluetooth music, as /etc/sv/a2dpbridge/run (minus sandbox and taskset): btd plays the phone's
# audio into the snd-aloop card "virtual"; alsaloop copies it into AudioWeaver's a2dp tplug.
if [ "$JOB" = a2dpbridge ]; then
    echo "qtcar-service: a2dpbridge (alsaloop a2dp_in_loop -> a2dp_out_loop)" >&2
    exec env -i PATH="$PATH" LC_ALL=C /usr/bin/alsaloop -v -p 35 -C a2dp_in_loop -P a2dp_out_loop -U -S1 -l 6144
fi

# Bluetooth (./tesla start --bluetooth, see bluetooth/README.md): the firmware's own system bus,
# Broadcom's BSA stack on the "UART" /dev/ttyS0 (on a PC: bluetooth/hci-bridge.py's pseudo
# terminal, bound there), and Tesla's btd on top, which QtCarBluetooth reaches over D-Bus.
if [ "$JOB" = dbus ]; then
    mkdir -p /var/run/dbus
    rm -f /var/run/messagebus.pid /var/run/dbus/pid /var/run/dbus/system_bus_socket
    echo "qtcar-service: dbus -> dbus-daemon --system (the firmware's own bus)" >&2
    exec /usr/bin/dbus-daemon --nofork --system
fi
if [ "$JOB" = bsa_server ]; then
    mkdir -p /var/run/bsa_server
    rm -f /var/run/bsa_server/bt-avk-fifo /var/run/bsa_server/bt-daemon-socket
    echo "qtcar-service: bsa_server on /dev/ttyS0 ${BSA_ARGS:-}" >&2
    # no -p (Broadcom firmware patch) unless the controller is the car's BCM4349. Not with env -i:
    # it segfaults with an empty environment.
    exec /usr/bin/bsa_server -d /dev/ttyS0 -u /var/run/bsa_server/ ${BSA_ARGS:-}
fi
if [ "$JOB" = btd ]; then
    # it needs the bus (without it, it runs but QtCarBluetooth never finds it) and BSA's socket
    for i in $(seq 100); do
        [ -S /var/run/dbus/system_bus_socket ] && [ -S /var/run/bsa_server/bt-daemon-socket ] && break
        sleep 0.2
    done
    mkdir -p /var/lib/btd /var/run/btd
    cd /var/run/btd || exit 1
    echo "qtcar-service: btd" >&2
    exec /usr/bin/btd
fi

# qtcar_env VAR: a variable from QtCar's environment (its display, X cookie, GL settings), for the
# programs QtCar has the escalator start on its screen (videos, games)
qtcar_env() {
    p=$(pidof QtCar | cut -d' ' -f1)
    [ -n "$p" ] && tr '\0' '\n' <"/proc/$p/environ" 2>/dev/null | sed -n "s/^$1=//p"
}

# The games (Toybox -> Arcade), as /etc/sv/{mame,cobalt,cobalt-input}/run minus sandbox, AppArmor,
# CPU pinning (game-mode.sh would also stop services such as valhalla). QtCar asks the escalator,
# which runs "sv start mame|cobalt" (the kit's /sbin/sv wrapper sends that here).
#   mame: the arcade classics (Asteroids, Missile Command, ...), /usr/bin/mametesla, game name in
#         /home/mame/game (QtCar writes it)
#   cobalt: Beach Buggy Racing 2 (/usr/bin/games/cobalt/CobaltLinux), window and car colour from
#         /tmp/games/cobalt/tidk/TIDK_*; cobalt-input (input_to_virtual) turns /dev/input/touch into
#         virtual steering/touch devices (uinput), which the game finds in /dev/input
case "$JOB" in mame|cobalt|cobalt-input)
    DISP=$(qtcar_env DISPLAY); XAUTH=$(qtcar_env XAUTHORITY)
    GLENV=""   # only what QtCar has: an empty MESA_LOADER_DRIVER_OVERRIDE would name driver ""
    for v in LIBGL_ALWAYS_SOFTWARE MESA_LOADER_DRIVER_OVERRIDE; do
        val=$(qtcar_env $v); [ -n "$val" ] && GLENV="$GLENV $v=$val"
    done
    ;;
esac
if [ "$JOB" = mame ]; then
    export HOME=/home/mame
    mkdir -p $HOME /var/run/mame
    [ -e $HOME/game ] || echo missile >$HOME/game
    touch /var/run/mame/polepos.state && chmod 644 /var/run/mame/polepos.state
    chown -hR mame:mame $HOME /var/run/mame
    game=$(cat $HOME/game)
    echo "qtcar-service: mame -> mametesla $game (DISPLAY ${DISP:-:1})" >&2
    as_user mame env -i PATH="$PATH" HOME=$HOME DISPLAY="${DISP:-:1}" XAUTHORITY="$XAUTH" $GLENV LC_ALL=C \
        /usr/bin/mametesla -inipath /opt/mame/ "$game"
fi
if [ "$JOB" = cobalt-input ]; then
    mkdir -p /home/games/cobalt-input /tmp/games/cobalt-input
    echo "qtcar-service: cobalt-input -> input_to_virtual" >&2
    cd /usr/bin/games/cobalt || exit 1
    # as root: it needs /dev/uinput and /dev/input/touch (on the car its sandbox grants them)
    exec env -i PATH="$PATH" LD_LIBRARY_PATH=/usr/bin/games/cobalt/ LC_ALL=C ./input_to_virtual
fi
if [ "$JOB" = cobalt ]; then
    mkdir -p /home/games/cobalt /tmp/games/cobalt /home/tesla/.Tesla/data/cobalt
    chown -hR cobalt:games /home/games/cobalt /tmp/games/cobalt /home/tesla/.Tesla/data/cobalt
    TIDK=""
    for f in /tmp/games/cobalt/tidk/TIDK_*; do [ -f "$f" ] && TIDK="$TIDK ${f##*/}=$(cat "$f")"; done
    # the virtual input devices: input_to_virtual creates them through uinput; the chroot's /dev
    # is the image's, so make their nodes (the game scans /dev/input and /sys/class/input)
    /usr/local/bin/qtcar-service cobalt-input </dev/null >>/tmp/games/cobalt-input.log 2>&1 &
    sleep 1
    for d in /sys/class/input/event*; do
        [ -r "$d/dev" ] || continue
        n=/dev/input/${d##*/}; [ -e "$n" ] && continue
        mknod "$n" c "$(cut -d: -f1 "$d/dev")" "$(cut -d: -f2 "$d/dev")" && chgrp games "$n" && chmod 660 "$n"
    done
    echo "qtcar-service: cobalt -> CobaltLinux (DISPLAY ${DISP:-:1}, $TIDK)" >&2
    cd /usr/bin/games/cobalt || exit 1
    as_user cobalt:games env -i PATH="$PATH" HOME=/home/games/cobalt DISPLAY="${DISP:-:1}" XAUTHORITY="$XAUTH" \
        $GLENV $TIDK LD_LIBRARY_PATH=/usr/bin/games/cobalt/ FMOD_ALSA_DEVICE=game LC_ALL=C \
        ./CobaltLinux --RootPath /usr/bin/games/cobalt/
fi

# The videos of fireplace / dog mode / HAL 9000 (as /etc/sv/<name>/run, minus the sandbox):
# /usr/bin/tvideo as user tvideo. QtCar asks the escalator, which runs "sv start <name>"
# (the kit's /sbin/sv wrapper sends that here). DISPLAY: QtCar's, not the car's :0.
case "$JOB" in fireplace|dog-mode|hal-9000)
    V=file:///usr/tesla/UI/assets/videos
    case "$JOB" in
        fireplace) ARGS="-l $V/fireplace.mp4" ;;
        dog-mode) ARGS="-l -n dog-mode -x 0 -y 0 $V/dog-mode.mp4" ;;
        hal-9000) ARGS="-l -n hal-9000 -c $V/hal-9000.mp4" ;;
    esac
    QTCAR_PID=$(pidof QtCar | cut -d' ' -f1)
    DISP=$(tr '\0' '\n' <"/proc/$QTCAR_PID/environ" 2>/dev/null | sed -n 's/^DISPLAY=//p')
    XAUTH=$(tr '\0' '\n' <"/proc/$QTCAR_PID/environ" 2>/dev/null | sed -n 's/^XAUTHORITY=//p')
    mkdir -p /home/tvideo && chown -hR tvideo:tvideo /home/tvideo 2>/dev/null
    echo "qtcar-service: $JOB -> tvideo $ARGS (DISPLAY ${DISP:-:1})" >&2
    as_user tvideo env -i PATH="$PATH" HOME=/home/tvideo DISPLAY="${DISP:-:1}" XAUTHORITY="$XAUTH" LC_ALL=C \
        /usr/bin/tvideo $ARGS
    ;;
esac

export QCSUBNET=127.0.0 QCCID=1 QCIC=1 QCGW=1 QCAP=1 QCLB=1

if [ "$JOB" = qtcar-sim ]; then
    # No runit job on the car (dev tool). Without options it talks to 127.0.0.1 / 127.255.255.255
    # like everything here: CAN frames to :1234, SIM_* data values over HTTP on port 4190.
    . /etc/tesla.env
    cd "$TESLA_BIN" || exit 1
    echo "qtcar-service: qtcar-sim -> QtCarSimService $*" >&2
    exec env -i PATH="$PATH" HOME=/root LD_LIBRARY_PATH="$TESLA_LIB:/usr/cid-lib:/usr/lib64:/lib64:/lib:/usr/lib" \
        LC_ALL=C LANG=C ./QtCarSimService "$@"
fi

cd "/etc/sv/$JOB" || exit 1    # RunQtCar.vars takes the job name from $PWD
. /etc/tesla.env
. /etc/RunQtCar.vars
export TESLA_IP=127.0.0.1 TESLA_IC=127.0.0.1 TESLA_GW=127.0.0.1
export LD_LIBRARY_PATH=$TESLA_LIB:/usr/cid-lib:/usr/lib64:/lib64:/lib:/usr/lib
export LC_ALL=C LANG=C LANGUAGE=C

[ -n "$QCAPP" ] || { echo "qtcar-service: $JOB is not a QtCar service" >&2; exit 1; }

# Services with their own user (gpsmanager, qtaudiod, mediaserver, ...) keep their settings DB
# and .watches in $HOME/.Tesla. On the car their minijail mounts their own home writable; here
# chpst would leave HOME=/home/tesla, which they can't write. Then a car config change (e.g.
# VAPI_driverAssist from the simulator) is never stored, and QtCarGpsManager restarted itself
# every ~13 s ("Restarting ... because of changed DVs") until one restart hung on exit.
# So: HOME = the user's home, with its .Tesla dirs and a link to the shared settings.conf.
if [ -n "$QCUSER" ] && [ "$QCUSER" != tesla ] && [ -z "$QCSVC_NOUSER" ] && [ "$(id -u)" = 0 ]; then
    UHOME=$(awk -F: -v u="$QCUSER" '$1 == u {print $6}' /etc/passwd)
    if [ "$UHOME" = / ]; then
        # qtbt (QtCarBluetooth) has "/" as its home. On the car its minijail mounts /home/tesla
        # writable and it keeps PhonebookV2.db, BTDevices.db there, where QtCar reads the contacts
        # (with HOME=/ they went to /.Tesla/data and the phone app stayed at "Loading...").
        # It's in group tesla: make tesla's data directory and files group-writable (SQLite also
        # needs the directory for its journal).
        chgrp tesla "$TESLA_HOME/.Tesla/data" && chmod g+ws "$TESLA_HOME/.Tesla/data"
        for f in "$TESLA_HOME"/.Tesla/data/*.db "$TESLA_HOME"/.Tesla/data/*.db-journal; do
            [ -e "$f" ] && chgrp tesla "$f" && chmod g+w "$f"
        done
        # contacts stored under /.Tesla by an earlier run: move them over (only if newer)
        for f in PhonebookV2.db BTDevices.db QtCarBluetoothSettings.db; do
            [ -f "/.Tesla/data/$f" ] && [ "/.Tesla/data/$f" -nt "$TESLA_HOME/.Tesla/data/$f" ] &&
                mv -f "/.Tesla/data/$f" "$TESLA_HOME/.Tesla/data/$f" && chgrp tesla "$TESLA_HOME/.Tesla/data/$f" &&
                chmod g+w "$TESLA_HOME/.Tesla/data/$f"
        done
        export HOME=$TESLA_HOME
    elif [ -n "$UHOME" ] && [ -d "$UHOME" ]; then
        mkdir -p "$UHOME/.Tesla/car" "$UHOME/.Tesla/data"
        [ -e "$UHOME/.Tesla/car/settings.conf" ] || ln -s "$TESLA_HOME/.Tesla/car/settings.conf" "$UHOME/.Tesla/car/settings.conf"
        chown -h "$QCUSER" "$UHOME/.Tesla" "$UHOME/.Tesla/car" "$UHOME/.Tesla/data" "$UHOME/.Tesla/car/settings.conf"
        export HOME=$UHOME
    fi
fi
# QtCar itself runs without address options and then uses local broadcast (127.255.255.255) for
# its data. With --gw/--ip/... the services switch to the car network's multicast (224.0.0.26)
# instead and QtCar never hears them (e.g. TMServer -> no navigation). Keep only the ports.
QCOPTS=$(echo " $QCOPTS " | sed -E 's/ --(gw|ip|ic|ap|lb) +[0-9.]+/ /g')

# like /etc/sv/qtcar-audiod/run
[ "$JOB" = qtcar-audiod ] && QCOPTS="$QCOPTS --audio-type $(audio_type) --audio-platform $(audio-type -p)"

cd "$TESLA_BIN" || exit 1
echo "qtcar-service: $JOB -> $QCAPP $QCOPTS $* (user ${QCUSER:-root})" >&2
# QCGROUPS is ":grp:grp" for most jobs, but "tesla" (no colon) for qtcar-bluetooth
case "$QCGROUPS" in ""|:*) ;; *) QCGROUPS=":$QCGROUPS" ;; esac
as_user "$QCUSER$QCGROUPS" "./$QCAPP" $QCOPTS "$@"
