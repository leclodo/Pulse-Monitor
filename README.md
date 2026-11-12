<p align="center"><img src="docs/logo.png" width="128" alt="Pulse Monitor"></p>

<h1 align="center">Pulse Monitor</h1>

<p align="center"><b>English</b> · <a href="README.fr.md">Français</a></p>

A simple, lightweight dashboard for Windows: real-time PC performance (like Task Manager), a hardware sheet with what you need to know when upgrading parts, storage analysis, disk health and gaming session reports. 100% local, no telemetry.

Current version: **v0.7.29**

## Download

From the [Releases](https://github.com/leclodo/Pulse-Monitor/releases/latest) page:
- **`Pulse-Monitor-vX-Setup.exe`**: installer (Start menu, desktop shortcut, uninstall from Windows Settings). Data is stored in `%LOCALAPPDATA%\Pulse Monitor`.
- **`Pulse-Monitor-vX-portable.zip`**: no installation, unzip and run `Pulse Monitor.exe`. Data stays in the folder.

## Screenshots

![Performance](docs/performances.png)

| Components | Storage |
|---|---|
| ![Components](docs/composants.png) | ![Storage](docs/stockage.png) |
| **Analysis** | **Gaming** |
| ![Analysis](docs/analyse.png) | ![Gaming](docs/gaming.png) |

## What it shows

**Performance** (updated every second)
- CPU: usage, real clock speed, per-core load
- Memory: used / available, page file
- Graphics cards: usage, temperature, video memory
- Disks: activity, read / write, free space per drive
- Network: download / upload

**Components**
- CPU, motherboard + BIOS, RAM sticks (slots, speed, part number), graphics cards, disks (HDD/SSD, SATA/NVMe, health), Windows, network, displays
- **Fill in**: power supply, case, cooling, M.2/SATA ports (pre-filled lists)
- "Copy my specs summary" button to ask for advice on a forum

**Storage** (TreeSize-like)
- Folders sorted by size, breakdown by file type, largest files, "Show in Explorer"
- **Disk health**: status, temperature, remaining life (SSD), power-on hours, errors (read with administrator rights, on demand)
- **Temporary files**: Windows temp files, updates, browser / app / shader caches, thumbnails, crash reports, Recycle Bin. Nothing is deleted.

**Analysis**
- **1 h / 6 h / 12 h / 24 h / 7 days** history: CPU, GPU + temperature, RAM, VRAM (hover = values, click = the most demanding programs for CPU, RAM and GPU); drag a chart to go back in time, switched-off periods compacted
- Which component holds you back the most (after 10 min of heavy usage)

**Gaming**
- Games recognized within 10 to 30 s (Steam, Epic, GOG, Battle.net, Riot… or full screen with the graphics card under load)
- Report for each session: duration, GPU / CPU (average / max), temperature, RAM / VRAM, what limited, and charts
- FPS (average, 1% low, max) read from Windows display events, no extra program (one-time permission: "Performance Log Users" group)
- Find installed games, add any program manually, pause / resume tracking

## Usage

- **Bell 🔔**: live tips (saturated component, program responsible, startup programs…). ✕ to dismiss a tip.
- **Click a card title** (Performance): programs using that resource. **Ctrl + click**: hide the card (restore it in Settings).
- **Cards: Collapsed / Small / Large** buttons at the top of Performance; **⋮⋮** handle to reorder by drag and drop.
- **Settings**: FR / EN language (Windows language by default), 5 themes, start with Windows, keep in tray when closed, daily update check.
- **Close the window (✕)**: the app stays near the clock with a small icon (~30 MB). Click = reopen, right-click = Quit.
- **Desktop widget**: CPU, GPU + temperature, VRAM, RAM on one line (compact or bars). 📌 = pinned on top with click-through (hold Ctrl to move it), 🧲 snaps to screen edges. Configurable keyboard shortcut.
- **Updates**: click the version number → "Check for updates".
- Hover any value for a short explanation.

## Privacy

Everything stays on your PC: no account, no ads, no telemetry, nothing is sold or shared. The only Internet connection is the update check with GitHub, when you ask for it (or once a day if you turn the option on, off by default). Details in the app: 🛡 icon next to the version.

## Lightweight

The window is fully closed when you close it, measurements slow down when it's minimized or on a tab other than Performance, and WebView2 runs in lean mode (single render process, no GPU acceleration).

## From source

- **Run**: double-click **`Lancer Pulse Monitor.bat`** (installs `psutil`, `pywebview`, `pystray` and `Pillow` on first launch), or `python pulse_monitor.py`.
- **Build the installer and the portable version**: double-click **`Creer l'exe.bat`**. It installs PyInstaller (and Inno Setup if needed) and creates both files in the `release` folder.

## Limitations

- CPU temperature: not available without a special driver (same as Task Manager).
- Power supply, case, cooler and M.2/SATA ports can't be detected by software → fill them in under "Fill in".
- If the window displays badly (blank page): run with `--full-gpu`.
- If something goes wrong, see `pulse_monitor.log` (in the data folder: Settings → Privacy → "Open the data folder").
