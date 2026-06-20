#!/usr/bin/env python3
"""Expiry reaper for the preview service.

Run periodically by a systemd timer. For every 'live' preview whose expiry has
passed it removes the local directory under LIVE_ROOT (reclaiming cloud disk)
and marks it 'expired'. The git archive (if any) is never touched. It also
retries any git pushes that are still pending.
"""
from __future__ import annotations

import os
import shutil
import time

import preview_archive
import preview_common as C


def reap() -> dict:
    C.init_db()
    now = int(time.time())
    reclaimed = []
    conn = C.connect()
    try:
        rows = conn.execute(
            "SELECT hash, topic, size_bytes FROM previews "
            "WHERE status='live' AND expires_at < ?",
            (now,),
        ).fetchall()
        for row in rows:
            h = row["hash"]
            path = os.path.join(C.LIVE_ROOT, h)
            shutil.rmtree(path, ignore_errors=True)
            conn.execute(
                "UPDATE previews SET status='expired', reclaimed_at=? WHERE hash=?",
                (now, h),
            )
            reclaimed.append({"hash": h, "topic": row["topic"], "freed_bytes": row["size_bytes"]})
        conn.commit()
    finally:
        conn.close()

    pushed = 0
    try:
        pushed = preview_archive.flush_pending()
    except Exception as e:  # noqa: BLE001
        print(f"[reaper] pending flush error: {e}", flush=True)

    return {"reclaimed": reclaimed, "pending_pushed": pushed}


def main():
    result = reap()
    n = len(result["reclaimed"])
    freed = sum(r["freed_bytes"] for r in result["reclaimed"])
    print(
        f"[reaper] {time.strftime('%Y-%m-%d %H:%M:%S')} reclaimed={n} "
        f"freed_bytes={freed} pending_pushed={result['pending_pushed']}",
        flush=True,
    )
    for r in result["reclaimed"]:
        print(f"[reaper]   - {r['hash']} ({r['topic']}) freed {r['freed_bytes']} B", flush=True)


if __name__ == "__main__":
    main()
