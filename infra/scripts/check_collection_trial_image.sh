#!/usr/bin/env bash
# Run only against a locally built image, before obtaining AWS credentials.
set -euo pipefail
test "$#" -eq 1
image="$1"
guards=(--rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges)

docker run "${guards[@]}" \
  -e FOODTECH_COLLECTION_RUN_MODE=private-rest-trial \
  -e FOODTECH_REST_TRIAL_ENABLED=true "$image" --help > /dev/null

# The image VOLUME must support CA materialization under UID 10001, even when
# the root filesystem is read-only. ECS must mount this same container path.
docker run "${guards[@]}" --entrypoint python "$image" -c '
import os, pathlib, stat, tempfile
assert os.getuid() == 10001
root = pathlib.Path("/run/foodtech-collection")
assert root.stat().st_uid == 10001
assert stat.S_IMODE(root.stat().st_mode) == 0o700
assert pathlib.Path(tempfile.gettempdir()) == root
with tempfile.TemporaryDirectory() as directory:
    assert pathlib.Path(directory).parent == root
    descriptor = os.open(str(pathlib.Path(directory) / "ca.pem"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    assert stat.S_IMODE((pathlib.Path(directory) / "ca.pem").stat().st_mode) == 0o600
'
