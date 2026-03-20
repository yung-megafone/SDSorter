<p align="center">
  <img src="media/sdsorter_logo1.png" alt="SDSorter" width="300"/>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-v0.4.2-blue" />
  <img src="https://img.shields.io/badge/platform-Windows%20%7C%20Linux-lightgrey" />
  <img src="https://img.shields.io/badge/license-MIT-green" />
</p>

<p align="center">
  <b>Scanner Recordings Sorter for the SDS100/200 (and beyond)</b><br>
  Created by <i>yung-megafone</i>
</p>

---

## Overview

Uniden SDS radios (and similar systems) generate **thousands of WAV files rapidly**.  
Managing them manually becomes unmanageable — Explorer slows down, SD cards fill, and archives turn into chaos.

**SDSorter automates that process.**

Point it at your ingest folder or SD card, and it organizes recordings into:

```

YYYY/MM/DD/

````

Now with a **full GUI, real-time metrics, and dataset analysis tools** — built for handling *hundreds of thousands to millions of files.*

---

## Features

### Core Sorting
- **High performance**: Streaming walker + multi-threaded I/O
- **Safe by default**: Copy unless explicitly set to move
- **Flexible formats**: WAV, MP3, M4A (timestamp-based)
- **Scanner-native support**: SDS100/200 naming (`YYYY-MM-DD_hh-mm-ss.wav`)

### GUI
- Modern interface (no CLI required)
- Real-time progress + stats
- One-click folder selection
- Built-in log viewer
- Safe job cancellation

### Live Metrics
- Files processed
- Files/sec (true rate)
- Elapsed time
- Stable UI (no shifting elements)

### Analysis Mode
- Scan datasets without modifying files
- Generate insights:
  - File counts
  - Time distribution
  - Density patterns
- Export CSV
- Built-in charts

### Reliability
- Handles **millions of files**
- Avoids:
  - Explorer slowdowns
  - UI freezing
  - Memory spikes
- Continues past errors

---

## Installation / Usage

### Windows (Recommended)

**Installer**
- Download: `SDSorter_Setup_v0.4.2.exe`
- Standard install

**Portable**
- Download: `SDSorter_Portable_v0.4.2.zip`
- Extract and run `SDSorter.exe`

---

### Cross-Platform (Python)

Requires Python 3.8+

```bash
pip install tqdm customtkinter matplotlib
````

Run GUI:

```bash
python sdsorter.py
```

Run CLI:

```bash
python sdsorter.py SRC_PATH DST_PATH [options]
```

---

## Quickstart (CLI)

Dry run (safe preview):

```powershell
python sdsorter.py D:\Ingest E:\Archive --dry-run -v
```

Real copy:

```powershell
python sdsorter.py D:\Ingest E:\Archive --workers 6 --skip-errors -v --logfile sort.log
```

Move (destructive):

```powershell
python sdsorter.py D:\Ingest E:\Archive --move --readonly --workers 4
```

Use modified time:

```powershell
python sdsorter.py D:\Ingest E:\Archive --date-source mtime
```

---

## Options

| Flag            | Description                                       |
| --------------- | ------------------------------------------------- |
| `--move`        | Move instead of copy (destructive across devices) |
| `--dry-run`     | Preview only                                      |
| `--readonly`    | Mark destination files read-only                  |
| `--skip-errors` | Continue on errors                                |
| `--date-source` | `filename` (default) or `mtime`                   |
| `--ext`         | Restrict extensions (default: `.wav`)             |
| `--workers`     | Parallel workers (SSD: 4–8, HDD: 2–3)             |
| `-v` / `-vv`    | Verbosity                                         |
| `-q`            | Quiet mode                                        |
| `--logfile`     | Output logs to file                               |

---

## Who is this for?

* SDS100/200 users overwhelmed with recordings
* SDR / scanner hobbyists archiving long-term
* Data hoarders managing massive datasets
* Anyone who’s ever said:

  > “I’ll sort it later…”

---

## Roadmap

* Channel / department-aware sorting
* SQLite indexing
* Config profiles
* Improved UI scaling + polish

---

## Author

Built out of frustration, caffeine, and way too many files
**yung-megafone**

---

## License

MIT — free to use, modify, and improve
