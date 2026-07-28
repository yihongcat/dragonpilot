#!/usr/bin/env bash
set -e
set -x

# git diff --name-status origin/release3-staging | grep "^A" | less

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null && pwd)"

cd $DIR

BUILD_DIR=/data/openpilot
SOURCE_DIR="$(git rev-parse --show-toplevel)"

if [ -z "$RELEASE_BRANCH" ]; then
  echo "RELEASE_BRANCH is not set"
  exit 1
fi

BUILD_BRANCH=release-mici-staging

if [ -z "$SOURCE_BRANCH" ]; then
  echo "SOURCE_BRANCH is not set"
  exit 1
fi

# set git identity
source /data/identity.sh

echo "[-] Setting up repo T=$SECONDS"
rm -rf $BUILD_DIR
mkdir -p $BUILD_DIR
cd $BUILD_DIR
git init
git remote add origin https://github.com/dragonpilot/dev.git
git checkout --orphan $SOURCE_BRANCH

# do the files copy
echo "[-] copying files T=$SECONDS"
cd $SOURCE_DIR
cp -pR --parents $(./tools/release/release_files.py) $BUILD_DIR/

# in the directory
cd $BUILD_DIR

rm -f panda/board/obj/panda.bin.signed
rm -f panda/board/obj/panda_h7.bin.signed

VERSION=$(cat openpilot/common/version.h | awk -F[\"-]  '{print $2}')
echo "[-] committing version $VERSION T=$SECONDS"
git add -f .
git commit -a -m "dragonpilot v$VERSION release"

# Build and test before launch_chffrplus.sh creates the on-device package
# symlinks. SConstruct uses the same package roots for build subprocesses.
export PYTHONPATH="$BUILD_DIR:$BUILD_DIR/msgq_repo:$BUILD_DIR/opendbc_repo:$BUILD_DIR/rednose_repo:$BUILD_DIR/teleoprtc_repo:$BUILD_DIR/tinygrad_repo"
scons

scons -j$(nproc) panda/

# panda tici
rm -f panda_tici/board/obj/panda.bin.signed
rm -f panda_tici/board/obj/panda_h7.bin.signed
scons -j$(nproc) panda_tici/

# Ensure no submodules in release
if test "$(git submodule--helper list | wc -l)" -gt "0"; then
  echo "submodules found:"
  git submodule--helper list
  exit 1
fi
git submodule status

# Cleanup
find . -name '*.a' -delete
find . -name '*.o' -delete
find . -name '*.os' -delete
find . -name '*.pyc' -delete
find . -name 'moc_*' -delete
find . -name '__pycache__' -delete
rm -rf .sconsign.dblite Jenkinsfile tools/release/
rm -f openpilot/selfdrive/modeld/models/*.onnx*

# Mark as prebuilt release
touch prebuilt

# dragonpilot customized
find . -name '*.cc' -delete
find openpilot/selfdrive/ui/ -name '*.h' -delete
# Some test code is imported by runtime code, so don't remove all test folders.
find . -type d -name 'x86_64' -exec rm -rf {} +
find . -type d -name 'Darwin' -exec rm -rf {} +
rm -fr tinygrad_repo/docs/tinygrad_intro.pdf
rm -fr openpilot/cereal/gen/cpp/log.capnp.h
rm -fr tinygrad_repo/extra/hip_gpu_driver/gc_10_3_0_offset.h
rm -fr tinygrad_repo/extra/accel/tpu/logs/tpu_driver.t1v-n-852cd0d5-w-0.taylor.log.INFO.20210619-062914.26926.gz

# Add built files to git
git add -f .
git commit --amend -m "dragonpilot v$VERSION"

git branch -m $RELEASE_BRANCH

# Run tests
# cd $BUILD_DIR
# RELEASE=1 ./openpilot/selfdrive/test/test_onroad.py
# tools/test_runner.py openpilot/selfdrive/car/tests/test_car_interfaces.py

# echo "[-] pushing release T=$SECONDS"
# REFS=()
# for branch in ${RELEASE_BRANCH//,/ }; do
#   REFS+=("$BUILD_BRANCH:$branch")
# done
# git push -f origin "${REFS[@]}"

echo "[-] done T=$SECONDS"
