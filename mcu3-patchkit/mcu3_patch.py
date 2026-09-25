#!/usr/bin/env python3
"""
Turn a pristine Tesla Model 3 MCU rootfs (firmware 2019.20.4.2, x86_64) into the
state that runs QtCar in a chroot on a desktop Linux host (see start_all.sh).

  * 5 byte patches: libQtCarUtils (asserts only log), libcef (WebAudio oscillator),
    libQtCarSim (no drive inverter), audiod (no A2B); patches of earlier kits that turned out
    unnecessary are restored to the firmware's bytes where found ('retired')
  * Mesa 18.0.5 + LLVM 6.0 + deps (downloaded per sources.toml, cached in payload/)
  * preload shims built from src/ (icu_preload, egl_pixmap_shim, cef_nosandbox)
  * config / directories / symlinks / startup script / qtcar-service

Usage:
  sudo python3 mcu3_patch.py /path/to/rootfs              # apply everything
  sudo python3 mcu3_patch.py /path/to/rootfs --check      # only report status
  python3 mcu3_patch.py --list                            # list steps and patches
  sudo python3 mcu3_patch.py ROOT --skip cef --skip identity
  sudo python3 mcu3_patch.py ROOT --vin 5YJ3... --birthday ...

Every step is idempotent: already applied things are detected and skipped.
Patched binaries are backed up as <file>.orig (unless --no-backup).
"""
import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

KIT = os.path.dirname(os.path.abspath(__file__))
PAYLOAD = os.path.join(KIT, 'payload')
SOURCES = os.path.join(KIT, 'sources.toml')      # download URLs for payload files
DOWNLOADS = os.path.join(KIT, 'downloads')       # cache for downloaded packages
TESLA_UID = TESLA_GID = 1111

# ---------------------------------------------------------------------------
# Binary patches: (id, group, file offset, original bytes, patched bytes, description)
# Offsets are file offsets (== virtual addresses for these binaries).
# ---------------------------------------------------------------------------
BINARIES = [
    {
        'path': 'usr/tesla/UI/bin/QtCar',
        'sha1_orig': '53f1c1bd1770fe5528ab29f2d797ef7b645f41db',
        'patches': [],
        # the escalator's cgroup setup succeeds in the chroot (./tesla start mounts net_cls; QtCar
        # ends up in net_cls:/qtcar), so QtCar's exit(1) on failure never happens (2026-09-25)
        'retired': [
            ('qtcar-cgroup-exit-1', 'core', 0x2ea5e5, 'e80645f7ffbf01000000e85c48f7ff', '9090909090bf010000009090909090',
             'ChromiumManager::initializeCef: perror("failed to set internet-cgroups") + exit(1)'),
            ('qtcar-cgroup-exit-2', 'core', 0x2ea691, 'e85a44f7ffe94f', '9090909090e959',
             'ChromiumManager::initializeCef: perror("failed to set qtcar-cgroups") + exit(1)'),
        ],
    },
    {
        'path': 'usr/tesla/UI/lib/libQtCarGUI.so.1.0.0',
        'sha1_orig': 'df5b74322502ea348e259d1c3b432c1e0c0b4421',
        'patches': [
        ],
        # patches of earlier kits that do nothing (or no longer apply): put back where found.
        # (id, group, offset, firmware bytes, bytes an earlier kit wrote, why)
        'retired': [
            # an assert only logs (utils-assert-nonfatal); the branch logs and continues
            ('gui-energy-roadload', 'core', 0xd75ed8, '0f8502020000', '909090909090',
             'EnergyModelUtils::roadLoadEstimateFor: carType assert (hid a log line)'),
            ('gui-energy-driveunit', 'core', 0xd7617d, '7571', '9090', 'EnergyModelUtils::driveUnitConfig: same'),
            ('gui-energy-pack', 'core', 0xd762af, '752f', '9090', 'EnergyModelUtils::packEnergy_kWh: same'),
            ('gui-escalator-1', 'core', 0x7c26a3, 'e8d89bf1ff', '31c0909090',
             'libQtCarGUI\'s ChromiumManager::initializeCef is never called: QtCar has its own copy'),
            ('gui-escalator-2', 'core', 0x7c295f, 'e81c99f1ff', '31c0909090', 'same'),
            ('gui-cgroup-exit-1', 'core', 0x7c32a5, 'e81609f2ffbf01000000e8cc99f2ff', '9090909090bf010000009090909090',
             'same (qtcar-cgroup-exit-* patch the copy in QtCar)'),
            ('gui-cgroup-exit-2', 'core', 0x7c3351, 'e86a08f2ffe94f', '9090909090e959', 'same'),
            ('gui-camera-reset', 'core', 0xd3f760, '4155be', '31c0c3',
             'the old "no cameras" stub CameraStream::resetDevice() -> 0 (the backup camera works)'),
            ('gui-camera-init', 'core', 0xd43000, '55beff', '31c0c3', 'the old stub CameraStream::initDevice() -> 0'),
            ('gui-video-ximagesink', 'core', 0x12f2e93, b'xvimagesink'.hex(), b'ximagesink\0'.hex(),
             'a gst-launch pipeline string nobody uses (the videos play in /usr/bin/tvideo)'),
            ('gui-cef-nosandbox', 'cef', 0x12ae593, '498d7c2408498d75', '41c7431001000000',
             'no_sandbox in the library\'s initializeCef copy (unused); src/cef_nosandbox.c sets it'),
        ],
    },
    {
        'path': 'usr/tesla/UI/lib/libQtCarUtils.so.1.0.0',
        'sha1_orig': '7286a3e0c86b69962ea1b102c48aa69432852fce',
        'patches': [
            ('utils-assert-nonfatal', 'core', 0xb4703, 'c604', 'eb0b',
             'Logger::logAssert: skip the deliberate write to address 0 outside "prod" mode, so a '
             'failed assert is only logged, like on the car (was: media button asserts crashed QtCar)'),
        ],
        # NOPs of single logAssert calls: with logAssert log-only they only hid a log line
        'retired': [
            ('utils-assert-ctor', 'core', 0xaf0ea, 'e87113feff', '9090909090', 'DataValue::DataValue: logAssert'),
            ('utils-assert-protected', 'core', 0xaf69f, 'e8bc0dfeff', '9090909090',
             'DataValue::setValueNoLock: "protected DataValue" logAssert'),
            ('utils-assert-duplicate', 'core', 0xc3997, 'e8c4cafcff', '9090909090',
             'DataValueManager::addValue: "Duplicate data value" logAssert'),
        ],
    },
    {
        'path': 'usr/tesla/UI/lib/libQtCarMediaV2.so.1.0.0',
        'sha1_orig': '89923a08454a1b9dd459a389184806c1eef1b14f',
        'patches': [
        ],
        'retired': [
            ('media-assert-pause', 'core', 0x463763, 'e8680df8ff', '9090909090',
             'MediaV2::MediaMasterSourceController::pause: logAssert (only hid a log line)'),
        ],
    },
    {
        # Tesla's CEF 3.3683 (Chrome 73) is built with Blink's stub FFT (fft_frame_stub.cc: FFTFrame
        # allocates nothing). Any page that creates a WebAudio OscillatorNode makes
        # PeriodicWave::CreateBandLimitedTables write into the null FFT arrays (VectorMath::Vsmul)
        # and the renderer dies ("tesla-cef-zygot segfault ... libcef.so[4165885]"). Many sites do
        # (fingerprinting / bot detection), tesla.com too. createOscillator() now returns null.
        'path': 'usr/lib/libcef.so',
        'sha1_orig': '975741de4fff287a55d498cf137e104623d23852',
        'patches': [
            ('cef-no-oscillator', 'cef', 0x8170490, '554889', '31c0c3',
             'BaseAudioContext::createOscillator() -> return null (WebAudio FFT is a stub in this build)'),
        ],
    },
    {
        # QtCarSimService (vehicle simulator, qtcar-service qtcar-sim): with the drive rail on it
        # also simulates the drive inverter, but its gear/speed don't follow SIM_shiftState /
        # SIM_vehicleSpeed and DI_vehicleSpeed comes out as -40 km/h. vehicle/tesla-can.py sends
        # the DI frames instead, so the sim's DI module is switched off.
        'path': 'usr/tesla/UI/lib/libQtCarSim.so.1.0.0',
        'sha1_orig': '96bcf8a90c03a7fa2183e9a62d53121ae1e31a26',
        'patches': [
            ('sim-no-di-2hz', 'vehicle', 0x51cd60, '4155b908', 'c355b908',
             'SimDriveInverter::send2Hz() -> return (no DI frames from the simulator)'),
            ('sim-no-di-10hz', 'vehicle', 0x523180, '41574156', 'c3574156',
             'SimDriveInverter::send10Hz() -> return (no DI_systemStatus/DI_speed/odometer frames)'),
        ],
    },
    {
        # audiod does the audio I/O for AudioWeaver (DSP) and pumps it. Once the A2B PCM device
        # pumps, its main loop brings up the A2B stack (transceiver on /dev/i2c-4, amplifiers,
        # microphones); without that hardware a2b_stackAlloc fails and audiod exits. The base
        # amp output alone is enough on a PC, so never start A2B.
        'path': 'usr/bin/audiod',
        'sha1_orig': '0ef70bcffe81e771d3b042970e972a3cbffc79e2',
        'patches': [
            ('audiod-no-a2b', 'audio', 0xa0aa, '488d0d', '31c0c3',
             'audiod: "is the A2B device pumping" -> 0, no A2B stack (no /dev/i2c-4 on a PC)'),
        ],
    },
]

