# ./tesla build-image (sourced by ./tesla): create or update the patched image.

build_usage() {
    cat <<EOF
${B}./tesla build-image$R0 [options] [-- patch kit options]

Brings the image ($IMAGE) up to date: applies the patch kit (mcu2-patchkit/mcu2_patch.py,
only what's missing) and installs the offline maps built by navigation/build-tiles.sh.

  (no options)     update the image: kit + maps (needs sudo; not while ./tesla start runs)
  --check          only show what an update would change
  --fresh          build a new image from the firmware dump in mcu2-original/ (copy, kit,
                   maps). The old image is kept as $(basename "$IMAGE").old. Settings QtCar
                   stored in the old image (car config, favorites, ...) start over.
  --size SIZE      size of a --fresh image (default 6G)
  --maps REGION    install these maps (navigation/work/tiles-REGION), e.g. US or EU
  --no-maps        leave the maps alone
  --sandbox        update sandbox/rootfs instead (no sudo; for ./sandbox.sh)
  -- ...           options for mcu2_patch.py, e.g. -- --skip cef --vin 5YJ3...

What the kit does: mcu2-patchkit/README.md; mcu2_patch.py --list lists every patch.
EOF
}

# tiles_version REGION: the version install-maps.sh gives these tiles
tiles_version() {
    echo "OSM-$1-$(date -r "navigation/work/tiles-$1/valhalla" +%Y%m%d)"
}

# pick_maps ROOT: the region to install, or nothing if the maps are up to date / none are built
pick_maps() {
    local root=$1 have region regions=()
    have=$(cat "$root/opt/navigon/VERSION" 2>/dev/null)
    for d in navigation/work/tiles-*/valhalla; do [ -d "$d" ] && regions+=("$(basename "$(dirname "$d")" | sed 's/^tiles-//')"); done
    if [ -n "$MAPS" ]; then
        [ -d "navigation/work/tiles-$MAPS/valhalla" ] || die "no tiles for $MAPS in navigation/work/tiles-$MAPS (navigation/build-tiles.sh)"
        [ "$(tiles_version "$MAPS")" = "$have" ] || echo "$MAPS"
        return
    fi
    [ ${#regions[@]} -gt 0 ] || return 0
    # the installed region, if newer tiles for it exist
    region=$(sed -n 's/^OSM-\([A-Z]*\)-.*/\1/p' <<<"$have")
    if [ -n "$region" ]; then
        if [ -d "navigation/work/tiles-$region/valhalla" ] && [ "$(tiles_version "$region")" != "$have" ]; then
            echo "$region"
        fi
        return
    fi
    [ ${#regions[@]} = 1 ] && echo "${regions[0]}" && return
    warn "maps for several regions built (${regions[*]}); pick one: --maps REGION" >&2
}

# update_root ROOT SUDO: kit + maps into a mounted rootfs
update_root() {
    local root=$1 sudo=$2 region
    step "Patch kit"
    $sudo python3 "$KIT" "$root" -q "${KIT_ARGS[@]}" | sed 's/^/    /'
    [ "${PIPESTATUS[0]}" = 0 ] || die "the patch kit reported errors (above)"
    [ "$MAPS" = none ] && return
    step "Maps"
    region=$(pick_maps "$root")
    if [ -n "$region" ]; then
        $sudo navigation/install-maps.sh "$root" "$region" | sed 's/^/    /'
    else
        local have
        have=$(cat "$root/opt/navigon/VERSION" 2>/dev/null)
        info "${have:-no maps installed}${have:+, up to date}$( [ -z "$have" ] && echo '; build some: navigation/README.md')"
    fi
}

check_root() {
    local root=$1 sudo=$2 region have
    step "Patch kit (what an update would change)"
    $sudo python3 "$KIT" "$root" --check -q "${KIT_ARGS[@]}" | sed 's/^/    /'
    [ "$MAPS" = none ] && return
    step "Maps"
    have=$(cat "$root/opt/navigon/VERSION" 2>/dev/null)
    region=$(pick_maps "$root")
    if [ -n "$region" ]; then
        info "would install $(tiles_version "$region") (installed: ${have:-none})"
    else
        info "${have:-no maps installed}${have:+, up to date}"
    fi
}

# with_image FUNC: mount the image if needed, run FUNC ROOT sudo, unmount again
with_image() {
    local mounted=0 rc
    if ! image_mounted >/dev/null; then
        [ -f "$IMAGE" ] || die "no image at $IMAGE; build one: ./tesla build-image --fresh"
        [ -z "$(image_mounted_elsewhere)" ] ||
            die "$IMAGE is mounted at $(image_mounted_elsewhere | head -1); unmount it first (sudo umount ...)"
        sudo mkdir -p "$CHROOT"
        sudo mount -o loop "$IMAGE" "$CHROOT" || die "couldn't mount $IMAGE"
        mounted=1
    fi
    ( "$@" "$CHROOT" sudo )
    rc=$?
    [ "$mounted" = 1 ] && unmount_image
    return $rc
}

build_fresh() {
    local orig=$TESLA_DIR/mcu2-original new=$IMAGE.new mnt
    # the dump's folder was called mcu3-original until 2026-09-25
    [ ! -d "$orig" ] && [ -d "$TESLA_DIR/mcu3-original" ] && orig=$TESLA_DIR/mcu3-original
    [ -f "$orig/usr/tesla/UI/bin/QtCar" ] || die "no firmware dump in $orig (usr/tesla/UI/bin/QtCar missing)"
    image_mounted >/dev/null && die "$CHROOT has an image mounted; unmount it first: sudo umount $CHROOT"
    if [ -e "$IMAGE" ]; then
        say "This builds a new image and renames the current one to $(basename "$IMAGE").old"
        say "(replacing an older .old). QtCar's stored settings start over."
        read -r -p "Continue? [y/N] " a
        case "$a" in y|Y|yes) ;; *) exit 1 ;; esac
    fi
    step "New image ($SIZE_NEW)"
    command rm -f "$new"
    truncate -s "$SIZE_NEW" "$new" && mkfs.ext4 -q -F -L mcu2 "$new" || die "mkfs failed"
    mnt=$(mktemp -d)
    sudo mount -o loop "$new" "$mnt" || die "couldn't mount $new"
    trap 'sudo umount "$mnt" 2>/dev/null; rmdir "$mnt"' EXIT
    step "Copying the firmware from ${orig##*/}/"
    sudo cp -a "$orig/." "$mnt/" || die "copy failed"
    ( update_root "$mnt" sudo ) || die "build failed; the old image is untouched ($new is incomplete)"
    sudo umount "$mnt" && rmdir "$mnt"
    trap - EXIT
    [ -e "$IMAGE" ] && mv -f "$IMAGE" "$IMAGE.old"
    mv "$new" "$IMAGE"
    step "Done: $IMAGE"
    [ -e "$IMAGE.old" ] && info "the previous image: $IMAGE.old (delete it when the new one works)"
}

cmd_build_image() {
    local mode=update
    MAPS="" SIZE_NEW=6G KIT_ARGS=()
    while [ $# -gt 0 ]; do
        case "$1" in
            --check) mode=check ;;
            --fresh) mode=fresh ;;
            --sandbox) mode=sandbox ;;
            --size) SIZE_NEW=${2:?--size needs a value}; shift ;;
            --maps) MAPS=${2:?--maps needs a region}; shift ;;
            --no-maps) MAPS=none ;;
            --) shift; KIT_ARGS=("$@"); break ;;
            -h|--help) build_usage; return 0 ;;
            *) die "unknown option: $1 (./tesla build-image --help)" ;;
        esac
        shift
    done
    if [ "$mode" != sandbox ] && [ "$mode" != check ]; then
        local pid
        pid=$(running_pid) && die "QtCar runs (pid $pid); stop it first: ./tesla stop"
    fi
    case "$mode" in
        sandbox)
            [ -d sandbox/rootfs/usr/tesla ] || die "no sandbox/rootfs (see CLAUDE.md)"
            podman ps -q 2>/dev/null | grep -q . && warn "podman containers run; the sandbox may see a mix of old and new files"
            update_root "$TESLA_DIR/sandbox/rootfs" "" ;;
        check)
            sudo_auth || exit 1
            with_image check_root ;;
        update)
            sudo_auth || exit 1
            [ -z "$(mounts_below)" ] || die "things are mounted inside $CHROOT (an earlier run?): ./tesla stop"
            with_image update_root || exit 1
            echo
            say "Image up to date. ./tesla check shows the version." ;;
        fresh)
            sudo_auth || exit 1
            build_fresh ;;
    esac
}
