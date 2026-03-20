
#!/usr/bin/env python3
"""
SDSorter GUI v0.4.2

Desktop utility for:
- Sorting Uniden SDS100-style scanner recordings into YYYY/MM/DD folders
- Analyzing RIFF INFO metadata stored in WAV headers
- Exporting summary reports to CSV

Design goals:
- Descriptive function and variable names
- Background threads for long-running work
- Queue-based GUI updates so the interface stays responsive
- Cooperative cancel support for sort and analysis jobs
- Packaging-friendly structure for later PyInstaller builds

Optional dependencies:
    py -m pip install customtkinter matplotlib
"""

from __future__ import annotations

import csv
import json
import os
import queue
import re
import shutil
import stat
import struct
import sys
import threading
import time
import traceback
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

try:
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib.figure import Figure
    MATPLOTLIB_AVAILABLE = True
except Exception:
    FigureCanvasTkAgg = None  # type: ignore
    Figure = None  # type: ignore
    MATPLOTLIB_AVAILABLE = False

# ------------------------------------------------------------
# Optional UI layer: CustomTkinter if available, tkinter fallback if not
# ------------------------------------------------------------
try:
    import customtkinter as ctk
    from tkinter import filedialog, messagebox
    USING_CUSTOMTKINTER = True
except Exception:
    import tkinter as ctk  # type: ignore
    from tkinter import filedialog, messagebox, ttk
    USING_CUSTOMTKINTER = False


APP_NAME = "SDSorter"
APP_VERSION = "v0.4.2"
APP_SUBTITLE = "Scanner sorting and RIFF metadata analysis"

APPLICATION_SETTINGS_FILENAME = "sdsorter_settings.json"
APPLICATION_ERROR_LOG_FILENAME = "sdsorter_errors.log"

SDS_RECORDING_FILENAME_DATE_REGEX = re.compile(
    r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})_"
)

# ------------------------------------------------------------
# Data models
# ------------------------------------------------------------

@dataclass
class SortOperationOptions:
    source_directory_path: Path
    destination_directory_path: Path
    move_files_instead_of_copying: bool = False
    perform_dry_run_only: bool = False
    mark_destination_files_readonly: bool = False
    skip_individual_file_errors: bool = True
    date_source_mode: str = "filename"
    allowed_file_extensions: tuple[str, ...] = (".wav",)


@dataclass
class AnalysisOperationResults:
    analyzed_root_directory: str
    total_wav_files_found: int
    successfully_parsed_wav_files: int
    skipped_or_failed_wav_files: int
    channel_name_counts: Counter
    region_name_counts: Counter
    system_name_counts: Counter
    hourly_activity_counts: Counter
    comment_field_counts: Counter
    source_field_counts: Counter
    unit_identifier_counts: Counter
    daily_activity_counts: Counter


# ------------------------------------------------------------
# Sorter backend
# ------------------------------------------------------------

def extract_recording_date_from_filename(recording_filename: str) -> Optional[datetime]:
    """
    Extract a date from an SDS-style filename.

    Expected format:
        yyyy-mm-dd_hh-mm-ss.wav
    """
    filename_match = SDS_RECORDING_FILENAME_DATE_REGEX.search(recording_filename)
    if filename_match is None:
        return None

    try:
        return datetime(
            int(filename_match.group("year")),
            int(filename_match.group("month")),
            int(filename_match.group("day")),
        )
    except Exception:
        return None


def choose_recording_date_for_sorting(
    source_file_path: Path,
    date_source_mode: str,
) -> datetime:
    """
    Select the date used to bucket the file into YYYY/MM/DD folders.

    date_source_mode:
        - 'filename' -> parse from filename
        - 'mtime'    -> use file modification time
    """
    if date_source_mode == "filename":
        parsed_filename_date = extract_recording_date_from_filename(source_file_path.name)
        if parsed_filename_date is not None:
            return parsed_filename_date
        raise ValueError(f"No valid date found in filename: {source_file_path.name}")

    return datetime.fromtimestamp(source_file_path.stat().st_mtime)


def build_destination_directory_for_recording_date(
    destination_root_directory: Path,
    recording_date: datetime,
) -> Path:
    """Return destination root / YYYY / MM / DD."""
    return (
        destination_root_directory
        / recording_date.strftime("%Y")
        / recording_date.strftime("%m")
        / recording_date.strftime("%d")
    )


def apply_readonly_attribute_to_file(destination_file_path: Path) -> None:
    """Remove write permissions from the destination file."""
    existing_mode = destination_file_path.stat().st_mode
    destination_file_path.chmod(
        existing_mode & ~stat.S_IWUSR & ~stat.S_IWGRP & ~stat.S_IWOTH
    )


def iter_matching_source_files(
    source_root_directory: Path,
    allowed_file_extensions: list[str],
) -> Iterable[Path]:
    """Yield matching files recursively without preloading the whole tree."""
    normalized_extension_set = {extension.lower() for extension in allowed_file_extensions}

    for candidate_path in source_root_directory.rglob("*"):
        if candidate_path.is_file() and (
            not normalized_extension_set
            or candidate_path.suffix.lower() in normalized_extension_set
        ):
            yield candidate_path


def count_matching_source_files(
    source_root_directory: Path,
    allowed_file_extensions: list[str],
    progress_callback: Optional[Callable[[int], None]] = None,
) -> int:
    """Count matching source files for progress reporting."""
    matching_file_count = 0
    normalized_extension_set = {extension.lower() for extension in allowed_file_extensions}
    last_progress_callback_at = 0.0

    for candidate_path in source_root_directory.rglob("*"):
        if candidate_path.is_file() and (
            not normalized_extension_set
            or candidate_path.suffix.lower() in normalized_extension_set
        ):
            matching_file_count += 1

            if progress_callback is not None:
                now = time.perf_counter()
                if now - last_progress_callback_at > 0.1:
                    last_progress_callback_at = now
                    progress_callback(matching_file_count)

    if progress_callback is not None:
        progress_callback(matching_file_count)

    return matching_file_count


def run_sort_operation(
    sort_options: SortOperationOptions,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    scan_progress_callback: Optional[Callable[[int], None]] = None,
) -> tuple[int, int, bool]:
    """
    Sort matching files into YYYY/MM/DD folders.

    Returns:
        processed_file_count, error_count, was_cancelled
    """
    if not sort_options.source_directory_path.is_dir():
        raise FileNotFoundError(
            f"Source directory does not exist: {sort_options.source_directory_path}"
        )

    sort_options.destination_directory_path.mkdir(parents=True, exist_ok=True)

    total_matching_files = count_matching_source_files(
        sort_options.source_directory_path,
        list(sort_options.allowed_file_extensions),
        progress_callback=scan_progress_callback,
    )

    processed_file_count = 0
    encountered_error_count = 0

    def emit_sort_log(log_message: str) -> None:
        if log_callback is not None:
            log_callback(log_message)

    emit_sort_log(f"Found {total_matching_files:,} files. Starting processing...")

    for source_file_path in iter_matching_source_files(
        sort_options.source_directory_path,
        list(sort_options.allowed_file_extensions),
    ):
        if cancel_event is not None and cancel_event.is_set():
            emit_sort_log("Sort operation cancelled by user.")
            return processed_file_count, encountered_error_count, True

        try:
            selected_recording_date = choose_recording_date_for_sorting(
                source_file_path,
                sort_options.date_source_mode,
            )

            destination_day_directory = build_destination_directory_for_recording_date(
                sort_options.destination_directory_path,
                selected_recording_date,
            )
            destination_day_directory.mkdir(parents=True, exist_ok=True)

            destination_file_path = destination_day_directory / source_file_path.name

            if sort_options.perform_dry_run_only:
                action_name = (
                    "MOVE"
                    if sort_options.move_files_instead_of_copying
                    else "COPY"
                )

                if (processed_file_count + 1) % 100 == 0:
                    emit_sort_log(
                        f"[DRY RUN] {action_name} preview: {processed_file_count + 1:,}/{total_matching_files:,} files checked..."
                    )
            else:
                if sort_options.move_files_instead_of_copying:
                    shutil.move(str(source_file_path), str(destination_file_path))
                else:
                    shutil.copy2(str(source_file_path), str(destination_file_path))

                if sort_options.mark_destination_files_readonly:
                    apply_readonly_attribute_to_file(destination_file_path)

        except Exception as sort_exception:
            encountered_error_count += 1
            error_text = f"ERROR {source_file_path}: {sort_exception}"
            if sort_options.skip_individual_file_errors:
                emit_sort_log(error_text)
            else:
                raise

        processed_file_count += 1

        if progress_callback is not None and (
            processed_file_count == total_matching_files or processed_file_count % 25 == 0
        ):
            progress_callback(
                processed_file_count,
                total_matching_files,
                source_file_path.name,
            )

    return processed_file_count, encountered_error_count, False