# ---------------------------------------------------------------------------
# Files the kit adds (downloaded per sources.toml or built from src/, cached in payload/): (group, path, mode, sha1)
# ---------------------------------------------------------------------------
PAYLOAD_FILES = [
    # Mesa 18.0.5 swrast (Ubuntu) replaces Tesla's GL stack, which does not work on Xvfb
    ('mesa', 'usr/lib/dri/swrast_dri.so', 0o644, 'e79ef300403a067005be6480841930965d8d7e64'),
    # Intel GPUs (./tesla start --native): the firmware's own i965 is Mesa 11.2.2 (2016), which
    # knows the car's Apollo Lake but not newer GPUs such as Kaby Lake. Mesa 18.0.5's knows them.
    ('mesa', 'usr/lib/dri/i965_dri.so', 0o644, 'c9d25e4f047893cd423d11cba4379db278c50570'),
    ('mesa', 'usr/lib/libglapi.so.0.0.0', 0o755, '17173722ee272789ff970d1ea45cb0694a5c27c9'),
    ('mesa', 'usr/lib/libdrm.so.2.4.0', 0o644, '20611823a72925941768e3c50844ffa42f811913'),
    ('mesa', 'usr/lib/libdrm_intel.so.1.0.0', 0o644, '8e3131c329c05700c3611c2cb73523ad830456a8'),
    ('mesa', 'usr/lib/libdrm_amdgpu.so.1.0.0', 0o644, 'a59141838c286e6b733cd01e5ed3f6d6d688aedd'),
    ('mesa', 'usr/lib/libdrm_nouveau.so.2.0.0', 0o644, '125decd5b11f506ffd816cb0fa64a547814bdb60'),
    ('mesa', 'usr/lib/libdrm_radeon.so.1.0.1', 0o644, '8ba204b8d56d6c10dd91cb20e0d6394324483222'),
    ('mesa', 'usr/lib/libffi.so.6.0.4', 0o644, 'a5be37d7823f44e5cb5399a41df25c76f4e1649d'),
    ('mesa', 'usr/lib/libLLVM-6.0.so.1', 0o644, 'cb66fc578ab21f183fc2ed8e64082757c8d92b1b'),
    ('mesa', 'usr/lib/libLLVM-3.8.so.1', 0o644, 'abea0c731126dfbfc04ad61c4d154a5dfe11f291'),
    ('mesa', 'usr/lib/libbsd.so.0.8.2', 0o644, 'c3e805c53edb389d81371800c2dc4d87ee6f60e4'),
    ('mesa', 'usr/lib/libedit.so.2.0.53', 0o644, '40321cfa7515de7c8889796bdbf8755c92e2b6c4'),
    ('mesa', 'usr/lib/libelf-0.165.so', 0o644, 'c3e1a15086816cb6cb7ed96e9884373222340f5c'),
    ('mesa', 'usr/lib/libexpatw.so.1.6.0', 0o644, '70f1d3fcde86b8d23b3b0abe79fc52ced557b688'),
    ('mesa', 'usr/lib/libsensors.so.4.4.0', 0o644, 'b68396fbd099769297be424b72a6505eca9838da'),
    ('mesa', 'usr/lib/libtic.so.5.9', 0o644, '9cf8c1943c093e781b747de427b03f8146844031'),
    ('mesa', 'usr/lib/libtinfo.so.5.9', 0o644, '2bd9389f52439de7a42efcb8a3c3f8d4c881e8b2'),
    # Passes an fd for icudtl.dat to CEF (fixes "Invalid file descriptor to ICU data"). src/icu_preload.c
    # Was /tmp/icu_preload.so in the original setup; moved out of /tmp so it can't get cleaned away.
    ('system', 'usr/lib/icu_preload.so', 0o755, 'a3da594a29ad33acc8ab61fa57e1139759804e4b'),
    # Shows the browser: emulates EGL images from X pixmaps, which Xvfb + Mesa swrast can't do.
    # Always built from src/egl_pixmap_shim.c (no fixed hash).
    ('cef', 'usr/lib/egl_pixmap_shim.so', 0o755, '-'),
    # Sets CefSettings.no_sandbox (and optional CEF logging / DevTools) via a cef_initialize()
    # wrapper. Always built from src/cef_nosandbox.c.
    ('cef', 'usr/lib/cef_nosandbox.so', 0o755, '-'),
]

