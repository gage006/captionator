#!/bin/sh
set -e

# Started as root (the image default): take ownership of the mounted volumes the
# unprivileged "app" user must read/write — the /storage bind mount is created
# with host ownership, and a pre-existing model cache may be root-owned from an
# earlier (root-running) image — then drop privileges and exec the real command
# as "app". chown is metadata-only, so it stays fast even for large videos/models.
if [ "$(id -u)" = "0" ]; then
    for d in /storage "$HF_HOME"; do
        [ -d "$d" ] && chown -R app:app "$d" 2>/dev/null || true
    done
    exec gosu app "$@"
fi

# Already non-root (e.g. compose `user:` override): just run the command.
exec "$@"
