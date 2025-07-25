#!/bin/bash

set -e

PROJECT_ROOT="$(dirname "$(readlink -e "$0")")/../.."
CONTRIB="$PROJECT_ROOT/contrib"
CONTRIB_SDIST="$CONTRIB/sdist"
BUILDDIR="$CONTRIB_SDIST/build"
DISTDIR="$PROJECT_ROOT/dist"

cd "$PROJECT_ROOT"

rm -rf "$BUILDDIR"
mkdir -p "$BUILDDIR" "$DISTDIR"


git_status=$(git status --porcelain)
if [ ! -z "$git_status" ]; then
    echo "$git_status"
    echo "git repo not clean, aborting"
    exit 1
fi


cd "$PROJECT_ROOT"
python3 -m build --sdist . --outdir "$BUILDDIR/dist1"


# the initial tar.gz is not reproducible, see https://github.com/pypa/setuptools/issues/2133
# so we untar, fix timestamps, and then re-tar
DISTNAME=$(find "$BUILDDIR/dist1/" -type f -name 'e_x-*.tar.gz' -printf "%f\\n")
DIST_BASENAME=$(basename --suffix ".tar.gz" "$DISTNAME")
mkdir -p "$BUILDDIR/dist2"
cd "$BUILDDIR/dist2"
tar -xzf "$BUILDDIR/dist1/$DISTNAME"
find -exec touch -h -d '2000-11-11T11:11:11+00:00' {} +
GZIP=-n tar --sort=name -czf "$DISTNAME" "$DIST_BASENAME/"
mv "$DISTNAME" "$DISTDIR/$DISTNAME"