# Files earlier kits added that aren't needed: removed where found (with their .orig backups).
# They aren't in the firmware, so whatever is there is the kit's. (group, path, why)
RETIRED_FILES = [
    ('cef', 'usr/tesla/UI/bin/chrome-sandbox',
     'fake setuid sandbox helper: CEF runs with no_sandbox (src/cef_nosandbox.c) and never starts it'),
    ('cef', 'usr/tesla/UI/lib/chrome-sandbox', 'patched copy of /usr/lib/chrome-sandbox, unused for the same reason'),
]

# (group, link path, target)
SYMLINKS = [
    # AMD GPUs (./tesla start --native --gpu): radeonsi (GCN) and r600 (TeraScale, e.g. HD 6670)
    # are part of the Gallium megadriver
    ('mesa', 'usr/lib/dri/radeonsi_dri.so', 'swrast_dri.so'),
    ('mesa', 'usr/lib/dri/r600_dri.so', 'swrast_dri.so'),
    ('mesa', 'usr/lib/libLLVM-6.0.so', 'libLLVM-6.0.so.1'),
    ('mesa', 'usr/lib/libLLVM-3.8.so', 'libLLVM-3.8.so.1'),
    ('mesa', 'usr/lib/libbsd.so.0', 'libbsd.so.0.8.2'),
    ('mesa', 'usr/lib/libdrm_amdgpu.so.1', 'libdrm_amdgpu.so.1.0.0'),
    ('mesa', 'usr/lib/libdrm_nouveau.so.2', 'libdrm_nouveau.so.2.0.0'),
    ('mesa', 'usr/lib/libdrm_radeon.so.1', 'libdrm_radeon.so.1.0.1'),
    ('mesa', 'usr/lib/libedit.so.2', 'libedit.so.2.0.53'),
    ('mesa', 'usr/lib/libelf.so.1', 'libelf-0.165.so'),
    ('mesa', 'usr/lib/libexpatw.so.1', 'libexpatw.so.1.6.0'),
    ('mesa', 'usr/lib/libsensors.so.4', 'libsensors.so.4.4.0'),
    ('mesa', 'usr/lib/libtic.so.5', 'libtic.so.5.9'),
    ('mesa', 'usr/lib/libtinfo.so.5', 'libtinfo.so.5.9'),
    ('assets', 'usr/tesla/UI/assets/day/about/badge_model_s_cid.png',
     '/usr/tesla/UI/assets/day/about/badge_model_3_cid.png'),
    ('assets', 'usr/tesla/UI/assets/night/about/badge_model_s_cid.png',
     '/usr/tesla/UI/assets/night/about/badge_model_3_cid.png'),
    ('assets', 'usr/tesla/UI/assets/night/car/park/roof_glass.png', '../hero/roof_glass.png'),
    ('cef', 'usr/tesla/UI/lib/libcef.so', '/usr/lib/libcef.so'),
    ('cef', 'usr/tesla/UI/lib/icudtl.dat', '/usr/lib/icudtl.dat'),
    ('cef', 'usr/tesla/UI/lib/locales', '/usr/lib/locales'),
    ('cef', 'usr/tesla/UI/lib/natives_blob.bin', '/usr/lib/natives_blob.bin'),
    ('cef', 'usr/tesla/UI/lib/snapshot_blob.bin', '/usr/lib/snapshot_blob.bin'),
    ('cef', 'usr/tesla/UI/lib/cef.pak', '/usr/lib/cef.pak'),
    ('cef', 'usr/tesla/UI/lib/cef_100_percent.pak', '/usr/lib/cef_100_percent.pak'),
    ('cef', 'usr/tesla/UI/lib/cef_200_percent.pak', '/usr/lib/cef_200_percent.pak'),
    ('cef', 'usr/tesla/UI/lib/cef_extensions.pak', '/usr/lib/cef_extensions.pak'),
    # CEF looks for its resources next to libcef.so, which the link above puts in UI/lib. Without
    # this one every renderer dies with "Failed to deserialize the V8 snapshot blob".
    ('cef', 'usr/tesla/UI/lib/v8_context_snapshot.bin', '/usr/lib/v8_context_snapshot.bin'),
    ('cef', 'usr/tesla/UI/lib/devtools_resources.pak', '/usr/lib/devtools_resources.pak'),
    ('cef', 'usr/tesla/UI/lib/swiftshader', '/usr/lib/swiftshader'),
]

# Directories QtCar expects, plus mount points used by start_all.sh. (path, owner uid or None)
# (path, mode) of existing directories whose mode needs changing
DIR_MODES = [
    # CEF's cache/cookies/local storage (cache_path). The browser process is QtCar (user tesla,
    # member of group tesla-cef), but the directory is 0755 tesla-cef: "Unable to create cache",
    # "Cookie sqlite error 14".
    ('home/tesla-cef', 0o775),
]

DIRECTORIES = [
    ('home/tesla', TESLA_UID),
    ('home/tesla/.Tesla/car', TESLA_UID),
    ('home/tesla/.Tesla/data', TESLA_UID),
    ('root/.Tesla/car', None),
    ('root/.Tesla/data/media', None),
    ('root/.Tesla/cache', None),
    ('root/.Tesla/browser', None),
    ('var/lock', None),
    ('run/connman', None),
    ('var/etc/qtcar-connman', None),   # qtcar-connman caches its cellular policy here
    ('dev/input', None),
    ('dev/shm', None),
    ('sys/fs/cgroup/net_cls', None),
    ('sys/fs/cgroup/memory', None),
]
EMPTY_FILES = ['dev/null', 'dev/random', 'dev/urandom', 'var/lock/log-top']  # bind-mount targets / lock file

