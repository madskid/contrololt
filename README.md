# ControlOLT Manager v1

ControlOLT Manager is a web application for monitoring and managing ZTE Optical Line Terminals (OLTs), including the
C300, C320, C600, C620, C650, and C680 families. It collects OLT and ONU information over SNMP and Telnet, synchronizes
ONU configuration and MAC addresses, and provides a browser dashboard for operations, diagnostics, and controlled
configuration changes.

This folder is a standalone installation package. It contains the application source, web assets, an example environment
file, and runtime Python dependencies. It intentionally does not include databases, credentials, generated secrets,
virtual environments, Docker files, or development tests.

## Features

- Monitor ONU connectivity, optical levels, and OLT health.
- Synchronize ONU configuration, MAC addresses, cards, PON ports, uplinks, VLANs, speed profiles, and ONU types.
- Search configured ONUs by serial number, MAC address, name, address, contact, PPPoE username, or external ID.
- Manage ONU registration, rename, reboot, mode, Ethernet ports, speed profiles, VLANs, and other supported operations.
- View traffic, signal, and status graphs; review an audit log of OLT changes.
- Manage administrator and read-only accounts. Read-only users can view only Configured and Diagnostics.
- Run scheduled polling, synchronization, graph sampling, and daily configuration saves in the application process.

OLT write actions require explicit per-OLT enablement, are validated by the application, and are recorded in the audit log.
Some actions execute after form confirmation; other write actions use a command preview.

## Requirements

- Linux (Ubuntu/Debian recommended) or Windows.
- Python 3.11 or later.
- Network connectivity from the application host to the OLTs over Telnet and, when used, SNMP.
- Net-SNMP command-line tools (`snmpget` and `snmpbulkwalk`) for SNMP polling.
- Internet access or a Python package mirror to install dependencies.

The application uses SQLite by default. PostgreSQL and Redis are optional.

## Install on Linux (Ubuntu/Debian)

1. Install Python, virtual environment support, Git, and Net-SNMP:

   ```bash
   sudo apt update
   sudo apt install -y python3 python3-venv python3-pip git snmp
   ```

2. Change to the extracted `ControlOLT-Manager-v1` folder.

3. Create a virtual environment and install the application dependencies:

   ```bash
   python3 -m venv .venv
   . .venv/bin/activate
   python -m pip install --upgrade pip
   pip install -r requirements.txt
   ```

4. Configure the administrator account and application secret in the current shell:

   ```bash
   export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
   export ADMIN_USER=admin
   read -rsp "Initial administrator password: " ADMIN_PASS; echo
   export ADMIN_PASS
   ```

   Choose a strong password of at least 5 characters. Keep both values private. Preserve the same `SECRET_KEY` across
   restarts; changing it prevents the application from decrypting stored OLT credentials. For persistent installations,
   configure these variables in a protected service environment file.

5. Start the application in the same shell session:

   ```bash
   uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```

6. Open `http://SERVER-IP:8000` from a trusted network and sign in with the administrator credentials. The first
   startup creates the initial administrator. Use **Settings → Users** to add and manage later accounts.

Press `Ctrl+C` to stop the application.

## Install on Windows (PowerShell)

Install Python 3.11 or later and Git. Install Net-SNMP and add `snmpget` and `snmpbulkwalk` to `PATH` if using SNMP
polling. Open PowerShell in the extracted `ControlOLT-Manager-v1` folder and run:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
$env:SECRET_KEY = (python -c "import secrets; print(secrets.token_urlsafe(48))")
$env:ADMIN_USER = "admin"
$securePassword = Read-Host "Initial administrator password (at least 5 characters)" -AsSecureString
$env:ADMIN_PASS = [System.Net.NetworkCredential]::new("", $securePassword).Password
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Keep `SECRET_KEY` and `ADMIN_PASS` private and preserve them across restarts. These environment variables last only for
the current PowerShell session; use a protected service environment for a persistent deployment.

## Configuration and operations

- `.env.example` documents available settings; the application does **not** automatically load a `.env` file. Set
  environment variables in the shell or service manager before starting the application.
- Without `DATABASE_URL`, SQLite is created automatically at `data/smartolt.db`.
- For PostgreSQL, set `DATABASE_URL`, for example
  `postgresql+asyncpg://USER:PASSWORD@HOST:5432/contrololt`.
- The generated secret and other persistent application data are stored under `data/`. Back up this directory and
  protect it; it contains the database and secret files.
- Run a single Uvicorn process. Do not use multiple `--workers` processes; polling and scheduled jobs run in-process.
- Restrict access to the dashboard to trusted administrator networks. Use an HTTPS reverse proxy and set
  `HTTPS_ONLY=true` when serving over an untrusted network.
- Permit the application host to reach OLTs using the required Telnet and SNMP ports.
- `TELNET_IDLE_CLOSE` must be lower than the OLT's configured Telnet idle timeout.
- Back up `data/` before updating the application. Database columns and tables are added automatically at startup.
- Optionally set `REDIS_URL` to enable Redis pub/sub for real-time events.

## ZTE family notes

| Feature | C300/C320 (`legacy`) | C600/C620/C650/C680 (`c6xx`) |
|---|---|---|
| OLT/ONU interface names | `gpon-olt_1/2/3`, `gpon-onu_1/2/3:4` | `gpon_olt-1/2/3`, `gpon_onu-1/2/3:4`; service port `vport-1/2/3.4:1` |
| Bridging service | `service INT gemport 1 vlan V` | `service INT gemport 1 cos 0 vlan V` |
| PPPoE routing | Flow/PPPoE configuration | `service` and `wan-ip ipv4 mode pppoe`; requires the `PPPoE` VLAN profile |
| MAC address command | `show mac gpon onu <onu-if>` | `show mac interface <vport-if>` |

Firmware output and available commands may vary. Review commands and test changes on a lab ONU before enabling writes on
production OLTs. Use the dashboard audit log to review command results.

## Security

Keep application secrets, database files, and OLT credentials private. Do not publish `.env` files or the contents of
`data/`. Use a strong administrator password and serve the dashboard only on a trusted network or behind HTTPS.
