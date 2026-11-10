# Treadmill Logger - Windows EXE Build

This project can be built into a single Windows 11 executable using PyInstaller.

## Prerequisites
- Windows 11
- Python 3.9+ installed and on PATH

## Build
From this folder:

```bat
build_exe.bat
```

The script installs the project dependencies and PyInstaller into the existing `.venv`, then generates `dist\TreadmillLogger.exe`.

## Run
Double-click:
```
dist\TreadmillLogger.exe
```

## Notes
- If Windows SmartScreen blocks the app, click **More info** → **Run anyway**.
- If you rebuild, delete `dist`/`build` if you want a clean output.

## Development environment

Create and activate a local virtual environment in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python treadmill_logger.py
```

The GUI uses Tkinter from the Python installation; pyserial is installed from `requirements.txt`.

## Live graphs

Click **Live Graph** to open real-time plots of speed magnitude (mm/s) and direction (1 forward, 0 stopped, -1 backward). **Sampling frequency (Hz)** sets the graph sampling rate from 1–30 Hz. The graph begins at 0 seconds when Start is pressed and shows the whole session.

If the graph window opens but shows no lines, enable the treadmill row, choose its serial port, and confirm the main window shows `connected` and `RX`. In PyCharm, set the project interpreter to `.venv\Scripts\python.exe` so it can import Matplotlib.

Graph update: speed is shown as magnitude (`abs(speed_mm_s)`), while direction uses the original sign (forward `1`, backward `-1`). Pressing Start resets graph time and data so the first sample is plotted from 0 seconds; the x-axis remains anchored at 0 for the session.


Stopped state behavior: the graph resamples connected devices at the selected sampling frequency. A zero-speed firmware record sets direction 0. If no encoder data arrives for 1 second, it infers a stopped state and plots speed 0 mm/s, direction 0. These resampled points are graph-only; CSV retains original serial records. Graph sampling runs only while recording; after Stop the graph freezes until the next Start.

The live UI queues are bounded and keep recent state so high-rate serial streams do not block the Tk event loop. CSV recording still queues every parsed serial record. Graph drawing is capped at 5 redraws per second and decimates displayed points while preserving the complete sampled graph data for export.

## TTL start/stop with a separate Arduino Uno R3

1. Open `TTL_Sync_Uno/TTL_Sync_Uno.ino` in Arduino IDE and upload it to the separate Uno R3.
2. For a ground-referenced 0–5V TTL output, connect the TTL signal to Uno D2 and signal ground to Uno GND. Add a 10k pulldown from D2 to GND. Use an optocoupler input stage if the signal source should be isolated or its ground relationship is unknown. Do not connect the 5V TTL line to the Teensy GPIO.
3. Connect the Uno by USB. In Treadmill Logger, enable **TTL start/stop**, select the Uno COM port as **Sync port**, and wait until the status says it is listening. Select and enable treadmill ports as usual.
4. A TTL rising edge (HIGH) starts recording for the enabled treadmill ports. A falling edge (LOW) stops recording. The Python logger writes edge receipt times and Uno timestamps to `<base>_sync.csv`.

The TTL line is a recording gate: keep it HIGH for the desired acquisition interval. The sync port must be separate from the treadmill serial ports. Arm the sync connection before starting an experiment, because opening an Uno serial port resets it. The Uno reports its current level after connection so a line already HIGH can still initiate recording.

TTL-triggered runs append to existing matching CSV files without a confirmation dialog so the trigger is not delayed. The _sync.csv sidecar records the Uno edge timestamp and host receipt time.

## Excel session export

When recording stops, the app creates a timestamped `<base>_<session>_session.xlsx` workbook. `TreadmillData` (or `Raw_<device>` sheets in per-device mode) contain the raw serial rows for that session, `GraphData` contains the sampled time/speed-magnitude/direction values used by the graph, and `SyncEvents` contains that session's TTL edges. CSV logging remains available as before. TTL-triggered runs append to an existing CSV without a confirmation dialog so the TTL edge is not delayed.