STARTUP_SH = r'''#!/bin/sh
# Run inside chroot as root (started by start_all.sh on the host)
export PATH=/bin:/usr/bin:/sbin:/usr/sbin:/usr/local/bin

# Kernel settings. NOTE: /proc is the host's, so these change the HOST kernel until reboot.
echo 1 > /proc/sys/vm/overcommit_memory
echo 0 > /proc/sys/vm/mmap_min_addr

# Cgroups QtCar / CEF expect (net_cls is mounted by start_all.sh)
mkdir -p /sys/fs/cgroup/net_cls/nonet
mkdir -p /sys/fs/cgroup/net_cls/qtcar
mkdir -p /sys/fs/cgroup/net_cls/internet-cgroup
mkdir -p /sys/fs/cgroup/net_cls/qtcar-cgroup
mkdir -p /sys/fs/cgroup/net_cls/internet
mkdir -p /sys/fs/cgroup/net_cls/tesla-cef
mkdir -p /sys/fs/cgroup/memory/cef
mkdir -p /var/lock

# DNS (connman is not running; /etc/resolv.conf points here). Needed for map tiles.
mkdir -p /var/run/connman
echo "nameserver 8.8.8.8" > /var/run/connman/resolv.conf

# Escalator (privileged helper QtCar talks to via /tmp/escalator). It sometimes exits after a
# request it can't serve (seen: starting the dashcam daemon on QtCar's first start), and
# QtCar's ChromiumManager::initializeCef crashes without it ("Memory corruption detected").
# So keep it in a respawn loop, one loop across QtCar restarts, and wait for its socket.
if ! grep -qs 'escalator-loo[p]' /proc/[0-9]*/cmdline; then  # [p]: don't match this grep
    sh -c 'while :; do
        pidof escalator >/dev/null || { rm -f /tmp/escalator; /usr/bin/escalator; }
        sleep 1
    done' escalator-loop </dev/null >/dev/null 2>&1 &
fi
for i in $(seq 50); do [ -S /tmp/escalator ] && break; sleep 0.1; done

# OpenGL: software (llvmpipe) unless ./tesla start passes QTCAR_GL=hardware (native screen with
# a GPU). Then the driver is named, since the firmware's libGL (Mesa 11.2) doesn't know newer GPUs.
if [ "$QTCAR_GL" = hardware ]; then
    GL_ENV="MESA_LOADER_DRIVER_OVERRIDE=${QTCAR_GL_DRIVER:-i965}"
else
    GL_ENV="LIBGL_ALWAYS_SOFTWARE=1"
fi

# Launch. ./tesla start passes QTCAR_DISPLAY, QTCAR_XAUTHORITY (native mode: the real screen)
# and QTCAR_ARGS (e.g. --size WxH for a screen that isn't 1920x1200).
su tesla -c "cd /usr/tesla/UI/bin && \
  DISPLAY=${QTCAR_DISPLAY:-:1} \
  XAUTHORITY=${QTCAR_XAUTHORITY:-/root/.Xauthority} \
  LD_LIBRARY_PATH=/usr/tesla/UI/lib:/usr/cid-lib:/usr/lib64:/lib64:/lib:/usr/lib \
  LC_ALL=C LANG=C LANGUAGE=C \
  QT_X11_NO_MITSHM=1 \
  $GL_ENV \
  LD_PRELOAD=/usr/lib/icu_preload.so:/usr/lib/egl_pixmap_shim.so:/usr/lib/cef_nosandbox.so \
  EGL_PIXMAP_SHIM_DEBUG=1 \
  CEF_SHIM_DEBUG=1 CEF_LOG_SEVERITY=warning CEF_REMOTE_DEBUGGING_PORT=9222 \
  ./QtCar --touch evdev,autorange $QTCAR_ARGS"
'''

# Car config keys the kit sets in settings.conf. Only these keys are touched; other settings
# (e.g. [nav] from navigation/install-maps.sh) stay as they are.
SETTINGS_KEYS = {'General': {'VAPI_carType': '3', 'VAPI_trim': '0', 'VAPI_chassisType': '0'},
                 # audio-type asks the gateway (gw-diag), which isn't there, and then says "premium"
                 # (amplifiers on the A2B bus); base = the MCU's own amp output (card model3, pcm 0)
                 'audiod': {'audio_type': 'base'},
                 # USB sticks are found under $HOME/media/usb-* (StorageUtils::usbMediaFolder); the
                 # media server runs with its own HOME, the udev rule mounts under /home/tesla/media
                 'usb': {'media_path': '/home/tesla/media'},
                 # QtCarVehicle's TPMS filter (TPMSStatusMessage::filterStatus): on (the default) it
                 # keeps the old warning values, so a low tire never showed a warning here
                 'vapi': {'filter_tpms_faults': 'false'}}
# QtCar runs as user tesla and reads /home/tesla/.Tesla/car; the /root copy is from when it
# still ran as root. (path, owner uid)
SETTINGS_FILES = [
    ('home/tesla/.Tesla/car/settings.conf', TESLA_UID),
    ('root/.Tesla/car/settings.conf', 0),
]

# (group, path, content, mode[, owner uid])
TEXT_FILES = [
    ('system', 'run/connman/resolv.conf', 'nameserver 8.8.8.8\n', 0o644),
    ('system', 'startup.sh', STARTUP_SH, 0o755),
    # starts the firmware's other QtCar services (qtcar-vehicle, qtcar-tmserver, ...), see start_all.sh
    ('system', 'usr/local/bin/qtcar-service', open(os.path.join(KIT, 'src/qtcar-service.sh')).read(), 0o755),
    # no runit in the chroot: the escalator's "sv start fireplace" etc. go to qtcar-service
    # (runit's sv stays as /sbin/sv.runit for everything else)
    ('system', 'sbin/sv', open(os.path.join(KIT, 'src/sv-wrapper.sh')).read(), 0o755),
]

# No "model3" sound card on a PC. start_all.sh (AUDIO=1) loads snd-aloop as card "model3"; the
# car's PCM devices 0 (base amp) and 2 (A2B) become subdevices 0 and 2 of loopback device 0, and
# start_all.sh plays what AudioWeaver writes there from loopback device 1 on the host.
ASOUND_MARK = '# --- mcu3-patchkit: sound card on a PC ---'
ASOUND_BLOCK = ASOUND_MARK + '''
pcm.!auc {
        @args [ DEVICE CHANNELS ]
        @args.DEVICE.type integer
        @args.CHANNELS.type integer

        type hw
        card model3
        device 0
        subdevice $DEVICE
        rate 48000
        channels $CHANNELS
}
# AudioWeaver opens the eCall channels at 48 kHz (AWE_CmdLine.ini), but awe_tplug8 is 8 kHz:
# "can't set sample rate requested=48000 got=8000", and AWE exits (100). Resample.
pcm.!awe_in_ecallrx { type plug slave.pcm "awe_tplug8:ecallrx,1" }
pcm.!awe_out_ecalltx { type plug slave.pcm "awe_tplug8:ecalltx,1" }
# --- end mcu3-patchkit ---
'''

