#!/usr/bin/env bash
# Prepare only a dedicated load container; never stop or reset another instance.
set -euo pipefail
project_root=$(cd "$(dirname "$0")/.." && pwd)
context_name=${CP4_DOCKER_CONTEXT:-colima-cp4-load}
container_name=${CP4_CONTAINER_NAME:-fabric-cp4-load-candidate}
staging=$(mktemp -d "${TMPDIR:-/tmp}/cp4-load-image.XXXXXX")
trap 'rm -rf "$staging"' EXIT
cd "$project_root"
uv export --frozen --format requirements-txt --no-hashes --no-emit-project --no-header \
    --output-file "$staging/requirements.txt" >/dev/null
cp deploy/cp4-load.Dockerfile "$staging/Dockerfile"
docker --context "$context_name" build --tag fabric-cp4-load:local "$staging"
# Git worktrees need their common repository metadata at its original absolute path.
common_git=$(git rev-parse --path-format=absolute --git-common-dir)
common_root=$(dirname "$common_git")
mount_args=(--mount "type=bind,src=$project_root,dst=$project_root")
if [[ "$common_root" != "$project_root" ]]; then
    mount_args=(--mount "type=bind,src=$common_root,dst=$common_root,readonly" "${mount_args[@]}")
fi
docker --context "$context_name" run --detach --name "$container_name" \
    --cpus 4 --memory 8g --memory-swap 8g \
    "${mount_args[@]}" \
    --workdir "$project_root" --env PYTHONPATH=src:. fabric-cp4-load:local
docker --context "$context_name" exec "$container_name" \
    git config --global --add safe.directory "$project_root"
docker --context "$context_name" inspect "$container_name" \
    --format '{{json .HostConfig}}'
docker --context "$context_name" exec "$container_name" \
    cat /sys/fs/cgroup/cpu.max /sys/fs/cgroup/memory.max
printf 'Prepared %s in Docker context %s. Run cp4_scale.py there after freezing the candidate.\n' \
    "$container_name" "$context_name"
