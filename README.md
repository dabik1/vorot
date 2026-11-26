# 🚪 Vorot — Camera & Gate Control System

**Vorot** is a web-based control system for IP cameras and GPIO devices, designed for Raspberry Pi and Orange Pi boards.

Manage your security cameras, gates, lights, and other devices through a unified dark-themed web interface with scheduling and automation.

![Version](https://img.shields.io/badge/version-4.4.9-green)
![Python](https://img.shields.io/badge/python-3.7+-blue)
![License](https://img.shields.io/badge/license-MIT-yellow)

## ✨ Features

- **RTSP Camera Streaming** — Hikvision & Dahua via FFmpeg; one FFmpeg per camera shared by all viewers, works on iPhone/iPad
- **GPIO Control** — Pulse and toggle modes for gates, lights, relays
- **Scheduler** — Automated actions by time and day of week
- **Multi-user** — Admin and user roles with individual layouts
- **Customizable UI** — Grid/list view, drag-and-drop reordering
- **Network Scanner** — Auto-discover cameras on local network
- **Backup & Restore** — Full configuration export/import
- **OTA Updates** — SSL + SHA-256 verified, test run before install, one-click rollback
- **Security** — salted password hashes, forced change of weak passwords, login rate limit, CSRF protection
- **Relay safety** — relays switched off at startup and on shutdown, even mid-pulse
- **Reverse proxy** — can run in a subfolder behind nginx (`URL_PREFIX`)
- **Dark Theme** — Clean, responsive interface (Ukrainian localization)

## 🚀 Quick Install

On an Orange Pi / NanoPi / Raspberry Pi with Armbian, Debian or Ubuntu:

```bash
wget https://tehnodron.in.ua/camera-control/install.txt -O install.sh
sudo LISTEN_PORT=8080 bash install.sh
```

On boards with 256 MB RAM (NanoPi NEO) run it in the background, so a dropped SSH
session cannot interrupt package installation:

```bash
sudo systemd-run --unit=vorot-install --setenv=LISTEN_PORT=8080 bash "$PWD/install.sh"
journalctl -u vorot-install -f
```

The installer refuses to run on < 512 MB RAM without swap — enable zram first
(Armbian has it by default).

Or manually:

```bash
sudo apt install -y --no-install-recommends python3 python3-flask python3-pip ffmpeg earlyoom
sudo pip3 install OPi.GPIO --break-system-packages   # Orange Pi / NanoPi
sudo mkdir -p /opt/camera-control
sudo cp camera_control.py /opt/camera-control/
sudo cp deploy/camera-control.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now camera-control
```

Open in browser: `http://<your-ip>:8080`
First login: `admin` / `admin` — you will be asked to set a new password immediately.

## 📦 Project Structure

```
vorot/
├── camera_control.py            # Main application (Flask)
├── version.json                 # Published version: URL + SHA-256
├── make_version.py              # Builds version.json for a release, checks the page JS
├── install.sh                   # Installation script
├── update.sh                    # Command-line update (verified, with rollback)
├── deploy/
│   ├── camera-control.service   # systemd unit
│   └── nginx-subfolder.conf     # nginx example: Vorot under /vorot/
├── htaccess-template.txt        # Apache config for hosting
├── CHANGELOG.md                 # Version history (UA)
├── DEPLOY_TO_SERVER.md          # Server deployment guide (UA)
├── QUICK_START.md               # Quick start guide (UA)
└── README.md                    # This file
```

## 🔧 Configuration

### Supported Cameras

| Brand     | Protocol | Stream Types     |
|-----------|----------|------------------|
| Hikvision | RTSP/TCP | Main + Substream |
| Dahua     | RTSP/TCP | Main + Substream |

### GPIO Modes

- **Pulse** — Activate for a set duration (e.g., gate opener)
- **Toggle** — Switch on/off (e.g., lights)

### Scheduler

Create automated schedules for any GPIO button:
- Set time (HH:MM)
- Choose days of week
- Actions: Pulse, ON, OFF

## ⚙️ Environment Variables

Set them in the `[Service]` section of `camera-control.service` (`Environment=NAME=value`).

| Variable          | Default                                   | Purpose                                         |
|-------------------|-------------------------------------------|-------------------------------------------------|
| `LISTEN_PORT`     | `8080`                                    | Web interface port                              |
| `LISTEN_HOST`     | `0.0.0.0`                                 | Bind address (`127.0.0.1` behind nginx)         |
| `URL_PREFIX`      | *(empty)*                                 | Subfolder behind nginx, e.g. `/vorot`           |
| `TRUST_PROXY`     | *(off)*                                   | `1` — take client IP from `X-Forwarded-For` (localhost only) |
| `UPDATE_REPO_URL` | `https://tehnodron.in.ua/camera-control`  | Where updates are checked                       |

## 🛠 Run as a Service

See [`deploy/camera-control.service`](deploy/camera-control.service). It includes
`OOMScoreAdjust=-500`, so under memory pressure the kernel and earlyoom kill
FFmpeg or apt before the gate controller.

## 🔄 Updates

### Via web interface
Settings → Updates → Check for updates. The new version is downloaded over HTTPS,
checked against the SHA-256 in `version.json`, test-started, then installed.
The previous version is kept — **Rollback** button in the same panel.

### Via command line
For devices where the web update does not work (4.4.1–4.4.5 had broken page JavaScript):

```bash
wget https://tehnodron.in.ua/camera-control/update.txt -O update.sh
sudo bash update.sh
```

### Publishing a release
1. Bump `CURRENT_VERSION` in `camera_control.py`
2. `python3 make_version.py camera_control.py "what changed"` — checks the page
   JavaScript and writes `version.json` + `camera_control.txt`
3. Upload both files to `https://tehnodron.in.ua/camera-control/`

## 🔒 Security Recommendations

- **Change default password** immediately after installation
- Use **VPN** (WireGuard / L2TP on the router) instead of exposing the web port to the internet
- Configure **firewall** (ufw) to restrict access
- Disable **password SSH** authentication, use keys only
- Keep the system **updated**

## 📋 Requirements

- Python 3.7+
- Debian / Ubuntu / Armbian; 256 MB RAM works with zram + earlyoom
- FFmpeg (for RTSP streaming)
- SQLite3
- Flask
- OPi.GPIO or RPi.GPIO (for GPIO control)

## 🌍 Localization

The web interface is in **Ukrainian** (Українська).

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.

## 👤 Author

**Oleksandr** — [dabik1](https://github.com/dabik1)

---

*Vorot — your gate, your control* 🚪