# AudioWeaver only (qtcar-service sets ALSA_CONFIG_PATH): on the car it runs in a minijail
# without /dev/snd, its own opens of the sound card fail and audiod owns the PCMs. Here /dev/snd
# is visible, AWE would take them first and audiod gets EBUSY.
ASOUND_AWE = '''# mcu3-patchkit: AudioWeaver only (ALSA_CONFIG_PATH in qtcar-service). On the car it runs
# without /dev/snd, so audiod (which feeds it) owns the sound card. Point it at a card that
# isn't there.
pcm.!auc {
        @args [ DEVICE CHANNELS ]
        @args.DEVICE.type integer
        @args.CHANNELS.type integer
        type hw
        card 31
}
'''

GROUPS = {
    'core': 'binary patches needed for QtCar to run (asserts only log, as on the car)',
    'cef': 'browser/CEF: no-sandbox shim, EGL pixmap shim, WebAudio oscillator patch, CEF symlinks',
    'mesa': 'Mesa 18 software renderer + LLVM 6.0 and dependencies',
    'assets': 'missing asset symlinks (Model S badge -> Model 3, roof_glass)',
    'system': 'passwd, settings.conf, DNS, directories, icu_preload.so, /startup.sh',
    'identity': '/var/etc/vin and /var/etc/birthday (only with --vin / --birthday)',
    'vehicle': 'QtCarSimService without its drive-inverter module (fake vehicle data, VEHICLE=1)',
    'audio': "asound.conf: the car's sound card on a snd-aloop card (AUDIO=1 in start_all.sh)",
}