# ------------------------------------------------------------
# RIFF INFO metadata backend
# ------------------------------------------------------------

def clean_riff_info_text(raw_riff_value_bytes: bytes) -> str:
    """
    Clean a RIFF INFO value.

    RIFF INFO values are often null-padded.
    Keep bytes only up to the first null terminator and normalize whitespace.
    """
    value_before_first_null = raw_riff_value_bytes.split(b"\x00", 1)[0]
    decoded_text = value_before_first_null.decode("utf-8", errors="replace").strip()
    return " ".join(decoded_text.split())


def read_riff_info_metadata(wav_file_path: str) -> dict[str, str]:
    """
    Read RIFF INFO metadata from a WAV file without loading the audio payload.

    Only the RIFF header and chunk headers are scanned until LIST/INFO data
    is found and parsed.
    """
    extracted_info_fields: dict[str, str] = {}

    with open(wav_file_path, "rb") as wav_file_handle:
        riff_header = wav_file_handle.read(12)
        if len(riff_header) < 12:
            return extracted_info_fields
        if riff_header[:4] != b"RIFF" or riff_header[8:12] != b"WAVE":
            return extracted_info_fields

        while True:
            chunk_header = wav_file_handle.read(8)
            if len(chunk_header) < 8:
                break

            chunk_identifier = chunk_header[:4]
            chunk_size = struct.unpack("<I", chunk_header[4:8])[0]

            if chunk_identifier == b"LIST":
                list_type = wav_file_handle.read(4)
                if len(list_type) < 4:
                    break

                bytes_remaining_in_list = chunk_size - 4

                if list_type == b"INFO":
                    info_chunk_end_position = (
                        wav_file_handle.tell() + bytes_remaining_in_list
                    )

                    while wav_file_handle.tell() + 8 <= info_chunk_end_position:
                        info_subchunk_header = wav_file_handle.read(8)
                        if len(info_subchunk_header) < 8:
                            break

                        info_key_bytes = info_subchunk_header[:4]
                        info_value_size = struct.unpack(
                            "<I", info_subchunk_header[4:8]
                        )[0]

                        info_value_bytes = wav_file_handle.read(info_value_size)
                        if len(info_value_bytes) < info_value_size:
                            break

                        info_key_name = info_key_bytes.decode(errors="replace")
                        cleaned_info_value = clean_riff_info_text(info_value_bytes)
                        extracted_info_fields[info_key_name] = cleaned_info_value

                        # RIFF chunks are word-aligned.
                        if info_value_size % 2 == 1:
                            wav_file_handle.seek(1, 1)

                    # Jump to the end of the INFO list if inner loop stopped early.
                    if wav_file_handle.tell() < info_chunk_end_position:
                        wav_file_handle.seek(
                            info_chunk_end_position - wav_file_handle.tell(),
                            1,
                        )
                else:
                    wav_file_handle.seek(bytes_remaining_in_list, 1)
            else:
                wav_file_handle.seek(chunk_size, 1)

            # Top-level chunks are also word-aligned.
            if chunk_size % 2 == 1:
                wav_file_handle.seek(1, 1)

    return extracted_info_fields


def extract_recording_datetime_from_filename(recording_filename: str) -> Optional[datetime]:
    """
    Extract full datetime from an SDS-style recording filename.

    Expected format:
        yyyy-mm-dd_hh-mm-ss.wav
    """
    try:
        return datetime.strptime(recording_filename[:19], "%Y-%m-%d_%H-%M-%S")
    except ValueError:
        return None


def extract_hour_from_recording_filename(recording_filename: str) -> Optional[int]:
    parsed_datetime = extract_recording_datetime_from_filename(recording_filename)
    if parsed_datetime is None:
        return None
    return parsed_datetime.hour


def increment_counter_if_value_is_meaningful(
    target_counter: Counter,
    candidate_value: str,
) -> None:
    """Only count non-empty, non-placeholder values."""
    if candidate_value and candidate_value != "Unknown":
        target_counter[candidate_value] += 1


def iter_wav_file_paths(root_directory: Path) -> Iterable[Path]:
    """Yield WAV files recursively without preloading them into memory."""
    for candidate_path in root_directory.rglob("*"):
        if candidate_path.is_file() and candidate_path.suffix.lower() == ".wav":
            yield candidate_path


def count_wav_files(root_directory: Path) -> int:
    """Count WAV files recursively for progress reporting."""
    wav_file_count = 0
    for _ in iter_wav_file_paths(root_directory):
        wav_file_count += 1
    return wav_file_count


def build_daily_activity_heatmap_matrix(
    daily_activity_counts: Counter,
) -> list[list[int]]:
    """
    Build a 12 x 31 matrix for a GitHub-style day-of-year view.

    Rows = months Jan..Dec
    Cols = days 1..31
    Values = number of recordings seen on that calendar day
    """
    heatmap_matrix = [[0 for _ in range(31)] for _ in range(12)]

    for date_value, activity_count in daily_activity_counts.items():
        month_index = date_value.month - 1
        day_index = date_value.day - 1
        if 0 <= month_index < 12 and 0 <= day_index < 31:
            heatmap_matrix[month_index][day_index] = activity_count

    return heatmap_matrix


