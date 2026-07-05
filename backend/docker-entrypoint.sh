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
    # A GPU render node passed through via docker-compose.hwaccel.yml is owned
    # by the host's render group, whose GID varies per host — grant "app"
    # membership in whatever group actually owns each node so the h264_qsv /
    # h264_vaapi probes can open it after privileges drop. No-op without /dev/dri.
    for node in /dev/dri/renderD*; do
        [ -e "$node" ] || continue
        gid=$(stat -c '%g' "$node")
        group=$(getent group "$gid" | cut -d: -f1)
        if [ -z "$group" ]; then
            group="render$gid"
            groupadd -g "$gid" "$group"
        fi
        usermod -aG "$group" app
    done
    exec gosu app "$@"
fi

# Already non-root (e.g. compose `user:` override): just run the command.
exec "$@"