# ---------------------------------------------------------------------------
class Runner:
    def __init__(self, root, check, backup, skip, quiet=False):
        self.root, self.check, self.backup, self.skip = root, check, backup, set(skip)
        self.is_root = os.geteuid() == 0
        self.counts = {'ok': 0, 'done': 0, 'todo': 0, 'error': 0}
        self.quiet = quiet          # only what isn't 'ok' (and its section header)
        self.header = None

    def section(self, name):
        self.header = f'\n== {name}'
        if not self.quiet:
            print(self.header)
            self.header = None

    def p(self, rel):
        return os.path.join(self.root, rel)

    def report(self, state, what):
        self.counts[state] += 1
        if self.quiet and state == 'ok':
            return
        if self.header:
            print(self.header)
            self.header = None
        tag = {'ok': 'already', 'done': 'APPLIED', 'todo': 'TODO', 'error': 'ERROR'}[state]
        print(f'  [{tag:>7}] {what}')

    def skipped(self, group, ident=None):
        return group in self.skip or (ident is not None and ident in self.skip)

    def chown(self, path, uid=0, gid=0, follow=True):
        if self.is_root:
            os.chown(path, uid, gid, follow_symlinks=follow)

    def write_atomic(self, path, data, mode, uid=0, gid=0):
        d = os.path.dirname(path)
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix='.mcu3patch.')
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(data)
            os.chmod(tmp, mode & 0o777)
            self.chown(tmp, uid, gid)
            os.chmod(tmp, mode)  # after chown, so setuid bits survive
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def backup_file(self, path):
        bak = path + '.orig'
        if self.backup and os.path.isfile(path) and not os.path.islink(path) and not os.path.exists(bak):
            shutil.copy2(path, bak)

    # -- binary patches ------------------------------------------------------
    def binaries(self):
        self.section('Binary patches')
        for b in BINARIES:
            patches = [x for x in b['patches'] if not self.skipped(x[1], x[0])]
            retired = b.get('retired', [])
            path = self.p(b['path'])
            if not os.path.exists(path):
                self.report('error', f"{b['path']}: file not found")
                continue
            data = bytearray(open(path, 'rb').read())
            base_sha = sha1(data)
            if base_sha != b['sha1_orig'] and not self.partially_patched(data, b['patches'] + retired):
                self.report('error', f"{b['path']}: unknown file version (sha1 {base_sha}), not touching it")
                continue
            changed = False
            # (id, offset, bytes found = ok, bytes to replace, what to write, label)
            todo = [(i, off, new, old, new, f"{b['path']} @0x{off:x} {i}: {d}") for i, g, off, old, new, d in patches]
            todo += [(i, off, orig, old, orig, f"{b['path']} @0x{off:x} {i} (retired, restoring the firmware's "
                      f"bytes): {d}") for i, g, off, orig, old, d in retired]
            for ident, off, good, bad, write, label in todo:
                good, bad = bytes.fromhex(good), bytes.fromhex(bad)
                cur = bytes(data[off:off + len(good)])
                if cur == good:
                    # a retired patch that isn't there is nothing to mention
                    if ident not in {x[0] for x in retired}:
                        self.report('ok', label)
                elif cur == bad:
                    if self.check:
                        self.report('todo', label)
                    else:
                        data[off:off + len(good)] = good
                        changed = True
                        self.report('done', label)
                else:
                    self.report('error', f'{label} -- unexpected bytes {cur.hex()}')
            if changed:
                self.backup_file(path)
                self.write_atomic(path, bytes(data), os.stat(path).st_mode & 0o7777)

    @staticmethod
    def partially_patched(data, patches):
        for _, _, off, a, b, _ in patches:
            cur = bytes(data[off:off + len(bytes.fromhex(a))])
            if cur not in (bytes.fromhex(a), bytes.fromhex(b)):
                return False
        return True

    def retired_files(self):
        """Remove files earlier kits added that aren't needed any more (RETIRED_FILES)."""
        self.section('Retired files')
        for group, rel, why in RETIRED_FILES:
            if self.skipped(group):
                continue
            for f in (rel, rel + '.orig'):
                path = self.p(f)
                if not os.path.lexists(path):
                    continue
                label = f'{f} (retired: {why})'
                if self.check:
                    self.report('todo', 'remove ' + label)
                else:
                    os.unlink(path)
                    self.report('done', 'removed ' + label)

    # -- payload -------------------------------------------------------------
    def payload(self):
        self.section('Payload files')
        for group, rel, mode, want in PAYLOAD_FILES:
            if self.skipped(group):
                continue
            src = os.path.join(PAYLOAD, rel)
            dst = self.p(rel)
            if not payload_ok(rel, want):
                if self.check:
                    self.report('todo', f'{rel} (not in payload/, would be fetched)')
                    continue
                try:
                    fetch_payload(rel, want)
                except FetchError as e:
                    self.report('error', f'{rel}: {e}')
                    continue
            want_installed = sha1_file(src)  # differs from `want` for files built from src/
            if os.path.isfile(dst) and not os.path.islink(dst) and sha1_file(dst) == want_installed:
                if (os.stat(dst).st_mode & 0o7777) != mode and not self.check:
                    os.chmod(dst, mode)
                self.report('ok', rel)
                continue
            if self.check:
                self.report('todo', rel)
                continue
            self.backup_file(dst)
            if os.path.islink(dst):
                os.unlink(dst)
            self.write_atomic(dst, open(src, 'rb').read(), mode)
            self.report('done', rel)

    def symlinks(self):
        self.section('Symlinks')
        for group, rel, target in SYMLINKS:
            if self.skipped(group):
                continue
            dst = self.p(rel)
            label = f'{rel} -> {target}'
            if os.path.islink(dst) and os.readlink(dst) == target:
                self.report('ok', label)
                continue
            if self.check:
                self.report('todo', label)
                continue
            if os.path.lexists(dst):
                if os.path.isdir(dst) and not os.path.islink(dst):
                    self.report('error', f'{label}: a directory is in the way')
                    continue
                # a plain copy of the link target (e.g. libLLVM-6.0.so) needs no backup
                resolved = os.path.join(os.path.dirname(dst), target) if not target.startswith('/') \
                    else self.p(target.lstrip('/'))
                if not (os.path.isfile(resolved) and not os.path.islink(dst)
                        and sha1_file(dst) == sha1_file(resolved)):
                    self.backup_file(dst)
                os.unlink(dst)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.symlink(target, dst)
            self.chown(dst, follow=False)
            self.report('done', label)

    # -- system --------------------------------------------------------------
    def system(self):
        if self.skipped('system'):
            return
        self.section('System config')
        # tesla user needs a real shell for `su tesla -c ...`
        passwd = self.p('etc/passwd')
        text = open(passwd).read()
        if re.search(r'^tesla:[^\n]*:/bin/sh$', text, re.M):
            self.report('ok', 'etc/passwd: tesla shell is /bin/sh')
        elif re.search(r'^tesla:[^\n]*:/bin/false$', text, re.M):
            if self.check:
                self.report('todo', 'etc/passwd: tesla shell /bin/false -> /bin/sh')
            else:
                new = re.sub(r'^(tesla:[^\n]*:)/bin/false$', r'\1/bin/sh', text, flags=re.M)
                self.write_atomic(passwd, new.encode(), os.stat(passwd).st_mode & 0o7777)
                self.report('done', 'etc/passwd: tesla shell /bin/false -> /bin/sh')
        else:
            self.report('error', 'etc/passwd: tesla user not found')

        for rel, uid in DIRECTORIES:
            d = self.p(rel)
            if os.path.isdir(d):
                self.report('ok', f'dir {rel}')
            elif self.check:
                self.report('todo', f'dir {rel}')
            else:
                os.makedirs(d)
                self.report('done', f'dir {rel}')
            if uid is not None and not self.check and os.path.isdir(d):
                self.chown(d, uid, uid)

        for rel, mode in DIR_MODES:
            d = self.p(rel)
            if not os.path.isdir(d):
                self.report('error', f'dir {rel} not found')
            elif os.stat(d).st_mode & 0o7777 == mode:
                self.report('ok', f'dir {rel} mode {mode:o}')
            elif self.check:
                self.report('todo', f'dir {rel} mode {mode:o}')
            else:
                os.chmod(d, mode)
                self.report('done', f'dir {rel} mode {mode:o}')

        for rel in EMPTY_FILES:
            f = self.p(rel)
            if os.path.lexists(f):  # may be a live bind mount - never replace
                self.report('ok', f'file {rel}')
            elif self.check:
                self.report('todo', f'file {rel}')
            else:
                open(f, 'a').close()
                os.chmod(f, 0o644)
                self.chown(f)
                self.report('done', f'file {rel}')

        for rel, owner in SETTINGS_FILES:
            self.settings_file(rel, owner)
        for group, rel, content, mode, *owner in TEXT_FILES:
            self.text_file(rel, content, mode, *owner)

    def settings_file(self, rel, owner):
        """Make sure SETTINGS_KEYS are set in an INI file, keeping everything else in it."""
        import configparser
        import io
        f = self.p(rel)
        conf = configparser.ConfigParser(interpolation=None)
        conf.optionxform = str  # keep key case
        if os.path.isfile(f):
            conf.read(f)
        missing = [(sec, k) for sec, keys in SETTINGS_KEYS.items() for k, v in keys.items()
                   if not conf.has_section(sec) or conf[sec].get(k) != v]
        if not missing:
            self.report('ok', rel)
            return
        if self.check:
            self.report('todo', f'{rel} (set {", ".join(k for _, k in missing)})')
            return
        for sec, keys in SETTINGS_KEYS.items():
            if not conf.has_section(sec):
                conf.add_section(sec)
            conf[sec].update(keys)
        out = io.StringIO()
        conf.write(out, space_around_delimiters=False)
        self.write_atomic(f, out.getvalue().encode(), 0o644, owner, owner)
        self.report('done', f'{rel} (set {", ".join(k for _, k in missing)})')

    def text_file(self, rel, content, mode, owner=0):
        f = self.p(rel)
        if os.path.isfile(f) and open(f, 'rb').read() == content.encode():
            self.report('ok', rel)
        elif self.check:
            self.report('todo', f'{rel} ({describe_change(f, content)})')
        else:
            if rel == 'startup.sh':
                self.backup_file(f)
            if rel == 'sbin/sv' and os.path.isfile(f) and open(f, 'rb').read(4) == b'\x7fELF':
                # the wrapper hands everything it doesn't handle to runit's sv
                shutil.copy2(f, self.p('sbin/sv.runit'))
            self.write_atomic(f, content.encode(), mode, owner, owner)
            self.report('done', rel)

    def audio(self):
        if self.skipped('audio'):
            return
        self.section('Audio')
        f = self.p('etc/asound.conf')
        text = open(f).read()
        if ASOUND_BLOCK in text:
            self.report('ok', 'etc/asound.conf: sound card block')
        elif self.check:
            self.report('todo', 'etc/asound.conf: sound card block')
        else:
            if ASOUND_MARK in text:     # an older version of the block: replace it
                text = text[:text.index(ASOUND_MARK)].rstrip('\n') + '\n'
            self.write_atomic(f, (text.rstrip('\n') + '\n\n' + ASOUND_BLOCK).encode(), 0o644)
            self.report('done', 'etc/asound.conf: sound card block')
        self.text_file('etc/asound-awe.conf', ASOUND_AWE, 0o644)

    def identity(self, vin, birthday):
        if self.skipped('identity') or not (vin or birthday):
            return
        self.section('Identity')
        for name, value in (('vin', vin), ('birthday', birthday)):
            if value:
                self.text_file(f'var/etc/{name}', value.strip() + '\n', 0o644)


