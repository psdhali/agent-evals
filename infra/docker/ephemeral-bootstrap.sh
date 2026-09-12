#!/bin/sh
# Bootstrap-container mount of the instance-store NVMe over the host docker
# data-root (/var/lib/docker) — Bottlerocket path at /.bottlerocket/rootfs.
# Idempotent: mount only when not already present; mkfs only when blank.
set -eu

ROOTFS=/.bottlerocket/rootfs
DATA="var/lib/docker"

# Where our mount target is on the host, for the already-mounted check.
TARGET="$ROOTFS/$DATA"

if findmnt "$TARGET" >/dev/null 2>&1; then
  echo "ephemeral-bootstrap: $TARGET already mounted; nothing to do"
  exit 0
fi

# Pick the first UNFORMATTED instance-store NVMe device.
# Device names: nvme<controller>n<namespace> (nvme1n1, nvme2n1 = ephemeral store);
# partitions append p<num> (nvme1n1p1). The old pattern `*[0-9]n[0-9]*` ALSO matched
# the device itself (nvme1n1 contains '1n1'), so every NVMe was skipped and docker
# stayed on the small EBS — the disk-full bug this container exists to fix. Match
# the p<num> partition suffix specifically, and rely on blkid to reject anything
# that already has a filesystem (the EBS root nvme0n1).
DEV=""
for d in /dev/nvme*n[0-9]; do
  [ -b "$d" ] || continue
  case "$d" in *p[0-9]*) continue ;; esac     # nvme1n1p1 partition - skip
  if ! blkid "$d" >/dev/null 2>&1; then
    DEV="$d"
    break
  fi
done
[ -n "$DEV" ] || { echo "ephemeral-bootstrap: no blank NVMe found; ok"; exit 0; }

# First boot: give it a filesystem (xfs for docker overlay2).
if ! blkid "$DEV" >/dev/null 2>&1; then
  mkfs.xfs -f "$DEV"
fi

mkdir -p "$TARGET"
mount "$DEV" "$TARGET"
echo "ephemeral-bootstrap: mounted $DEV over $TARGET"
