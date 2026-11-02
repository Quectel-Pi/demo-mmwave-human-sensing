# demo-mmwave-human-sensing

Human sensing demo using mmWave radar technology. This project targets the Quectel Pi M2 and R60ABD1 respiration & sleep radar module.

[简体中文](README.zh-CN.md) | English

This demo leverages the UART1 kernel serdev driver on Quectel Pi M2 to receive and parse raw radar frames. A C program collects sensing events, a Python standard-library backend provides local web service, and an HTML dashboard visualizes respiration rate, heart rate, body movement, sleep metrics and historical sensing data.

## 1. Development Materials & Hardware

- Quectel Pi M2, running Debian 13, with M2 SDK and Ubuntu cross-compilation environment
- R60ABD1 mmWave radar module, HDMI display, keyboard & mouse, USB-A dual-head cable, DuPont wires
- The module uses 2.0 mm pin pitch, while regular DuPont cables are 2.54 mm. Verify stable contact before power-on.

UART parameter: 115200 baud, 8N1, no flow control, 3.3 V TTL signal. **Do NOT feed 5 V to signal pins.**
Allow at least 30 seconds for module initialization after power-up. Follow the official module guide for mounting orientation, tilt angle and detection distance. Measurement performance varies heavily with installation conditions.

## 2. Get Source Code