# ---------------------------------------------------------------------------
# Getting payload files: payload/ -> download (sources.toml) -> build (src/)

# Files we can compile ourselves. Built with a modern gcc the result differs from the old image's
# binary, so these skip the hash check. fake_sandbox is linked statically: a dynamic build
# from a current host needs glibc >= 2.34, the chroot has 2.22.
BUILDABLE = {
    'usr/lib/icu_preload.so': ('src/icu_preload.c', ['-shared', '-fPIC', '-O2']),
    'usr/lib/egl_pixmap_shim.so': ('src/egl_pixmap_shim.c', ['-shared', '-fPIC', '-O2']),
    'usr/lib/cef_nosandbox.so': ('src/cef_nosandbox.c', ['-shared', '-fPIC', '-O2']),
}


class FetchError(Exception):
    pass


def payload_ok(rel, want):
    path = os.path.join(PAYLOAD, rel)
    if not os.path.isfile(path):
        return False
    if rel in BUILDABLE:  # rebuild when the source changed
        return os.path.getmtime(path) >= os.path.getmtime(os.path.join(KIT, BUILDABLE[rel][0]))
    return sha1_file(path) == want


def fetch_payload(rel, want):
    """Put payload/<rel> in place: download it per sources.toml, or build it from src/."""
    out = os.path.join(PAYLOAD, rel)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    if rel in BUILDABLE:
        source, flags = BUILDABLE[rel]
        cmd = ['gcc', *flags, '-o', out, os.path.join(KIT, source)]
        print(f'            building {rel}: {" ".join(cmd)}')
        try:
            subprocess.run(cmd, check=True)
        except (OSError, subprocess.CalledProcessError) as e:
            raise FetchError(f'build failed ({e})')
        check_glibc(out)
        return

    entry = load_sources().get(rel, {})
    url = entry.get('url', '').strip()
    if not url:
        raise FetchError(f'not in payload/ and no url in sources.toml [\"{rel}\"]')
    archive = download(url, entry.get('sha256', '').strip())
    member = entry.get('member', '').strip()
    data = extract(archive, member) if member else open(archive, 'rb').read()
    if sha1(data) != want:
        raise FetchError(f'file from {url}{" -> " + member if member else ""} has sha1 {sha1(data)}, '
                         f'expected {want} (wrong package version?)')
    with open(out, 'wb') as f:
        f.write(data)
    print(f'            fetched {rel} from {url}')


def check_glibc(path, limit=(2, 22)):
    """The firmware has glibc 2.22: a shim built on a newer host must not need newer symbol
    versions (e.g. __isoc23_strtol@GLIBC_2.38), or it won't load in the chroot."""
    try:
        out = subprocess.run(['objdump', '-T', path], capture_output=True, text=True).stdout
    except OSError:
        return      # no objdump: can't check
    newer = sorted({v for v in re.findall(r'GLIBC_(\d+\.\d+)', out)
                    if tuple(map(int, v.split('.'))) > limit}, key=lambda v: tuple(map(int, v.split('.'))))
    if newer:
        os.unlink(path)
        raise FetchError(f'{os.path.basename(path)} needs GLIBC_{newer[-1]}, the firmware has 2.22 '
                         f'(objdump -T shows which functions; see CLAUDE.md)')


def load_sources():
    if not os.path.exists(SOURCES):
        return {}
    import tomllib
    with open(SOURCES, 'rb') as f:
        return tomllib.load(f)


def download(url, sha256):
    os.makedirs(DOWNLOADS, exist_ok=True)
    path = os.path.join(DOWNLOADS, os.path.basename(url.split('?')[0]) or 'download')
    if not os.path.exists(path):
        print(f'            downloading {url}')
        try:
            with urllib.request.urlopen(url, timeout=60) as r, open(path + '.part', 'wb') as f:
                shutil.copyfileobj(r, f)
        except OSError as e:
            raise FetchError(f'download failed: {e}')
        os.replace(path + '.part', path)
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    got = h.hexdigest()
    if not sha256:
        print(f'            WARNING: no sha256 in sources.toml for {url}; it is {got}')
    elif got != sha256.lower():
        os.unlink(path)
        raise FetchError(f'sha256 mismatch for {url}: got {got}')
    return path


def extract(archive, member):
    """Read one file from a .deb, tar(.gz/.xz/.bz2/.zst) or .zip."""
    import io
    import tarfile
    import zipfile
    want = member.lstrip('./')
    if archive.endswith('.deb'):
        with open(archive, 'rb') as f:
            data = f.read()
        if not data.startswith(b'!<arch>\n'):
            raise FetchError(f'{archive} is not a .deb')
        pos = 8
        while pos + 60 <= len(data):  # ar archive: 60-byte headers
            name = data[pos:pos + 16].decode().strip().rstrip('/')
            size = int(data[pos + 48:pos + 58])
            body = data[pos + 60:pos + 60 + size]
            pos += 60 + size + (size & 1)
            if name.startswith('data.tar'):
                if name.endswith('.zst'):
                    body = subprocess.run(['zstd', '-dc'], input=body, capture_output=True, check=True).stdout
                return extract_tar(tarfile.open(fileobj=io.BytesIO(body)), want)
        raise FetchError(f'no data.tar in {archive}')
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            for name in z.namelist():
                if name.lstrip('./') == want:
                    return z.read(name)
        raise FetchError(f'{member} not found in {archive}')
    return extract_tar(tarfile.open(archive), want)


def extract_tar(tar, want):
    with tar:
        for m in tar:
            if m.name.lstrip('./') == want:
                while m.issym() or m.islnk():  # packages often ship the soname as a symlink
                    target = m.linkname if m.islnk() else os.path.normpath(
                        os.path.join(os.path.dirname(m.name), m.linkname))
                    m = tar.getmember(next(n for n in tar.getnames() if n.lstrip('./') == target.lstrip('./')))
                return tar.extractfile(m).read()
    raise FetchError(f'{want} not found in archive')


def sha1(data):
    return hashlib.sha1(data).hexdigest()


