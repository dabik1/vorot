# 🚪 Vorot — Camera & Gate Control System

**Vorot** is a web-based control system for IP cameras and GPIO devices, designed for Raspberry Pi and Orange Pi boards.

Manage your security cameras, gates, lights, and other devices through a unified dark-themed web interface with scheduling and automation.

![Version](https://img.shields.io/badge/version-4.4.1-green)
![Python](https://img.shields.io/badge/python-3.7+-blue)
![License](https://img.shields.io/badge/license-MIT-yellow)

## ✨ Features

- **RTSP Camera Streaming** — Hikvision & Dahua support via FFmpeg
- **GPIO Control** — Pulse and toggle modes for gates, lights, relays
- **Scheduler** — Automated actions by time and day of week
- **Multi-user** — Admin and user roles with individual layouts
- **Customizable UI** — Grid/list view, drag-and-drop reordering
- **Network Scanner** — Auto-discover cameras on local network
- **Backup & Restore** — Full configuration export/import
- **OTA Updates** — Web-based and command-line update system
- **Dark Theme** — Clean, responsive interface (Ukrainian localization)

## 🚀 Quick Install

On a Raspberry Pi / Orange Pi with Armbian/Debian/Ubuntu:

```bash
curl -sSL https://tehnodron.in.ua/camera-control/install.txt | sudo bash
```

Or manually:

```bash
sudo apt update
sudo apt install -y python3 python3-pip ffmpeg sqlite3
pip3 install flask --break-system-packages
pip3 install OPi.GPIO --break-system-packages  # for Orange Pi

git clone https://github.com/dabik1/vorot.git /opt/camera-control
cd /opt/camera-control
chmod +x camera-control.py
python3 camera-control.py
```

Open in browser: `http://<your-ip>:8080`  
Default login: `admin` / `admin`

## 📦 Project Structure

```
vorot/
├── camera-control.py      # Main application (Flask)
├── version.json           # Version metadata
├── install.sh             # Installation script
├── update.sh              # Update script
├── htaccess-template.txt  # Apache config for hosting
├── DEPLOY_TO_SERVER.md    # Server deployment guide
├── QUICK_START.md         # Quick start guide (UA)
└── README.md              # This file
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

## 🛠 Run as a Service

```bash
sudo cp camera-control.py /opt/camera-control/
sudo tee /etc/systemd/system/camera-control.service <<EOF
[Unit]
Description=Camera Control System
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/camera-control
ExecStart=/usr/bin/python3 /opt/camera-control/camera-control.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable camera-control
sudo systemctl start camera-control
```

## 🔄 Updates

### Via web interface:
Settings → Updates → Check for updates

### Via command line:
```bash
curl -sSL https://tehnodron.in.ua/camera-control/update.txt | sudo bash
```

## 🔒 Security Recommendations

- **Change default password** immediately after installation
- Use **VPN** (WireGuard/Tailscale) instead of exposing port 8080
- Configure **firewall** (ufw) to restrict access
- Disable **password SSH** authentication, use keys only
- Keep the system **updated**

## 📋 Requirements

- Python 3.7+
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