Grab the M2 SDK and this demo repository:
[https://github.com/Quectel-Pi/QPi-SDK/tree/M2-SDK](https://github.com/Quectel-Pi/QPi-SDK/tree/M2-SDK)
[https://github.com/Quectel-Pi/demo-mmwave-human-sensing](https://github.com/Quectel-Pi/demo-mmwave-human-sensing)

After cloning, place the demo folder under `m2/docs/examples/demo-mmwave-human-sensing/` for SDK build scripts to locate it.

```
m2/
├── kernel/                 # Kernel source (6.1.118) + build artifacts
├── overlay/
├── prebuilds/
├── tools/
├── toolchains/
├── skills/
├── docs/                   # Documentation & example projects
    ├── examples/demo-mmwave-human-sensing/
        ├── README.md               # English version
        ├── README.zh-CN.md         # Chinese version
        ├── Makefile                # Compile src/, output to build/
        ├── .gitignore              # Ignore build/, *.pyc, *.log, *.jsonl
        ├── src/                    # Core source files
            ├── kernel.patch            # Kernel driver, UAPI, SDK integration patch
            ├── quectel-r60abd1.config
            ├── main.c                  # Collector program entry
            ├── r60abd1.c / r60abd1.h   # Device access interface
            ├── ui_server.py            # Local web backend service
            └── r60abd1-ui.html        # Web dashboard
        └── build/                   # Auto-generated build output, not committed
            ├── demo-mmwave-human-sensing
            ├── ui_server.py           # Copied from src/ for ADB push
            ├── r60abd1-ui.html
            ├── libr60abd1.a
            ├── r60abd1_client.o
            └── uapi/linux/r60abd1.h
├── build.sh                # Source this script to register build commands & export cross-compile env
└── Makefile
```

## 3. Initial Build & Flash

### 3.1 Compile the project

```
cd ~/m2/

# 1. Install and check build dependencies
./build.sh buildenv
./build.sh check

# 2. Apply kernel patch
git apply --check docs/examples/demo-mmwave-human-sensing/src/kernel.patch &&
git apply docs/examples/demo-mmwave-human-sensing/src/kernel.patch

# 3. Compile kernel, user-space program, embed web assets and generate full firmware
./build.sh all

# 4. Main build outputs
~/m2/docs/examples/demo-mmwave-human-sensing/build/
├── demo-mmwave-human-sensing
├── ui_server.py
└── r60abd1-ui.html
```

### 3.2 Flash firmware

Connect your build PC and the board’s ADB port with the dual-head USB-A cable. Use Rockchip Download Tool for flashing:

1. Click Upgrade Firmware
2. Select firmware file
3. Choose the generated `update.img`
4. Hold the BOOT button + POWER button together for more than 3 seconds until the device enters LOADER flashing mode
5. Click Upgrade to start flashing
6. If only an ADB device is detected, click Switch and repeat step 4

### 3.3 Verify runtime environment

Run these commands to confirm driver loading:

#### On host via ADB

```
adb shell "dmesg | grep -i r60abd1"
adb shell "ls -l /dev/r60abd1"
adb shell "ls -l /opt/demo-mmwave-human-sensing"
```

#### Inside board shell

```
sudo chgrp "$(id -gn)" /dev/r60abd1
sudo chmod g+rw /dev/r60abd1
```

## 4. Run the Application

Default mode is real-time sensing. Visit `http://127.0.0.1:8765/` in browser.

```
cd ~
/opt/demo-mmwave-human-sensing/demo-mmwave-human-sensing
```

## 5. Incremental User-space Compilation

Use this workflow when you modify only user-space code and do NOT want to re-flash the whole firmware. **The patched kernel driver must already be flashed to the board.**

```
cd /home/cherin/data/QpiDemo/SDK_M2/m2
make -C docs/examples/demo-mmwave-human-sensing userspace
```

Push new binaries to the board:

```
adb push ./docs/examples/demo-mmwave-human-sensing/build/demo-mmwave-human-sensing /opt/demo-mmwave-human-sensing/
adb push ./docs/examples/demo-mmwave-human-sensing/build/ui_server.py /opt/demo-mmwave-human-sensing/
adb push ./docs/examples/demo-mmwave-human-sensing/build/r60abd1-ui.html /opt/demo-mmwave-human-sensing/
adb shell chmod 755 /opt/demo-mmwave-human-sensing/demo-mmwave-human-sensing
```

Restart the collector and hard-refresh the web page.

> 
> Note: Changes to HTML / Python files do not require C compilation. After editing files under `src/`, run `make` to sync them into `build/`, then push the updated files.

## 6. Web UI & Local Data Storage

Two switchable working modes: Real-time Mode and Sleep Mode. Light theme dashboard layout:

1. Real-time vital signs: human presence, respiration rate, heart rate, body movement amplitude, motion state, target distance and detection range. Displays `--` for absent persons or stale data (old values will not be retained).
2. Mode panel: Real-time mode renders respiration & heart waveforms; Sleep mode displays sleep staging, duration, sleep score and sleep report.
3. Local history viewer: Select 1h / 6h / 12h / 24h or custom time window. Shows presence timeline and curves of respiration, heart rate and movement. Light shaded bands represent min–max value range in the selected period. Supports CSV / JSONL export.
4. Device status panel (collapsed by default): For debugging driver data reception: received frame count, protocol errors, lost frames, waveform packet count.

UI supports English / Chinese. Toggle via `EN` / `中文` button on top-right. The first load follows browser language, and the selection is saved for future visits.

### Data specification

- Presence and motion state are reported only on state change. It may take up to ~1 minute for the module to report "person gone". The application actively queries status every 3 seconds to recover from lost packets.
- Respiration / heart rate = 0 means the module has no valid measurement yet, dashboard shows "measuring".
- Waveform is polled once per second. If waveform packet count stays zero in device status: ensure a stationary human target stays within 0.4–1.5 m.
- Historical records are stored under `~/r60abd1-data/` in JSONL format. Events are written on state change, plus one full snapshot every minute. Truncated / broken lines caused by abnormal program exit will be skipped automatically.
- Active query for motion, range and waveform requires the updated patched kernel driver. Older drivers can run the demo normally, but these extended queries are unavailable.

## 7. Core Implementation

### Kernel Driver

```
static const struct serdev_device_ops serial_ops = {
    .receive_buf = receive_buf,
    .write_wakeup = write_wakeup,
};
```

The serial receive callback caches and parses complete radar frames, converts raw payloads into sensing events and queues them. User-space programs access the device through these entry points:

```
.read = radar_read,
.poll = radar_poll,
.unlocked_ioctl = radar_ioctl,
```

Purpose: encapsulate low-level UART communication and expose unified character-device API to application layer.

### Frame Parsing

```
MSG(0x81, 2, RESP_RATE, BYTE, 1),
MSG(0x85, 2, HEART_RATE, BYTE, 1),
MSG(0x81, 5, RESP_WAVE, WAVE, 5),

// Waveform conversion snippet
case F_WAVE:
    event->count = 5;
    for (i = 0; i < 5; i++)
        event->value[i] = (int)data[i] - 128;
    break;
```

Purpose: Validate frame header, length, checksum and footer, convert raw protocol bytes into unified event structure. Each waveform packet carries 5 raw sample points, not synthetic values derived from heart / respiration rate.

### Application API

Event read logic uses `poll` + `read`:

```
wait = (struct pollfd){.fd = radar->fd, .events = POLLIN};
ret = poll(&wait, 1, timeout_ms);
if (ret <= 0)
    return ret;
length = read(radar->fd, event, sizeof(*event));
```

Full implementation includes length validation, interface version check and range sanitization. Upper layer call:

```
ret = r60abd1_get_data(&radar, &event, 200);
```

Return code: `1` = new event received, `0` = no available data, `-1` = error. This API also wraps initialization, deinit and mode control.

### Web Rendering

Frontend fetches snapshot from backend and refreshes UI:

```
const r = await fetch(
  "/api/snapshot?after="+cursor+"&session="+(session||""),
  {cache:"no-store"});
if (!r.ok) throw Error("HTTP "+r.status);
const s = await r.json();
if (version === requestVersion && !changing) render(s);
```

Conditional rendering for different modes:

```
cards("common", s.common, commonFields);
$("sleep-section").hidden = s.mode !== 1 || s.switching;
$("waveforms").hidden = s.mode !== 0 || s.switching;
```

Purpose: Refresh general data roughly every two seconds. Render real waveform in real-time mode and sleep analysis in sleep mode, avoid mixed view of unrelated panels.

### Local Logging & Rotation

```
volume = int((time.monotonic() - self.started) // RECORD_VOLUME_SECONDS)
if self.volume != volume:
    if self.record is not None:
        self.record.close()
    path = self.directory / "{}-{}-{:04d}.jsonl".format(
        self.prefix, self.started_ns, volume)
    self.record = path.open("x", encoding="utf-8")
    self.volume = volume
```

Exclusive file creation prevents overwriting existing logs. Buffers are flushed periodically, and a final snapshot is written on graceful exit. Write failures and corrupted records are logged explicitly.

## 8. Known Issues & Contribution Guide

- DP1 presence detection: The built-in DP1 human presence algorithm has noticeable jitter and slow state transition. Manufacturer states this metric is for reference only and heavily depends on installation.
- DP5 human azimuth report: The protocol definition for DP5 azimuth is ambiguous. After confirmation with module vendor and Quectel technical team: the highest bit = `1` represents negative angle.

If you meet problems during development, post your technical questions on the [Quectel Official Forum](https://forumschinese.quectel.com/c/quectel-pi/58). Technical support will reply as soon as possible.
Issues and Pull Requests are welcome to improve this project.

Copyright © Quectel Wireless Solutions Co., Ltd. 2026. All rights reserved.