def sha1_file(path):
    h = hashlib.sha1()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def mounts_below(root):
    root = os.path.realpath(root)
    found = []
    with open('/proc/mounts') as f:
        for line in f:
            mp = line.split()[1].replace('\\040', ' ')
            if mp != root and mp.startswith(root + '/'):
                found.append(mp)
    return found


# The image remembers which kit built it (etc/mcu3-patchkit), so `./tesla check` can tell whether
# it is up to date without reading every file. The version is a hash over everything in the kit
# that ends up in the image: this script (which pins all payload hashes) and src/.
STAMP = 'etc/mcu3-patchkit'


def kit_version():
    h = hashlib.sha1()
    files = [os.path.abspath(__file__)] + sorted(
        os.path.join(KIT, 'src', f) for f in os.listdir(os.path.join(KIT, 'src')))
    for path in files:
        h.update(os.path.basename(path).encode() + b'\0')
        with open(path, 'rb') as f:
            h.update(f.read())
    return h.hexdigest()[:12]


def git_describe():
    try:
        r = subprocess.run(['git', '-C', KIT, 'log', '-1', '--format=%h %cs'], capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else ''
    except OSError:
        return ''


def read_stamp(root):
    """The stamp as a dict (kit, commit, date, skipped), or {} if there is none."""
    try:
        with open(os.path.join(root, STAMP)) as f:
            return dict(line.rstrip('\n').split('=', 1) for line in f if '=' in line)
    except OSError:
        return {}


def describe_change(path, content):
    """How a text file differs from what the kit writes, for --check."""
    if not os.path.isfile(path):
        return 'missing'
    try:
        have = open(path, encoding='utf-8').read().splitlines()
    except UnicodeDecodeError:
        return 'a binary file, replaced by a script'
    import difflib
    diff = [l for l in difflib.unified_diff(have, content.splitlines(), lineterm='', n=0)
            if l[:1] in '+-' and not l.startswith(('+++', '---'))]
    added = sum(l.startswith('+') for l in diff)
    return f'differs: {added} lines new/changed, {len(diff) - added} removed'


def list_all():
    print('Groups (use with --skip):')
    for g, d in GROUPS.items():
        print(f'  {g:9} {d}')
    print('\nBinary patches (ids also usable with --skip):')
    for b in BINARIES:
        print(f"  {b['path']}")
        for ident, group, off, old, new, desc in b['patches']:
            print(f'    {ident:24} [{group}] 0x{off:x}: {desc}')
        for ident, group, off, orig, old, desc in b.get('retired', []):
            print(f'    {ident:24} retired, restored where found: {desc}')
    print('\nRetired files (removed where found):')
    for group, rel, why in RETIRED_FILES:
        print(f'  [{group}] {rel}: {why}')
    print('\nPayload files:')
    for group, rel, mode, _ in PAYLOAD_FILES:
        print(f'  [{group}] {rel} ({oct(mode)})')
    print('\nSymlinks:')
    for group, rel, target in SYMLINKS:
        print(f'  [{group}] {rel} -> {target}')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('root', nargs='?', help='root of the MCU rootfs (e.g. the mounted mcu3.ext4)')
    ap.add_argument('--check', action='store_true', help='only report what is applied / missing')
    ap.add_argument('--list', action='store_true', help='list all groups, patches and files')
    ap.add_argument('--skip', action='append', default=[], metavar='GROUP|ID',
                    help='skip a group (core, cef, mesa, assets, system, identity) or a patch id; repeatable')
    ap.add_argument('--no-backup', action='store_true', help='do not keep <file>.orig backups')
    ap.add_argument('--force', action='store_true', help='run even if something is mounted inside ROOT')
    ap.add_argument('-q', '--quiet', action='store_true', help="only show what isn't already ok")
    ap.add_argument('--kit-version', action='store_true', help='print the kit version (hash) and exit')
    ap.add_argument('--vin', help='write /var/etc/vin')
    ap.add_argument('--birthday', help='write /var/etc/birthday')
    args = ap.parse_args()

    if args.list:
        list_all()
        return 0
    if args.kit_version:
        print(kit_version())
        return 0
    if not args.root:
        ap.error('ROOT is required (or use --list)')
    root = os.path.abspath(args.root)
    if not os.path.isfile(os.path.join(root, 'usr/tesla/UI/bin/QtCar')):
        sys.exit(f'{root} does not look like the MCU rootfs (usr/tesla/UI/bin/QtCar missing)')
    for s in args.skip:
        known = set(GROUPS) | {x[0] for b in BINARIES for x in b['patches']}
        if s not in known:
            sys.exit(f'--skip {s}: unknown group / patch id (see --list)')

    busy = mounts_below(root)
    if busy and not args.check and not args.force:
        sys.exit('Things are mounted inside the rootfs (QtCar running?). Stop it / run teardown first:\n  '
                 + '\n  '.join(busy) + '\n(or pass --force)')
    if os.geteuid() != 0 and not args.check:
        print('WARNING: not running as root - file ownership (root / tesla) will not be set.\n')

    r = Runner(root, args.check, not args.no_backup, args.skip, args.quiet)
    print(f'Rootfs: {root}' + ('  (check only)' if args.check else ''))
    r.binaries()
    r.retired_files()
    r.payload()
    r.symlinks()
    r.system()
    r.audio()
    r.identity(args.vin, args.birthday)

    c = r.counts
    print(f"\nSummary: {c['ok']} already ok, {c['done']} applied, {c['todo']} todo, {c['error']} errors")
    version, stamp = kit_version(), read_stamp(root)
    if args.check:
        if not stamp:
            print('Kit version: the image has no stamp (built by a kit older than 2026-09-24)')
        elif stamp.get('kit') == version and c['todo']:
            print(f"Kit version: {version} (applied {stamp.get('date', '?')}), but {c['todo']} item(s) differ "
                  f"from it (changed afterwards?)")
        elif stamp.get('kit') == version:
            print(f"Kit version: {version}, the image is up to date (applied {stamp.get('date', '?')})")
        else:
            print(f"Kit version: the image has {stamp.get('kit')} ({stamp.get('commit') or '?'}, applied "
                  f"{stamp.get('date', '?')}), this kit is {version}")
    elif not c['error'] and (c['done'] or stamp.get('kit') != version or stamp.get('skipped') != ','.join(args.skip)):
        import datetime
        text = (f'kit={version}\ncommit={git_describe()}\n'
                f'date={datetime.datetime.now().isoformat(timespec="seconds")}\n'
                f'skipped={",".join(args.skip)}\n')
        r.write_atomic(r.p(STAMP), text.encode(), 0o644)
        print(f'Kit version {version} written to /{STAMP}')
    return 1 if c['error'] else 0


if __name__ == '__main__':
    sys.exit(main())
