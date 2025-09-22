#!/usr/bin/env python3
"""
scanner_sorter.py

Streaming CLI to reorganize Uniden SDS100-style audio dumps into
YYYY/MM/DD folders without renaming files. Files remain untouched except
for relocation (copy by default; move only with --move). Designed for
NVMe → HDD workflows and million+ file archives.

Filename format supported: yyyy-mm-dd_hh-mm-ss.wav
Only the date portion (yyyy-mm-dd) is used for bucket folders.

Key features
- **Streaming**: does not preload file list (handles 1M+ files)
- Recursively scans source dir
- Sorts into dest/YYYY/MM/DD/
- Copy by default; `--move` to relocate and delete the source after cross-device copy
- Dry-run support
- Skip errors option
- Optional mark destination files read-only
- Progress bar (tqdm) without needing a fixed total
- Verbose/debug logging (-v); quiet mode; **--logfile** to tee logs to disk
- Worker threads (tune for SSD/NVMe: 4–8; HDD: 2–3)

Usage examples
--------------
# Dry-run on NVMe ingest; copy-by-default (safe)
python scanner_sorter.py D:\\Ingest E:\\Scanner --dry-run -v

# Real copy, skip errors, tee logs, 6 workers (NVMe sweet spot)
python scanner_sorter.py D:\\Ingest E:\\Scanner --skip-errors --workers 6 -v --logfile sort.log

# Move (destructive across devices), set read-only on dest
python scanner_sorter.py D:\\Ingest E:\\Scanner --move --readonly --workers 4

# Use file modified time instead of filename date
python scanner_sorter.py D:\\Ingest E:\\Scanner --date-source mtime
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import logging
from pathlib import Path
import re
import shutil
import stat
from datetime import datetime
from typing import Iterable, Optional, Tuple

try:
    from tqdm import tqdm  # type: ignore
except Exception:  # pragma: no cover
    tqdm = None

# Match yyyy-mm-dd_ anywhere in the filename, e.g., 2025-09-13_03-35-28.wav
SDS_REGEX = re.compile(r"(?P<Y>\d{4})-(?P<M>\d{2})-(?P<D>\d{2})_")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Sort SDS100 files into YYYY/MM/DD folders (streaming)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("src", type=Path, help="Source directory (recursively scanned)")
    p.add_argument("dst", type=Path, help="Destination root directory")

    # SAFER DEFAULT: copy unless --move is specified
    p.add_argument("--move", action="store_true", help="Move instead of copy (destructive across devices)")
    p.add_argument("--dry-run", action="store_true", help="Only log actions, no writes")
    p.add_argument("--readonly", action="store_true", help="Mark destination files read-only")
    p.add_argument("--skip-errors", action="store_true", help="Skip errors and continue")

    p.add_argument("--date-source", choices=["filename", "mtime"], default="filename",
                   help="Date source for bucketing")

    p.add_argument("--ext", action="append", default=[".wav"],
                   help="Only include files with these extensions (repeatable)")
    p.add_argument("--workers", type=int, default=1,
                   help="Parallel workers (SSD/NVMe: 4–8; HDD: 2–3)")
    p.add_argument("-v", "--verbose", action="count", default=0,
                   help="Increase verbosity (-v, -vv)")
    p.add_argument("-q", "--quiet", action="store_true", help="Minimal output")
    p.add_argument("--logfile", type=Path, help="Path to write a log file (tee console + file)")

    return p.parse_args()


def setup_logging(verbose: int, quiet: bool, logfile: Optional[Path]) -> None:
    if quiet:
        level = logging.ERROR
    else:
        level = logging.WARNING
        if verbose == 1:
            level = logging.INFO
        elif verbose >= 2:
            level = logging.DEBUG

    handlers = [logging.StreamHandler()]
    if logfile:
        logfile.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(logfile, encoding="utf-8"))

    logging.basicConfig(
        format="[%(levelname)s] %(message)s",
        level=level,
        handlers=handlers,
    )


def iter_files(src: Path, exts: list[str]) -> Iterable[Path]:
    exts_norm = {e.lower() for e in exts}
    for p in src.rglob("*"):
        if p.is_file() and (not exts_norm or p.suffix.lower() in exts_norm):
            yield p


def date_from_filename(name: str) -> Optional[datetime]:
    m = SDS_REGEX.search(name)
    if m:
        try:
            return datetime(int(m.group('Y')), int(m.group('M')), int(m.group('D')))
        except Exception:
            return None
    return None


def choose_date(p: Path, source: str) -> datetime:
    if source == "filename":
        dt = date_from_filename(p.name)
        if dt:
            return dt
        raise ValueError(f"No valid date in filename: {p.name}")
    return datetime.fromtimestamp(p.stat().st_mtime)


def dest_for(dst_root: Path, dt: datetime) -> Path:
    return dst_root / dt.strftime("%Y") / dt.strftime("%m") / dt.strftime("%d")


def ensure_readonly(path: Path) -> None:
    try:
        mode = path.stat().st_mode
        path.chmod(mode & ~stat.S_IWUSR & ~stat.S_IWGRP & ~stat.S_IWOTH)
    except Exception as e:
        logging.warning(f"Failed to set read-only on {path}: {e}")


def process_one(p: Path, dst_root: Path, *,
                date_source: str, do_move: bool, dry_run: bool, readonly: bool) -> Tuple[Path, Optional[Path], Optional[str]]:
    try:
        dt = choose_date(p, date_source)
        out_dir = dest_for(dst_root, dt)
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / p.name

        if dry_run:
            logging.info(f"[DRY] {'MOVE' if do_move else 'COPY'} {p} -> {dest}")
            return p, dest, None

        if do_move:
            shutil.move(str(p), str(dest))
        else:
            shutil.copy2(str(p), str(dest))

        if readonly:
            ensure_readonly(dest)

        return p, dest, None
    except Exception as e:
        return p, None, str(e)


def main() -> int:
    args = parse_args()
    setup_logging(args.verbose, args.quiet, args.logfile)

    if not args.src.exists() or not args.src.is_dir():
        logging.error(f"Source directory not found: {args.src}")
        return 2

    args.dst.mkdir(parents=True, exist_ok=True)

    # Streaming execution: no pre-list; optionally batch submissions when threaded
    pbar = tqdm(unit="file", dynamic_ncols=True) if tqdm and not args.quiet else None
    errors = 0

    def handle_result(res):
        nonlocal errors
        src, dest, err = res
        if err:
            errors += 1
            if args.skip_errors:
                logging.warning(f"SKIP {src} :: {err}")
            else:
                if pbar is not None:
                    pbar.close()
                logging.error(f"ERROR on {src}: {err}")
                raise SystemExit(1)
        if pbar is not None:
            pbar.update(1)

    files_iter = iter_files(args.src, args.ext)

    if args.workers <= 1:
        for f in files_iter:
            res = process_one(f, args.dst,
                              date_source=args.date_source,
                              do_move=bool(args.move),
                              dry_run=bool(args.dry_run),
                              readonly=bool(args.readonly))
            handle_result(res)
    else:
        BATCH = 2000  # limit in-flight futures to bound memory
        with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
            batch = []
            for f in files_iter:
                batch.append(f)
                if len(batch) >= BATCH:
                    futs = [ex.submit(process_one, x, args.dst,
                                      date_source=args.date_source,
                                      do_move=bool(args.move),
                                      dry_run=bool(args.dry_run),
                                      readonly=bool(args.readonly)) for x in batch]
                    for fut in cf.as_completed(futs):
                        handle_result(fut.result())
                    batch.clear()
            if batch:
                futs = [ex.submit(process_one, x, args.dst,
                                  date_source=args.date_source,
                                  do_move=bool(args.move),
                                  dry_run=bool(args.dry_run),
                                  readonly=bool(args.readonly)) for x in batch]
                for fut in cf.as_completed(futs):
                    handle_result(fut.result())

    if pbar is not None:
        pbar.close()

    if errors:
        logging.warning(f"Completed with {errors} file error(s)")
        return 1

    logging.info("Done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
