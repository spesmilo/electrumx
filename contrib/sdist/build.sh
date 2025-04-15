#!/bin/bash
#
# env vars:
# - ELECBUILD_NOCACHE: if set, forces rebuild of docker image
# - ELECBUILD_COMMIT: if set, do a fresh clone and git checkout

set -e

PROJECT_ROOT="$(dirname "$(readlink -e "$0")")/../.."
PROJECT_ROOT_OR_FRESHCLONE_ROOT="$PROJECT_ROOT"
CONTRIB="$PROJECT_ROOT/contrib"
CONTRIB_SDIST="$CONTRIB/sdist"
DISTDIR="$PROJECT_ROOT/dist"
BUILD_UID=$(/usr/bin/stat -c %u "$PROJECT_ROOT")

export SOURCE_DATE_EPOCH=1530212462
export PYTHONHASHSEED=22


DOCKER_BUILD_FLAGS=""
if [ ! -z "$ELECBUILD_NOCACHE" ] ; then
    echo "ELECBUILD_NOCACHE is set. forcing rebuild of docker image."
    DOCKER_BUILD_FLAGS="--pull --no-cache"
fi

if [ -z "$ELECBUILD_COMMIT" ] ; then  # local dev build
    DOCKER_BUILD_FLAGS="$DOCKER_BUILD_FLAGS --build-arg UID=$BUILD_UID"
fi

echo "building docker image."
docker build \
    $DOCKER_BUILD_FLAGS \
    -t electrumx-builder-img \
    "$CONTRIB_SDIST"

# maybe do fresh clone
if [ ! -z "$ELECBUILD_COMMIT" ] ; then
    echo "ELECBUILD_COMMIT=$ELECBUILD_COMMIT. doing fresh clone and git checkout."
    FRESH_CLONE="/tmp/electrumx_build/fresh_clone/electrumx"
    rm -rf "$FRESH_CLONE" 2>/dev/null || ( echo "we need sudo to rm prev FRESH_CLONE." && sudo rm -rf "$FRESH_CLONE" )
    umask 0022
    git clone "$PROJECT_ROOT" "$FRESH_CLONE"
    cd "$FRESH_CLONE"
    git checkout "$ELECBUILD_COMMIT"
    PROJECT_ROOT_OR_FRESHCLONE_ROOT="$FRESH_CLONE"
else
    echo "not doing fresh clone."
fi

echo "building binary..."
# check uid and maybe chown. see #8261
if [ ! -z "$ELECBUILD_COMMIT" ] ; then  # fresh clone (reproducible build)
    if [ $(id -u) != "1000" ] || [ $(id -g) != "1000" ] ; then
        echo "need to chown -R FRESH_CLONE dir. prompting for sudo."
        sudo chown -R 1000:1000 "$FRESH_CLONE"
    fi
fi
docker run -it \
    --name electrumx-builder-cont \
    -v "$PROJECT_ROOT_OR_FRESHCLONE_ROOT":/opt/electrumx \
    --rm \
    --workdir /opt/electrumx/contrib/sdist \
    electrumx-builder-img \
    ./make_sdist.sh

# make sure resulting binary location is independent of fresh_clone
if [ ! -z "$ELECBUILD_COMMIT" ] ; then
    mkdir --parents "$DISTDIR/"
    cp -f "$FRESH_CLONE/dist"/* "$DISTDIR/"
fi
