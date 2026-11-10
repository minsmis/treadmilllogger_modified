# Treadmill Logger

A Windows desktop logger for treadmill encoder data received over USB serial. The application can record up to six serial devices to CSV, display live position and speed/direction plots, use a separate Arduino Uno R3 as an optional TTL start/stop input, and export each completed session to an Excel workbook.

## Provenance and citation

This project modifies the Python logger published in the **Software** directory of Janelia Experimental Technology's [Treadmill-Interface repository](https://github.com/janelia-experimental-technology/Treadmill-Interface), specifically [`Software/treadmill_logger.py`](https://github.com/janelia-experimental-technology/Treadmill-Interface/blob/main/Software/treadmill_logger.py). The upstream logger provides the serial/Tkinter CSV logging foundation. This repository adds live speed/direction graphs, TTL-triggered recording through a separate Uno R3, Excel session export, and subsequent reliability and export fixes.

The upstream repository asks users to cite the treadmill interface design as:

> Janelia Experimental Technology. *Treadmill Interface* (Janelia 2017-055). https://doi.org/10.25378/janelia.33179270

For the software source attribution, cite:

> Janelia Experimental Technology. *Treadmill-Interface*, `Software/treadmill_logger.py`. GitHub. https://github.com/janelia-experimental-technology/Treadmill-Interface/blob/main/Software/treadmill_logger.py

See the upstream repository's [`hardware_license.txt`](https://github.com/janelia-experimental-technology/Treadmill-Interface/blob/main/hardware_license.txt) and project README for applicable terms. Attribution is not a substitute for checking permission to redistribute upstream-derived software or hardware designs.

## Features

- Reads up to six newline-delimited serial streams at 192000 baud.
- Accepts `distance_mm,speed_mm_s` or `device_micros,distance_mm,speed_mm_s` input lines.
- Records every parsed serial sample to CSV in combined or per-device mode.
- Shows live position and receiving status in the main window.
- Plots speed magnitude in mm/s and direction (`1` forward, `0` stopped, `-1` backward). Sampling frequency is configurable from 1 to 30 Hz; graph time starts at 0 when recording begins.
- Uses a separate Arduino Uno R3 serial port to start recording on TTL HIGH and stop on LOW.
- Creates a timestamped Excel workbook when recording stops. It includes raw treadmill data, sampled graph data, session information, and TTL events received during that session.
- Keeps the UI responsive under high-rate serial input by bounding the live display queues; the recording queue retains parsed samples for CSV output.

## Hardware

### Treadmill encoder interface

Connect the treadmill's LabMaker/Janelia interface board to the computer over USB and select its serial port in an enabled device row. The logger expects the firmware serial format documented above and uses a baud rate of 192000. The **Auto-Assign ESP32-S2** button is only a convenience for matching ESP32-S2 devices; a Teensy-based interface can be selected manually from its COM port.

### Optional TTL start/stop input

The TTL input uses a **separate Arduino Uno R3** running [`TTL_Sync_Uno/TTL_Sync_Uno.ino`](TTL_Sync_Uno/TTL_Sync_Uno.ino). It is not the Teensy treadmill interface.

1. Upload the sketch to the Uno R3 using Arduino IDE.
2. Connect a ground-referenced 0–5 V TTL signal to Uno D2 and connect signal ground to Uno GND. Add an external 10 kΩ pulldown between D2 and GND so an unconnected input stays LOW. An unconnected D2 input can float and appear to switch HIGH/LOW.
3. Connect the Uno by USB. In the app, enable **TTL start/stop**, choose the Uno port as **Sync port**, and wait for the listening status. Keep the treadmill interface selected in a device row on its own port.
4. TTL HIGH starts recording and TTL LOW stops it. The app writes the received TTL edges and timestamps to `<base>_sync.csv` and the session workbook's `SyncEvents` sheet.

Do not connect the 5 V TTL signal to a Teensy GPIO. If the TTL source and Uno must be electrically isolated, use an appropriate optocoupler input stage. Opening the Uno serial port resets it; arm the sync connection before the experiment starts.

## Run from source

Requirements: Windows, Python 3.9 or newer, and the Python Tkinter component.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python treadmill_logger.py
```

Dependencies are listed in [`requirements.txt`](requirements.txt): pyserial, Matplotlib, and openpyxl.

## Build the Windows executable

With the project virtual environment created, run:

```bat
build_exe.bat
```

The script installs the requirements and PyInstaller into `.venv`, then builds `dist\TreadmillLogger.exe`. The one-file executable includes the Python runtime and application dependencies; a separate Python installation is not needed to run it. USB serial drivers may still be required for the connected boards.

## Output files

Choose a base filename and output folder in the app.

- **Per Device**: `<base>_<device ID>.csv`, or `<base>_<COM port>.csv` when the worker has no ID. Rotated files receive a numeric suffix.
- **Combined**: `<base>.csv`, with columns for the enabled devices. Rotated files receive a numeric suffix.
- **TTL sidecar**: `<base>_sync.csv`.
- **Session workbook**: `<base>_YYYYMMDD_HHMMSS_session.xlsx`.

The workbook contains `SessionInfo`, raw treadmill data (`Raw_<device>` sheets or `TreadmillData`), `GraphData`, and `SyncEvents`. A `SyncEvents` sheet with only its header means that no TTL edges were recorded for that session. In per-device mode, the exporter checks both the configured device ID and COM-port filename so the relevant session rows are included if the worker was created before an ID change.

## Repository files

- `treadmill_logger.py` — modified application source.
- `TTL_Sync_Uno/TTL_Sync_Uno.ino` — optional Uno R3 TTL input firmware.
- `requirements.txt` — Python dependencies.
- `build_exe.bat` — Windows executable build script.
- `README_DEPLOY.md` — additional deployment notes.
- `REVERSE_ENGINEERING.md` — implementation notes from the initial adaptation.
