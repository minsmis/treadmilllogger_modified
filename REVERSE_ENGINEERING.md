# Treadmill Logger: reverse-engineering notes

## Project purpose
A Windows desktop application for reading treadmill encoder measurements over USB serial and recording them as CSV. The supplied implementation is a single-file Python/Tkinter application; the Windows executable is packaged separately with PyInstaller.

## Runtime and dependencies
- Python 3
- Tkinter (GUI)
- pyserial (serial port enumeration and communication)
- Standard library: csv, threading, queue, datetime, os, time
- Serial baud rate: 192000

## Data path
1. Tkinter GUI lets the user select up to six serial devices and assign optional IDs.
2. A `SerialWorker` thread per enabled port opens the port and reads newline-delimited text.
3. `_parse_line` accepts either `distance,speed` or `micros,distance,speed` numeric fields.
4. Parsed samples get a UTC receipt timestamp and elapsed time since Start.
5. Samples are sent to the UI queue for live position/status and, while recording, to a queue consumed by `LogWriter`.
6. `LogWriter` writes CSV rows, appending to existing files and rotating files at 524,288 rows.

## GUI controls
- Base File Name and Directory: output path stem and folder.
- Combined: one wide CSV with per-device columns.
- Per Device: one CSV per device/port.
- Rescan Ports: refresh COM-port choices.
- Auto-Assign ESP32-S2: selects matching Adafruit VID/PID `239A:80ED` or descriptions containing ESP32-S2/Feather.
- Start / Stop: begin or stop CSV logging; serial readers remain active to show live values.
- Read/Set ID: sends line commands `ID` and `ID <VALUE>` at 192000 baud.

## Serial protocol assumptions
Streaming input is newline-delimited ASCII/UTF-8 text. The parser expects comma-separated numbers:
- `distance_mm,speed_mm_s`
- `device_micros,distance_mm,speed_mm_s`

Non-numeric, blank, and other lines are ignored. ID commands are handled separately and are not part of the streaming parser.

## CSV output
Per-device columns: `timestamp_iso,elapsed_s,distance_mm,speed_mm_s,device_micros`.
Combined output starts with `timestamp_iso,elapsed_s`, then adds `<label>_position_mm`, `<label>_speed_mm_s`, and `<label>_micros` per selected device. Combined rows are sparse: a sample fills only the columns for the device that produced it.

Output filename rules:
- Combined: `<base>.csv`, then `<base>_2.csv`, etc. after row limit.
- Per device: `<base>_<device ID or port>.csv`, then numbered suffixes after row limit.
Existing files are appended after a confirmation prompt.

## Threading model
- Main Tkinter thread owns widgets and polls `ui_queue` every 200 ms.
- Serial workers run as daemon threads and reconnect every 2 seconds after errors.
- A writer thread drains a queue and closes files after Stop.
- The GUI uses a separate shared queue reference so samples are logged only while recording.

## Important boundaries / likely pitfalls
- The source is a generic serial logger and does not identify the Teensy by VID/PID; the ESP32 auto-assign button is only a convenience for a particular ESP32-S2 board.
- The app assumes the device firmware emits exactly the numeric CSV lines described above. Firmware output with labels or another field order will be ignored.
- The code uses a fixed baud rate of 192000.
- Device IDs are used for output filename keys and combined column labels; avoid characters Windows does not allow in filenames.
- Combined CSV headers are formed from IDs or DEV1..DEV6 at Start. Keep IDs stable across runs if appending to a prior combined file.
- The GUI catches serial worker exceptions and retries, but does not display the underlying exception detail.
- `stop_logging` signals the writer and clears its reference immediately; the writer is daemonized and may still be draining the queue briefly.

## Files
- `treadmill_logger.py`: copied source implementation for inspection and modification.
- `README_DEPLOY.md`: original packaging/run notes.
- `REVERSE_ENGINEERING.md`: this architecture and behavior summary.

No source modification or execution was performed as part of this analysis.

## Live graph extension
The modified app adds a separate Live Graph window with speed and direction against elapsed time. It displays the most recent 60 seconds and refreshes at a configurable maximum of 1–30 Hz. Incoming samples are not dropped for CSV; graph refresh is throttled independently. Direction comes from the sign of the firmware speed value: positive = forward (1), negative = backward (-1). Zero holds the last known direction; direction remains unknown until the first nonzero speed sample.