def run_analysis_operation(
    analysis_root_directory: str,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> AnalysisOperationResults:
    """
    Analyze WAV files beneath a directory by reading RIFF INFO fields.

    Relevant RIFF INFO fields:
        INAM -> channel/title
        IGNR -> region
        IART -> system
        ICMT -> comment/group
        ISRC -> source/NAC
        ITCH -> unit identifier
    """
    analysis_root_path = Path(analysis_root_directory)
    if not analysis_root_path.is_dir():
        raise FileNotFoundError(
            f"Analysis root directory does not exist: {analysis_root_directory}"
        )

    total_wav_file_count = count_wav_files(analysis_root_path)

    channel_name_counter = Counter()
    region_name_counter = Counter()
    system_name_counter = Counter()
    hourly_activity_counter = Counter()
    comment_field_counter = Counter()
    source_field_counter = Counter()
    unit_identifier_counter = Counter()
    daily_activity_counter = Counter()

    successful_parse_count = 0
    skipped_or_failed_count = 0

    def emit_analysis_log(log_message: str) -> None:
        if log_callback is not None:
            log_callback(log_message)

    for wav_file_index, wav_file_path in enumerate(
        iter_wav_file_paths(analysis_root_path), start=1
    ):
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("Analysis cancelled by user.")

        extracted_recording_datetime = extract_recording_datetime_from_filename(
            wav_file_path.name
        )
        if extracted_recording_datetime is None:
            skipped_or_failed_count += 1
            if progress_callback is not None:
                progress_callback(
                    wav_file_index,
                    total_wav_file_count,
                    wav_file_path.name,
                )
            continue

        extracted_hour = extracted_recording_datetime.hour
        extracted_date = extracted_recording_datetime.date()

        hourly_activity_counter[extracted_hour] += 1
        daily_activity_counter[extracted_date] += 1

        try:
            riff_info_fields = read_riff_info_metadata(str(wav_file_path))

            channel_name = riff_info_fields.get("INAM", "Unknown") or "Unknown"
            region_name = riff_info_fields.get("IGNR", "Unknown") or "Unknown"
            system_name = riff_info_fields.get("IART", "Unknown") or "Unknown"
            comment_field_value = riff_info_fields.get("ICMT", "Unknown") or "Unknown"
            source_field_value = riff_info_fields.get("ISRC", "Unknown") or "Unknown"
            unit_identifier_value = riff_info_fields.get("ITCH", "Unknown") or "Unknown"

            increment_counter_if_value_is_meaningful(
                channel_name_counter, channel_name
            )
            increment_counter_if_value_is_meaningful(
                region_name_counter, region_name
            )
            increment_counter_if_value_is_meaningful(
                system_name_counter, system_name
            )
            increment_counter_if_value_is_meaningful(
                comment_field_counter, comment_field_value
            )
            increment_counter_if_value_is_meaningful(
                source_field_counter, source_field_value
            )
            increment_counter_if_value_is_meaningful(
                unit_identifier_counter, unit_identifier_value
            )

            successful_parse_count += 1

        except Exception as analysis_exception:
            skipped_or_failed_count += 1
            emit_analysis_log(f"SKIP {wav_file_path}: {analysis_exception}")

        if progress_callback is not None and (
            wav_file_index == total_wav_file_count or wav_file_index % 25 == 0
        ):
            progress_callback(
                wav_file_index,
                total_wav_file_count,
                wav_file_path.name,
            )

    return AnalysisOperationResults(
        analyzed_root_directory=analysis_root_directory,
        total_wav_files_found=total_wav_file_count,
        successfully_parsed_wav_files=successful_parse_count,
        skipped_or_failed_wav_files=skipped_or_failed_count,
        channel_name_counts=channel_name_counter,
        region_name_counts=region_name_counter,
        system_name_counts=system_name_counter,
        hourly_activity_counts=hourly_activity_counter,
        comment_field_counts=comment_field_counter,
        source_field_counts=source_field_counter,
        unit_identifier_counts=unit_identifier_counter,
        daily_activity_counts=daily_activity_counter,
    )


def write_counter_to_csv_file(
    output_file_path: str,
    first_column_header: str,
    counter_values: Counter,
) -> None:
    """Write a Counter to CSV."""
    with open(output_file_path, "w", newline="", encoding="utf-8") as csv_file_handle:
        csv_writer = csv.writer(csv_file_handle)
        csv_writer.writerow([first_column_header, "Count"])
        for item_name, item_count in counter_values.most_common():
            csv_writer.writerow([item_name, item_count])


def export_analysis_results_to_csv_files(
    analysis_results: AnalysisOperationResults,
    export_output_directory: str,
) -> list[str]:
    """Export all analysis outputs to CSV/TXT files."""
    os.makedirs(export_output_directory, exist_ok=True)

    written_export_paths: list[str] = []

    export_mapping = {
        "channels.csv": ("Channel", analysis_results.channel_name_counts),
        "regions.csv": ("Region", analysis_results.region_name_counts),
        "systems.csv": ("System", analysis_results.system_name_counts),
        "comments.csv": ("Comment", analysis_results.comment_field_counts),
        "sources.csv": ("Source", analysis_results.source_field_counts),
        "uids.csv": ("UID", analysis_results.unit_identifier_counts),
    }

    for export_filename, (column_header, counter_values) in export_mapping.items():
        export_path = os.path.join(export_output_directory, export_filename)
        write_counter_to_csv_file(export_path, column_header, counter_values)
        written_export_paths.append(export_path)

    hourly_export_path = os.path.join(export_output_directory, "hourly_activity.csv")
    with open(hourly_export_path, "w", newline="", encoding="utf-8") as hourly_csv_handle:
        csv_writer = csv.writer(hourly_csv_handle)
        csv_writer.writerow(["Hour", "Count"])
        for hour_value in range(24):
            csv_writer.writerow(
                [f"{hour_value:02d}:00", analysis_results.hourly_activity_counts[hour_value]]
            )
    written_export_paths.append(hourly_export_path)

    daily_activity_export_path = os.path.join(export_output_directory, "daily_activity.csv")
    with open(daily_activity_export_path, "w", newline="", encoding="utf-8") as daily_csv_handle:
        csv_writer = csv.writer(daily_csv_handle)
        csv_writer.writerow(["Date", "Count"])
        for date_value, activity_count in sorted(analysis_results.daily_activity_counts.items()):
            csv_writer.writerow([date_value.isoformat(), activity_count])
    written_export_paths.append(daily_activity_export_path)

    summary_export_path = os.path.join(export_output_directory, "summary.txt")
    with open(summary_export_path, "w", encoding="utf-8") as summary_handle:
        summary_handle.write("Scanner Corpus Analysis Summary\n")
        summary_handle.write("==============================\n")
        summary_handle.write(f"Root: {analysis_results.analyzed_root_directory}\n")
        summary_handle.write(f"Total WAV files found: {analysis_results.total_wav_files_found}\n")
        summary_handle.write(
            f"Successfully parsed: {analysis_results.successfully_parsed_wav_files}\n"
        )
        summary_handle.write(
            f"Skipped / failed: {analysis_results.skipped_or_failed_wav_files}\n"
        )
    written_export_paths.append(summary_export_path)

    summary_json_export_path = os.path.join(export_output_directory, "summary.json")
    with open(summary_json_export_path, "w", encoding="utf-8") as json_handle:
        json.dump(
            {
                "root": analysis_results.analyzed_root_directory,
                "total_wav_files_found": analysis_results.total_wav_files_found,
                "successfully_parsed_wav_files": analysis_results.successfully_parsed_wav_files,
                "skipped_or_failed_wav_files": analysis_results.skipped_or_failed_wav_files,
                "channels": dict(analysis_results.channel_name_counts),
                "regions": dict(analysis_results.region_name_counts),
                "systems": dict(analysis_results.system_name_counts),
                "comments": dict(analysis_results.comment_field_counts),
                "sources": dict(analysis_results.source_field_counts),
                "unit_identifiers": dict(analysis_results.unit_identifier_counts),
                "hourly_activity": {str(hour): analysis_results.hourly_activity_counts[hour] for hour in range(24)},
                "daily_activity": {date_value.isoformat(): count for date_value, count in sorted(analysis_results.daily_activity_counts.items())},
            },
            json_handle,
            indent=2,
        )
    written_export_paths.append(summary_json_export_path)

    return written_export_paths


# ------------------------------------------------------------
# About/version helpers
# ------------------------------------------------------------

def get_runtime_dependency_status() -> dict[str, str]:
    """Return a short runtime status map for the About dialog."""
    status_map: dict[str, str] = {}
    status_map["ui"] = "CustomTkinter" if USING_CUSTOMTKINTER else "tkinter fallback"
    status_map["matplotlib"] = "available" if MATPLOTLIB_AVAILABLE else "not installed"
    status_map["python"] = (
        f"{os.sys.version_info.major}."
        f"{os.sys.version_info.minor}."
        f"{os.sys.version_info.micro}"
    )
    return status_map


def build_about_dialog_text() -> str:
    """Build About dialog text."""
    runtime_status = get_runtime_dependency_status()

    return "\n".join(
        [
            f"{APP_NAME} {APP_VERSION}",
            APP_SUBTITLE,
            "",
            "Runtime:",
            f"  UI: {runtime_status['ui']}",
            f"  Matplotlib: {runtime_status['matplotlib']}",
            f"  Python: {runtime_status['python']}",
            "",
            "Capabilities:",
            "  - Sort recordings into YYYY/MM/DD folders",
            "  - Analyze RIFF INFO metadata from WAV files",
            "  - Export CSV and JSON reports",
            "  - Background job threading with cancel support",
            "  - Chart dashboard and day-of-year activity heatmap",
        ]
    )


# ------------------------------------------------------------
# Application settings and diagnostics helpers
# ------------------------------------------------------------

def get_application_storage_directory() -> Path:
    """Return the directory used for settings and logs."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def get_application_settings_file_path() -> Path:
    return get_application_storage_directory() / APPLICATION_SETTINGS_FILENAME


def get_application_error_log_file_path() -> Path:
    return get_application_storage_directory() / APPLICATION_ERROR_LOG_FILENAME


def load_application_settings() -> dict:
    settings_file_path = get_application_settings_file_path()
    if not settings_file_path.exists():
        return {}

    try:
        with open(settings_file_path, "r", encoding="utf-8") as settings_file_handle:
            loaded_settings = json.load(settings_file_handle)
            return loaded_settings if isinstance(loaded_settings, dict) else {}
    except Exception:
        return {}


def save_application_settings(settings_values: dict) -> None:
    settings_file_path = get_application_settings_file_path()
    with open(settings_file_path, "w", encoding="utf-8") as settings_file_handle:
        json.dump(settings_values, settings_file_handle, indent=2)


def append_exception_to_error_log(operation_name: str, exception_text: str, traceback_text: str) -> None:
    error_log_file_path = get_application_error_log_file_path()
    timestamp_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with open(error_log_file_path, "a", encoding="utf-8") as error_log_handle:
        error_log_handle.write(f"[{timestamp_text}] {operation_name}\n")
        error_log_handle.write(f"Error: {exception_text}\n")
        error_log_handle.write(traceback_text.rstrip() + "\n")
        error_log_handle.write("-" * 80 + "\n")


def format_progress_status_text(
    current_count: int,
    total_count: int,
    operation_started_at: Optional[float],
) -> str:
    elapsed_seconds = 0.0
    if operation_started_at is not None:
        elapsed_seconds = max(0.0, time.perf_counter() - operation_started_at)

    files_per_second = (current_count / elapsed_seconds) if elapsed_seconds > 0 else 0.0
    return f"{current_count}/{total_count} · {files_per_second:.1f} f/s · {elapsed_seconds:.1f}s"


# ------------------------------------------------------------
# Queue-based GUI helpers
# ------------------------------------------------------------

class JobMessageQueueLogger:
    """Push log lines into the GUI message queue."""

    def __init__(self, message_queue: queue.Queue, target_name: str):
        self.message_queue = message_queue
        self.target_name = target_name

    def __call__(self, log_message: str) -> None:
        self.message_queue.put(("log", self.target_name, log_message))


class JobMessageQueueProgressReporter:
    """Push progress updates into the GUI message queue."""

    def __init__(self, message_queue: queue.Queue, target_name: str):
        self.message_queue = message_queue
        self.target_name = target_name

    def __call__(
        self,
        current_count: int,
        total_count: int,
        current_item_name: str,
    ) -> None:
        self.message_queue.put(
            ("progress", self.target_name, current_count, total_count, current_item_name)
        )


class JobMessageQueueScanReporter:
    """Push counting/scan-phase updates into the GUI message queue."""

    def __init__(self, message_queue: queue.Queue, target_name: str):
        self.message_queue = message_queue
        self.target_name = target_name

    def __call__(self, files_found: int) -> None:
        self.message_queue.put(("scan_progress", self.target_name, files_found))


# ------------------------------------------------------------
# GUI widget aliases
# ------------------------------------------------------------

if USING_CUSTOMTKINTER:
    BaseWindow = ctk.CTk
    AppFrame = ctk.CTkFrame
    AppButton = ctk.CTkButton
    AppEntry = ctk.CTkEntry
    AppLabel = ctk.CTkLabel
    AppCheckBox = ctk.CTkCheckBox
    AppTabView = ctk.CTkTabview
    AppTextBox = ctk.CTkTextbox
    AppOptionMenu = ctk.CTkOptionMenu
    AppProgressBar = ctk.CTkProgressBar
else:
    BaseWindow = ctk.Tk
    AppFrame = ctk.Frame
    AppButton = ctk.Button
    AppEntry = ctk.Entry
    AppLabel = ctk.Label
    AppCheckBox = ctk.Checkbutton
    AppTabView = None
    AppTextBox = ctk.Text
    AppOptionMenu = None
    AppProgressBar = ttk.Progressbar


# ------------------------------------------------------------
# Main GUI application
# ------------------------------------------------------------

class SDSorterApplication(BaseWindow):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.geometry("1040x760")

        self.gui_message_queue: queue.Queue = queue.Queue()

        self.active_sort_thread: Optional[threading.Thread] = None
        self.active_analysis_thread: Optional[threading.Thread] = None

        self.sort_cancel_event = threading.Event()
        self.analysis_cancel_event = threading.Event()

        self.sort_job_is_running = False
        self.analysis_job_is_running = False

        self.latest_analysis_results: Optional[AnalysisOperationResults] = None

        self.sort_job_started_at: Optional[float] = None
        self.analysis_job_started_at: Optional[float] = None

        self.loaded_application_settings = load_application_settings()

        if USING_CUSTOMTKINTER:
            ctk.set_appearance_mode("dark")
            ctk.set_default_color_theme("blue")

        self._build_application_gui()
        self._apply_loaded_settings_to_widgets()
        self.protocol("WM_DELETE_WINDOW", self._handle_application_close)
        self.after(100, self._poll_gui_message_queue)

    # -------------------------
    # GUI construction
    # -------------------------

    def _build_application_gui(self) -> None:
        if USING_CUSTOMTKINTER:
            self.grid_columnconfigure(0, weight=1)
            self.grid_rowconfigure(0, weight=1)
            self.grid_rowconfigure(1, weight=0)

            self.main_tab_view = AppTabView(self)
            self.main_tab_view.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)

            self.sort_tab = self.main_tab_view.add("Sort")
            self.analysis_tab = self.main_tab_view.add("Analyze")

            self.footer_about_button = AppButton(
                self,
                text=f"{APP_NAME} {APP_VERSION}",
                command=self.show_about_dialog,
                fg_color="transparent",
                hover_color=("gray20", "gray80"),
                text_color=("gray75", "gray35"),
                border_width=0,
                width=140,
            )
            self.footer_about_button.grid(row=1, column=0, sticky="e", padx=12, pady=(0, 8))
        else:
            self.main_tab_view = ttk.Notebook(self)
            self.main_tab_view.pack(fill="both", expand=True, padx=12, pady=12)

            self.sort_tab = AppFrame(self.main_tab_view)
            self.analysis_tab = AppFrame(self.main_tab_view)

            self.main_tab_view.add(self.sort_tab, text="Sort")
            self.main_tab_view.add(self.analysis_tab, text="Analyze")

            self.footer_about_button = AppButton(
                self,
                text=f"{APP_NAME} {APP_VERSION}",
                command=self.show_about_dialog,
            )
            self.footer_about_button.pack(anchor="e", padx=12, pady=(0, 8))

        self._build_sort_tab()
        self._build_analysis_tab()

    def _build_sort_tab(self) -> None:
        parent_frame = self.sort_tab

        try:
            parent_frame.grid_columnconfigure(0, weight=0, minsize=220)
            parent_frame.grid_columnconfigure(1, weight=1, minsize=420)
            parent_frame.grid_columnconfigure(2, weight=0, minsize=190)
        except Exception:
            pass

        AppLabel(parent_frame, text="Source Folder").grid(
            row=0, column=0, sticky="w", padx=10, pady=8
        )
        self.sort_source_directory_entry = AppEntry(parent_frame)
        self.sort_source_directory_entry.grid(
            row=0, column=1, sticky="ew", padx=10, pady=8
        )
        AppButton(
            parent_frame,
            text="Browse",
            command=lambda: self._browse_for_directory(self.sort_source_directory_entry),
        ).grid(row=0, column=2, padx=10, pady=8)

        AppLabel(parent_frame, text="Destination Folder").grid(
            row=1, column=0, sticky="w", padx=10, pady=8
        )
        self.sort_destination_directory_entry = AppEntry(parent_frame)
        self.sort_destination_directory_entry.grid(
            row=1, column=1, sticky="ew", padx=10, pady=8
        )
        AppButton(
            parent_frame,
            text="Browse",
            command=lambda: self._browse_for_directory(
                self.sort_destination_directory_entry
            ),
        ).grid(row=1, column=2, padx=10, pady=8)

        self.sort_move_mode_variable = self._create_boolean_variable(False)
        self.sort_dry_run_variable = self._create_boolean_variable(False)
        self.sort_readonly_variable = self._create_boolean_variable(False)
        self.sort_skip_errors_variable = self._create_boolean_variable(True)

        self._create_checkbox_widget(
            parent_frame,
            "Move instead of copy",
            self.sort_move_mode_variable,
        ).grid(row=2, column=0, sticky="w", padx=10, pady=4)

        self._create_checkbox_widget(
            parent_frame,
            "Dry run",
            self.sort_dry_run_variable,
        ).grid(row=2, column=1, sticky="w", padx=10, pady=4)

        self._create_checkbox_widget(
            parent_frame,
            "Mark destination read-only",
            self.sort_readonly_variable,
        ).grid(row=3, column=0, sticky="w", padx=10, pady=4)

        self._create_checkbox_widget(
            parent_frame,
            "Skip errors and continue",
            self.sort_skip_errors_variable,
        ).grid(row=3, column=1, sticky="w", padx=10, pady=4)

        AppLabel(parent_frame, text="Date Source").grid(
            row=4, column=0, sticky="w", padx=10, pady=8
        )
        self.sort_date_source_widget = self._create_option_widget(
            parent_frame,
            ["filename", "mtime"],
            "filename",
        )
        self.sort_date_source_widget.grid(
            row=4, column=1, sticky="w", padx=10, pady=8
        )

        self.sort_action_button_frame = AppFrame(
            parent_frame,
            fg_color="transparent" if USING_CUSTOMTKINTER else None,
        )
        self.sort_action_button_frame.grid(
            row=5, column=0, sticky="w", padx=10, pady=12
        )

        self.sort_start_button = AppButton(
            self.sort_action_button_frame,
            text="Start Sorting",
            command=self.start_sort_job,
        )
        self.sort_start_button.grid(row=0, column=0, padx=(0, 10), pady=0, sticky="w")

        self.sort_cancel_button = AppButton(
            self.sort_action_button_frame,
            text="Cancel",
            command=self.cancel_sort_job,
        )
        self.sort_cancel_button.grid(row=0, column=1, padx=(0, 10), pady=0, sticky="w")
        self.sort_cancel_button.configure(state="disabled")

        self.sort_save_log_button = AppButton(
            self.sort_action_button_frame,
            text="Save Log",
            command=self.save_sort_log_to_file,
        )
        self.sort_save_log_button.grid(row=0, column=2, padx=(0, 10), pady=0, sticky="w")

        self.sort_progress_bar = self._create_progress_widget(parent_frame)
        self.sort_progress_bar.grid(row=5, column=1, sticky="ew", padx=10, pady=12)
        self._set_progress_widget_fraction(self.sort_progress_bar, 0)

        self.sort_progress_percent_label = AppLabel(
            parent_frame,
            text="0.0%",
            width=70,
            anchor="w",
        )
        self.sort_progress_percent_label.grid(row=5, column=2, padx=10, pady=12, sticky="w")

        self.sort_current_item_label = AppLabel(
            parent_frame,
            text="Idle",
            anchor="w",
            justify="left",
        )
        self.sort_current_item_label.grid(
            row=6, column=1, columnspan=2, padx=10, pady=(0, 4), sticky="w"
        )

        AppLabel(parent_frame, text="Sort Log").grid(
            row=7, column=0, sticky="w", padx=10, pady=(8, 0)
        )
        self.sort_log_text_widget = self._create_text_widget(parent_frame, height=350)
        self.sort_log_text_widget.grid(
            row=8, column=0, columnspan=3, sticky="nsew", padx=10, pady=8
        )

        try:
            parent_frame.grid_rowconfigure(8, weight=1)
        except Exception:
            pass

    def _build_analysis_tab(self) -> None:
        parent_frame = self.analysis_tab

        try:
            parent_frame.grid_columnconfigure(0, weight=0, minsize=220)
            parent_frame.grid_columnconfigure(1, weight=1, minsize=420)
            parent_frame.grid_columnconfigure(2, weight=0, minsize=190)
        except Exception:
            pass

        AppLabel(parent_frame, text="Audio Folder").grid(
            row=0, column=0, sticky="w", padx=10, pady=8
        )
        self.analysis_root_directory_entry = AppEntry(parent_frame)
        self.analysis_root_directory_entry.grid(
            row=0, column=1, sticky="ew", padx=10, pady=8
        )
        AppButton(
            parent_frame,
            text="Browse",
            command=lambda: self._browse_for_directory(
                self.analysis_root_directory_entry
            ),
        ).grid(row=0, column=2, padx=10, pady=8)

        self.analysis_action_button_frame = AppFrame(
            parent_frame,
            fg_color="transparent" if USING_CUSTOMTKINTER else None,
        )
        self.analysis_action_button_frame.grid(
            row=1, column=0, sticky="w", padx=10, pady=12
        )

        self.analysis_start_button = AppButton(
            self.analysis_action_button_frame,
            text="Analyze",
            command=self.start_analysis_job,
        )
        self.analysis_start_button.grid(row=0, column=0, padx=(0, 10), pady=0, sticky="w")

        self.analysis_cancel_button = AppButton(
            self.analysis_action_button_frame,
            text="Cancel",
            command=self.cancel_analysis_job,
        )
        self.analysis_cancel_button.grid(row=0, column=1, padx=(0, 10), pady=0, sticky="w")
        self.analysis_cancel_button.configure(state="disabled")

        self.analysis_export_button = AppButton(
            self.analysis_action_button_frame,
            text="Export CSV",
            command=self.export_latest_analysis,
        )
        self.analysis_export_button.grid(row=0, column=2, padx=(0, 10), pady=0, sticky="w")
        self.analysis_export_button.configure(state="disabled")

        self.analysis_show_charts_button = AppButton(
            self.analysis_action_button_frame,
            text="Show Charts",
            command=self.show_analysis_charts,
        )
        self.analysis_show_charts_button.grid(row=0, column=3, padx=(0, 10), pady=0, sticky="w")
        self.analysis_show_charts_button.configure(state="disabled")

        self.analysis_progress_bar = self._create_progress_widget(parent_frame)
        self.analysis_progress_bar.grid(
            row=1, column=1, sticky="ew", padx=10, pady=12
        )
        self._set_progress_widget_fraction(self.analysis_progress_bar, 0)

        self.analysis_progress_percent_label = AppLabel(
            parent_frame,
            text="0.0%",
            width=70,
            anchor="w",
        )
        self.analysis_progress_percent_label.grid(
            row=1, column=2, padx=10, pady=12, sticky="w"
        )

        self.analysis_current_item_label = AppLabel(
            parent_frame,
            text="Idle",
            anchor="w",
            justify="left",
        )
        self.analysis_current_item_label.grid(
            row=2, column=1, columnspan=2, padx=10, pady=(0, 4), sticky="w"
        )

        AppLabel(parent_frame, text="Analysis Results").grid(
            row=3, column=0, sticky="w", padx=10, pady=(8, 0)
        )
        self.analysis_results_text_widget = self._create_text_widget(
            parent_frame,
            height=440,
        )
        self.analysis_results_text_widget.grid(
            row=4, column=0, columnspan=3, sticky="nsew", padx=10, pady=8
        )

        try:
            parent_frame.grid_rowconfigure(4, weight=1)
        except Exception:
            pass

    # -------------------------
    # Generic widget helpers
    # -------------------------

    def _create_boolean_variable(self, initial_value: bool):
        return ctk.BooleanVar(value=initial_value)

    def _create_checkbox_widget(self, parent_frame, text: str, variable):
        if USING_CUSTOMTKINTER:
            return AppCheckBox(parent_frame, text=text, variable=variable)
        return AppCheckBox(
            parent_frame,
            text=text,
            variable=variable,
            onvalue=True,
            offvalue=False,
        )

    def _create_option_widget(
        self,
        parent_frame,
        option_values: list[str],
        default_value: str,
    ):
        if USING_CUSTOMTKINTER:
            option_widget = AppOptionMenu(parent_frame, values=option_values)
            option_widget.set(default_value)
            return option_widget

        string_variable = ctk.StringVar(value=default_value)
        option_widget = ttk.Combobox(
            parent_frame,
            values=option_values,
            textvariable=string_variable,
            state="readonly",
        )
        option_widget.set(default_value)
        return option_widget

    def _get_option_widget_value(self, option_widget) -> str:
        try:
            return option_widget.get()
        except Exception:
            return "filename"

    def _create_progress_widget(self, parent_frame):
        if USING_CUSTOMTKINTER:
            return AppProgressBar(parent_frame)
        return AppProgressBar(parent_frame, orient="horizontal", mode="determinate")

    def _set_progress_widget_fraction(
        self,
        progress_widget,
        fraction_complete: float,
    ) -> None:
        bounded_fraction = max(0.0, min(1.0, fraction_complete))
        if USING_CUSTOMTKINTER:
            progress_widget.set(bounded_fraction)
        else:
            progress_widget["value"] = bounded_fraction * 100

    def _create_text_widget(self, parent_frame, height: int = 200):
        return AppTextBox(parent_frame, height=height)

    def _append_line_to_text_widget(self, text_widget, text_line: str) -> None:
        text_widget.insert("end", text_line + "\n")
        try:
            text_widget.see("end")
        except Exception:
            pass

    def _clear_text_widget(self, text_widget) -> None:
        text_widget.delete("1.0", "end")

    def _browse_for_directory(self, target_entry_widget) -> None:
        selected_directory = filedialog.askdirectory()
        if selected_directory:
            target_entry_widget.delete(0, "end")
            target_entry_widget.insert(0, selected_directory)
            self._persist_current_widget_settings()

    def show_about_dialog(self) -> None:
        messagebox.showinfo(
            f"{APP_NAME} {APP_VERSION}",
            build_about_dialog_text(),
        )

    def save_sort_log_to_file(self) -> None:
        save_path = filedialog.asksaveasfilename(
            title="Save Sort Log",
            defaultextension=".txt",
            filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")],
        )
        if not save_path:
            return

        try:
            log_contents = self.sort_log_text_widget.get("1.0", "end")
            with open(save_path, "w", encoding="utf-8") as log_output_handle:
                log_output_handle.write(log_contents)
        except Exception as save_exception:
            messagebox.showerror("Save failed", str(save_exception))


    # -------------------------
    # Chart helpers
    # -------------------------

    def _truncate_chart_label(self, label_text: str, maximum_length: int = 28) -> str:
        if len(label_text) <= maximum_length:
            return label_text
        return label_text[: maximum_length - 3] + "..."

    def _build_daily_activity_heatmap_matrix(
        self,
        analysis_results: AnalysisOperationResults,
    ) -> list[list[int]]:
        return build_daily_activity_heatmap_matrix(
            analysis_results.daily_activity_counts
        )

    def _style_matplotlib_axis(self, axis) -> None:
        axis.set_facecolor("#2b2b2b")
        axis.tick_params(colors="white")
        axis.xaxis.label.set_color("white")
        axis.yaxis.label.set_color("white")
        axis.title.set_color("white")
        for spine in axis.spines.values():
            spine.set_color("white")

    def _plot_counter_barh(
        self,
        axis,
        chart_title: str,
        counter_values: Counter,
        top_n: int = 10,
    ) -> None:
        most_common_values = counter_values.most_common(top_n)
        if not most_common_values:
            axis.text(0.5, 0.5, "No data", color="white", ha="center", va="center")
            axis.set_title(chart_title)
            self._style_matplotlib_axis(axis)
            return

        chart_labels = [
            self._truncate_chart_label(item_name)
            for item_name, _ in reversed(most_common_values)
        ]
        chart_counts = [item_count for _, item_count in reversed(most_common_values)]

        axis.barh(chart_labels, chart_counts)
        axis.set_title(chart_title)
        axis.set_xlabel("Count")
        self._style_matplotlib_axis(axis)

    def _plot_hourly_activity_chart(
        self,
        axis,
        analysis_results: AnalysisOperationResults,
    ) -> None:
        hour_labels = [f"{hour_value:02d}:00" for hour_value in range(24)]
        hourly_counts = [
            analysis_results.hourly_activity_counts[hour_value] for hour_value in range(24)
        ]
        axis.bar(range(24), hourly_counts)
        axis.set_title("Hourly Activity")
        axis.set_xlabel("Hour")
        axis.set_ylabel("Count")
        axis.set_xticks(range(24))
        axis.set_xticklabels(hour_labels, rotation=45, ha="right")
        self._style_matplotlib_axis(axis)

    def _plot_daily_activity_heatmap(
        self,
        axis,
        figure,
        analysis_results: AnalysisOperationResults,
    ) -> None:
        heatmap_matrix = self._build_daily_activity_heatmap_matrix(analysis_results)
        heatmap_image = axis.imshow(heatmap_matrix, aspect="auto", interpolation="nearest")
        axis.set_title("Daily Activity Heatmap")
        axis.set_xlabel("Day of Month")
        axis.set_ylabel("Month")
        axis.set_xticks(range(31))
        axis.set_xticklabels([str(day_value) for day_value in range(1, 32)], rotation=45, ha="right")
        axis.set_yticks(range(12))
        axis.set_yticklabels(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])
        self._style_matplotlib_axis(axis)
        figure.colorbar(heatmap_image, ax=axis, fraction=0.046, pad=0.04)

    def show_analysis_charts(self) -> None:
        if self.latest_analysis_results is None:
            messagebox.showinfo("No results", "Run an analysis first.")
            return

        if not MATPLOTLIB_AVAILABLE or Figure is None or FigureCanvasTkAgg is None:
            messagebox.showerror(
                "Charts unavailable",
                "Matplotlib is not installed. Install it with:\n\npy -m pip install matplotlib",
            )
            return

        chart_window = ctk.CTkToplevel(self) if USING_CUSTOMTKINTER else ctk.Toplevel(self)
        chart_window.title(f"{APP_NAME} Charts")
        chart_window.geometry("1400x980")

        if USING_CUSTOMTKINTER:
            chart_window.grid_columnconfigure(0, weight=1)
            chart_window.grid_rowconfigure(1, weight=1)

        chart_toolbar_frame = AppFrame(
            chart_window,
            fg_color="transparent" if USING_CUSTOMTKINTER else None,
        )
        if USING_CUSTOMTKINTER:
            chart_toolbar_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=10)
        else:
            chart_toolbar_frame.pack(fill="x", padx=10, pady=10)

        dashboard_figure = Figure(figsize=(16, 12), dpi=100)
        dashboard_figure.patch.set_facecolor("#2b2b2b")
        chart_axes = dashboard_figure.subplots(4, 2)

        self._plot_counter_barh(
            chart_axes[0][0],
            "Top Channels",
            self.latest_analysis_results.channel_name_counts,
            top_n=10,
        )
        self._plot_counter_barh(
            chart_axes[0][1],
            "Top Regions",
            self.latest_analysis_results.region_name_counts,
            top_n=10,
        )
        self._plot_counter_barh(
            chart_axes[1][0],
            "Top Systems",
            self.latest_analysis_results.system_name_counts,
            top_n=10,
        )
        self._plot_counter_barh(
            chart_axes[1][1],
            "Top Comments / Groups",
            self.latest_analysis_results.comment_field_counts,
            top_n=10,
        )
        self._plot_counter_barh(
            chart_axes[2][0],
            "Top Source / NAC Values",
            self.latest_analysis_results.source_field_counts,
            top_n=10,
        )
        self._plot_counter_barh(
            chart_axes[2][1],
            "Top Unit Identifiers",
            self.latest_analysis_results.unit_identifier_counts,
            top_n=10,
        )
        self._plot_hourly_activity_chart(
            chart_axes[3][0],
            self.latest_analysis_results,
        )
        self._plot_daily_activity_heatmap(
            chart_axes[3][1],
            dashboard_figure,
            self.latest_analysis_results,
        )

        dashboard_figure.tight_layout()

        def save_chart_dashboard_to_png() -> None:
            save_path = filedialog.asksaveasfilename(
                title="Save Chart Dashboard",
                defaultextension=".png",
                filetypes=[("PNG Files", "*.png"), ("All Files", "*.*")],
            )
            if not save_path:
                return
            try:
                dashboard_figure.savefig(save_path, facecolor=dashboard_figure.get_facecolor(), bbox_inches="tight")
                messagebox.showinfo("Saved", f"Chart dashboard saved to:\n{save_path}")
            except Exception as chart_save_exception:
                append_exception_to_error_log(
                    "save_chart_dashboard_to_png",
                    str(chart_save_exception),
                    traceback.format_exc(),
                )
                messagebox.showerror("Save failed", str(chart_save_exception))

        chart_save_button = AppButton(
            chart_toolbar_frame,
            text="Save PNG",
            command=save_chart_dashboard_to_png,
        )
        if USING_CUSTOMTKINTER:
            chart_save_button.grid(row=0, column=0, padx=(0, 10), pady=0, sticky="w")
        else:
            chart_save_button.pack(side="left", padx=(0, 10))

        chart_close_button = AppButton(
            chart_toolbar_frame,
            text="Close",
            command=chart_window.destroy,
        )
        if USING_CUSTOMTKINTER:
            chart_close_button.grid(row=0, column=1, padx=(0, 10), pady=0, sticky="w")
        else:
            chart_close_button.pack(side="left", padx=(0, 10))

        chart_canvas = FigureCanvasTkAgg(dashboard_figure, master=chart_window)
        chart_canvas.draw()

        chart_widget = chart_canvas.get_tk_widget()
        if USING_CUSTOMTKINTER:
            chart_widget.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        else:
            chart_widget.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    # -------------------------
    # Settings helpers
    # -------------------------

    def _persist_current_widget_settings(self) -> None:
        current_settings = {
            "sort_source_directory": self.sort_source_directory_entry.get().strip(),
            "sort_destination_directory": self.sort_destination_directory_entry.get().strip(),
            "analysis_root_directory": self.analysis_root_directory_entry.get().strip(),
            "sort_move_mode": bool(self.sort_move_mode_variable.get()),
            "sort_dry_run_mode": bool(self.sort_dry_run_variable.get()),
            "sort_readonly_mode": bool(self.sort_readonly_variable.get()),
            "sort_skip_errors_mode": bool(self.sort_skip_errors_variable.get()),
            "sort_date_source_mode": self._get_option_widget_value(self.sort_date_source_widget),
        }

        try:
            save_application_settings(current_settings)
        except Exception:
            pass

    def _apply_loaded_settings_to_widgets(self) -> None:
        loaded_settings = self.loaded_application_settings or {}

        if loaded_settings.get("sort_source_directory"):
            self.sort_source_directory_entry.insert(0, loaded_settings["sort_source_directory"])

        if loaded_settings.get("sort_destination_directory"):
            self.sort_destination_directory_entry.insert(0, loaded_settings["sort_destination_directory"])

        if loaded_settings.get("analysis_root_directory"):
            self.analysis_root_directory_entry.insert(0, loaded_settings["analysis_root_directory"])

        self.sort_move_mode_variable.set(bool(loaded_settings.get("sort_move_mode", False)))
        self.sort_dry_run_variable.set(bool(loaded_settings.get("sort_dry_run_mode", False)))
        self.sort_readonly_variable.set(bool(loaded_settings.get("sort_readonly_mode", False)))
        self.sort_skip_errors_variable.set(bool(loaded_settings.get("sort_skip_errors_mode", True)))

        saved_date_source_mode = loaded_settings.get("sort_date_source_mode", "filename")
        try:
            self.sort_date_source_widget.set(saved_date_source_mode)
        except Exception:
            pass

    def _handle_application_close(self) -> None:
        self._persist_current_widget_settings()
        self.destroy()

    # -------------------------
    # Sort job controls
    # -------------------------

    def start_sort_job(self) -> None:
        if self.sort_job_is_running:
            messagebox.showinfo("Sorter busy", "A sort job is already running.")
            return

        source_directory_path = Path(
            self.sort_source_directory_entry.get().strip().strip('"')
        )
        destination_directory_path = Path(
            self.sort_destination_directory_entry.get().strip().strip('"')
        )

        if not source_directory_path.is_dir():
            messagebox.showerror(
                "Invalid source",
                "Please select a valid source folder.",
            )
            return

        if not str(destination_directory_path):
            messagebox.showerror(
                "Invalid destination",
                "Please select a destination folder.",
            )
            return

        self._persist_current_widget_settings()

        sort_options = SortOperationOptions(
            source_directory_path=source_directory_path,
            destination_directory_path=destination_directory_path,
            move_files_instead_of_copying=bool(self.sort_move_mode_variable.get()),
            perform_dry_run_only=bool(self.sort_dry_run_variable.get()),
            mark_destination_files_readonly=bool(self.sort_readonly_variable.get()),
            skip_individual_file_errors=bool(self.sort_skip_errors_variable.get()),
            date_source_mode=self._get_option_widget_value(self.sort_date_source_widget),
        )

        self._clear_text_widget(self.sort_log_text_widget)
        self._set_progress_widget_fraction(self.sort_progress_bar, 0)
        self.sort_progress_percent_label.configure(text="0.0%")
        self.sort_current_item_label.configure(text="Scanning database, please wait...")
        self.sort_start_button.configure(state="disabled")
        self.sort_cancel_button.configure(state="normal")
        self.sort_cancel_event.clear()
        self.sort_job_is_running = True
        self.sort_job_started_at = time.perf_counter()

        queue_logger = JobMessageQueueLogger(self.gui_message_queue, "sort")
        queue_progress_reporter = JobMessageQueueProgressReporter(
            self.gui_message_queue,
            "sort",
        )
        queue_scan_reporter = JobMessageQueueScanReporter(
            self.gui_message_queue,
            "sort",
        )

        def sort_worker() -> None:
            try:
                (
                    processed_count,
                    error_count,
                    was_cancelled,
                ) = run_sort_operation(
                    sort_options,
                    progress_callback=queue_progress_reporter,
                    log_callback=queue_logger,
                    cancel_event=self.sort_cancel_event,
                    scan_progress_callback=queue_scan_reporter,
                )
                self.gui_message_queue.put(
                    ("sort_done", processed_count, error_count, was_cancelled)
                )
            except Exception as sort_exception:
                self.gui_message_queue.put(
                    ("sort_error", str(sort_exception), traceback.format_exc())
                )

        self.active_sort_thread = threading.Thread(target=sort_worker, daemon=True)
        self.active_sort_thread.start()

    def cancel_sort_job(self) -> None:
        if self.sort_job_is_running:
            self.sort_cancel_event.set()
            self.sort_current_item_label.configure(text="Cancelling...")
            self.sort_cancel_button.configure(state="disabled")

    # -------------------------
    # Analysis job controls
    # -------------------------

    def start_analysis_job(self) -> None:
        if self.analysis_job_is_running:
            messagebox.showinfo("Analyzer busy", "An analysis job is already running.")
            return

        analysis_root_directory = self.analysis_root_directory_entry.get().strip().strip('"')
        if not Path(analysis_root_directory).is_dir():
            messagebox.showerror(
                "Invalid folder",
                "Please select a valid audio folder.",
            )
            return

        self._persist_current_widget_settings()

        self._clear_text_widget(self.analysis_results_text_widget)
        self._set_progress_widget_fraction(self.analysis_progress_bar, 0)
        self.analysis_progress_percent_label.configure(text="0.0%")
        self.analysis_current_item_label.configure(text="Running...")
        self.analysis_start_button.configure(state="disabled")
        self.analysis_cancel_button.configure(state="normal")
        self.analysis_export_button.configure(state="disabled")
        self.analysis_show_charts_button.configure(state="disabled")
        self.analysis_cancel_event.clear()
        self.analysis_job_is_running = True
        self.analysis_job_started_at = time.perf_counter()
        self.latest_analysis_results = None

        queue_logger = JobMessageQueueLogger(self.gui_message_queue, "analysis")
        queue_progress_reporter = JobMessageQueueProgressReporter(
            self.gui_message_queue,
            "analysis",
        )

        def analysis_worker() -> None:
            try:
                analysis_results = run_analysis_operation(
                    analysis_root_directory,
                    progress_callback=queue_progress_reporter,
                    log_callback=queue_logger,
                    cancel_event=self.analysis_cancel_event,
                )
                self.gui_message_queue.put(("analysis_done", analysis_results))
            except Exception as analysis_exception:
                self.gui_message_queue.put(("analysis_error", str(analysis_exception), traceback.format_exc()))

        self.active_analysis_thread = threading.Thread(
            target=analysis_worker,
            daemon=True,
        )
        self.active_analysis_thread.start()

    def cancel_analysis_job(self) -> None:
        if self.analysis_job_is_running:
            self.analysis_cancel_event.set()
            self.analysis_current_item_label.configure(text="Cancelling...")
            self.analysis_cancel_button.configure(state="disabled")

    def export_latest_analysis(self) -> None:
        if self.latest_analysis_results is None:
            messagebox.showinfo("No results", "Run an analysis first.")
            return

        export_directory = filedialog.askdirectory()
        if not export_directory:
            return

        try:
            written_files = export_analysis_results_to_csv_files(
                self.latest_analysis_results,
                export_directory,
            )
            messagebox.showinfo("Export complete", "\n".join(written_files))
        except Exception as export_exception:
            append_exception_to_error_log(
                "export_latest_analysis",
                str(export_exception),
                traceback.format_exc(),
            )
            messagebox.showerror("Export failed", str(export_exception))

    # -------------------------
    # Analysis rendering
    # -------------------------

    def _render_analysis_results(
        self,
        analysis_results: AnalysisOperationResults,
    ) -> None:
        self._append_line_to_text_widget(
            self.analysis_results_text_widget,
            "=== Analysis Summary ===",
        )
        self._append_line_to_text_widget(
            self.analysis_results_text_widget,
            f"Root: {analysis_results.analyzed_root_directory}",
        )
        self._append_line_to_text_widget(
            self.analysis_results_text_widget,
            f"Total WAV files found: {analysis_results.total_wav_files_found}",
        )
        self._append_line_to_text_widget(
            self.analysis_results_text_widget,
            f"Successfully parsed: {analysis_results.successfully_parsed_wav_files}",
        )
        self._append_line_to_text_widget(
            self.analysis_results_text_widget,
            f"Skipped / failed: {analysis_results.skipped_or_failed_wav_files}",
        )
        self._append_line_to_text_widget(self.analysis_results_text_widget, "")

        self._append_line_to_text_widget(
            self.analysis_results_text_widget,
            "=== Top Channels ===",
        )
        for channel_name, channel_count in analysis_results.channel_name_counts.most_common(25):
            self._append_line_to_text_widget(
                self.analysis_results_text_widget,
                f"{channel_name}: {channel_count}",
            )
        self._append_line_to_text_widget(self.analysis_results_text_widget, "")

        self._append_line_to_text_widget(
            self.analysis_results_text_widget,
            "=== Regions ===",
        )
        for region_name, region_count in analysis_results.region_name_counts.most_common():
            self._append_line_to_text_widget(
                self.analysis_results_text_widget,
                f"{region_name}: {region_count}",
            )
        self._append_line_to_text_widget(self.analysis_results_text_widget, "")

        self._append_line_to_text_widget(
            self.analysis_results_text_widget,
            "=== Systems ===",
        )
        for system_name, system_count in analysis_results.system_name_counts.most_common():
            self._append_line_to_text_widget(
                self.analysis_results_text_widget,
                f"{system_name}: {system_count}",
            )
        self._append_line_to_text_widget(self.analysis_results_text_widget, "")

        self._append_line_to_text_widget(
            self.analysis_results_text_widget,
            "=== Activity by Hour ===",
        )
        for hour_value in range(24):
            self._append_line_to_text_widget(
                self.analysis_results_text_widget,
                f"{hour_value:02d}:00 - {analysis_results.hourly_activity_counts[hour_value]}",
            )
        self._append_line_to_text_widget(self.analysis_results_text_widget, "")

        self._append_line_to_text_widget(
            self.analysis_results_text_widget,
            "=== Daily Activity Heatmap ===",
        )
        self._append_line_to_text_widget(
            self.analysis_results_text_widget,
            "Use 'Show Charts' to view the full day-of-year style heatmap dashboard.",
        )
        self._append_line_to_text_widget(self.analysis_results_text_widget, "")

        self._append_line_to_text_widget(
            self.analysis_results_text_widget,
            "=== Top Source / NAC Values ===",
        )
        for source_value, source_count in analysis_results.source_field_counts.most_common(25):
            self._append_line_to_text_widget(
                self.analysis_results_text_widget,
                f"{source_value}: {source_count}",
            )
        self._append_line_to_text_widget(self.analysis_results_text_widget, "")

        self._append_line_to_text_widget(
            self.analysis_results_text_widget,
            "=== Top Unit Identifiers ===",
        )
        for unit_identifier, unit_count in analysis_results.unit_identifier_counts.most_common(25):
            self._append_line_to_text_widget(
                self.analysis_results_text_widget,
                f"{unit_identifier}: {unit_count}",
            )
        self._append_line_to_text_widget(self.analysis_results_text_widget, "")

        self._append_line_to_text_widget(
            self.analysis_results_text_widget,
            "=== Top Comments / Groups ===",
        )
        for comment_text, comment_count in analysis_results.comment_field_counts.most_common(25):
            self._append_line_to_text_widget(
                self.analysis_results_text_widget,
                f"{comment_text}: {comment_count}",
            )

    # -------------------------
    # Queue polling
    # -------------------------

    def _poll_gui_message_queue(self) -> None:
        try:
            while True:
                queued_message = self.gui_message_queue.get_nowait()
                message_type = queued_message[0]

                if message_type == "log":
                    _, target_name, log_message = queued_message
                    if target_name == "sort":
                        self._append_line_to_text_widget(
                            self.sort_log_text_widget,
                            log_message,
                        )
                    elif target_name == "analysis":
                        self._append_line_to_text_widget(
                            self.analysis_results_text_widget,
                            log_message,
                        )

                elif message_type == "scan_progress":
                    _, target_name, files_found = queued_message

                    if target_name == "sort":
                        self.sort_progress_percent_label.configure(text="0.0%")
                        self.sort_current_item_label.configure(
                            text=f"Scanning database... ({files_found:,} found)"
                        )

                elif message_type == "progress":
                    (
                        _,
                        target_name,
                        current_count,
                        total_count,
                        current_item_name,
                    ) = queued_message

                    if target_name == "sort":
                        progress_fraction = (
                            current_count / total_count if total_count else 0
                        )
                        self._set_progress_widget_fraction(
                            self.sort_progress_bar,
                            progress_fraction,
                        )
                        self.sort_progress_percent_label.configure(
                            text=f"{progress_fraction * 100:.1f}%"
                        )
                        self.sort_current_item_label.configure(
                            text=(
                                f"{format_progress_status_text(current_count, total_count, self.sort_job_started_at)}"
                                f" · {current_item_name}"
                            )
                        )
                    elif target_name == "analysis":
                        progress_fraction = (
                            current_count / total_count if total_count else 0
                        )
                        self._set_progress_widget_fraction(
                            self.analysis_progress_bar,
                            progress_fraction,
                        )
                        self.analysis_progress_percent_label.configure(
                            text=f"{progress_fraction * 100:.1f}%"
                        )
                        self.analysis_current_item_label.configure(
                            text=(
                                f"{format_progress_status_text(current_count, total_count, self.analysis_job_started_at)}"
                                f" · {current_item_name}"
                            )
                        )

                elif message_type == "sort_done":
                    (
                        _,
                        processed_count,
                        error_count,
                        was_cancelled,
                    ) = queued_message

                    self.sort_job_is_running = False
                    self.sort_job_started_at = None
                    self.sort_start_button.configure(state="normal")
                    self.sort_cancel_button.configure(state="disabled")

                    if was_cancelled:
                        self.sort_progress_percent_label.configure(text="—")
                        self.sort_current_item_label.configure(
                            text=f"Cancelled · {processed_count:,} files · {error_count} errors"
                        )
                        self._append_line_to_text_widget(
                            self.sort_log_text_widget,
                            f"Cancelled after {processed_count} files with {error_count} error(s).",
                        )
                    else:
                        self.sort_progress_percent_label.configure(text="100.0%")
                        self.sort_current_item_label.configure(
                            text=f"Done · {processed_count:,} files · {error_count} errors"
                        )
                        self._append_line_to_text_widget(
                            self.sort_log_text_widget,
                            f"Completed. Processed {processed_count} files with {error_count} error(s).",
                        )

                elif message_type == "sort_error":
                    _, error_text, traceback_text = queued_message
                    self.sort_job_is_running = False
                    self.sort_job_started_at = None
                    self.sort_start_button.configure(state="normal")
                    self.sort_cancel_button.configure(state="disabled")
                    self.sort_progress_percent_label.configure(text="—")
                    self.sort_current_item_label.configure(text="Failed")
                    append_exception_to_error_log("sort_operation", error_text, traceback_text)
                    messagebox.showerror("Sort failed", error_text)

                elif message_type == "analysis_done":
                    _, analysis_results = queued_message
                    self.analysis_job_is_running = False
                    self.analysis_job_started_at = None
                    self.analysis_start_button.configure(state="normal")
                    self.analysis_cancel_button.configure(state="disabled")
                    self.analysis_show_charts_button.configure(state="disabled")
                    self.analysis_export_button.configure(state="normal")
                    self.analysis_show_charts_button.configure(
                        state="normal" if MATPLOTLIB_AVAILABLE else "disabled"
                    )
                    self.latest_analysis_results = analysis_results

                    self.analysis_progress_percent_label.configure(text="100.0%")
                    self.analysis_current_item_label.configure(
                        text=(
                            "Done · "
                            f"{analysis_results.successfully_parsed_wav_files:,}/"
                            f"{analysis_results.total_wav_files_found:,}"
                        )
                    )

                    self._clear_text_widget(self.analysis_results_text_widget)
                    self._render_analysis_results(analysis_results)

                elif message_type == "analysis_error":
                    _, error_text, traceback_text = queued_message
                    self.analysis_job_is_running = False
                    self.analysis_job_started_at = None
                    self.analysis_start_button.configure(state="normal")
                    self.analysis_cancel_button.configure(state="disabled")

                    if "cancelled by user" in error_text.lower():
                        self.analysis_progress_percent_label.configure(text="—")
                        self.analysis_current_item_label.configure(text="Cancelled")
                    else:
                        self.analysis_progress_percent_label.configure(text="—")
                        self.analysis_current_item_label.configure(text="Failed")
                        append_exception_to_error_log("analysis_operation", error_text, traceback_text)
                        messagebox.showerror("Analysis failed", error_text)

        except queue.Empty:
            pass

        self.after(100, self._poll_gui_message_queue)


def main() -> None:
    application = SDSorterApplication()
    application.mainloop()


if __name__ == "__main__":
    main()
