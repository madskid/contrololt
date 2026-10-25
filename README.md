# ControlOLT Manager v1.1

ControlOLT Manager is a web application for monitoring and managing ZTE Optical Line Terminals (OLTs), including the
C300, C320, C600, C620, C650, and C680 families. It collects OLT and ONU information over SNMP and Telnet, synchronizes
ONU configuration and MAC addresses, and provides a browser dashboard for operations, diagnostics, and controlled
configuration changes.

This folder is a standalone installation package containing the OLT management application source, web assets, and
runtime Python dependencies. This distribution excludes MikroTik router management and PPPoE user management. It does not
include environment files, databases, credentials, generated secrets, virtual environments, Docker files, or development tests.

## Features

- Monitor ONU connectivity, optical levels, and OLT health.
- Synchronize ONU configuration, MAC addresses, cards, PON ports, uplinks, VLANs, speed profiles, and ONU types.
- Search configured ONUs by serial number, MAC address, name, address, contact, PPPoE username, or external ID.
- Manage ONU registration, rename, reboot, mode, Ethernet ports, speed profiles, VLANs, and other supported operations.
- Replace a registered ONU's serial number from its detail page when a new ONU is detected unconfigured on the same OLT/PON; the existing bridge service is reapplied, the old registration is removed, and the dashboard external ID is updated to the new SN.
- Register ONUs in bridge mode for PPPoE configured locally through the ONU web interface; the OLT carries the selected VLAN without storing PPPoE credentials.
- Manually move an ONU whose serial number is already registered by scanning its newly detected PON in Unconfigured; the existing bridge configuration is applied to the destination and the source configuration is removed.
- View traffic, signal, and status graphs; review an audit log of OLT changes.
- Manage administrator and read-only accounts. Read-only users can view only Configured and Diagnostics.
- Run scheduled polling, synchronization, graph sampling, and daily configuration saves in the application process.

OLT write actions require explicit per-OLT enablement, are validated by the application, and are recorded in the audit log.
Some actions execute after form confirmation; other write actions use a command preview.
When an ONU SN found in **Unconfigured** is already registered, an administrator can manually move its configuration from
the current registration to the newly detected PON. The move begins after confirmation and runs through the application
worker when `RUN_WORKERS=true`, or the separate `python -m app.worker` process when `RUN_WORKERS=false`. Both source and
destination OLTs must allow writes. The implementation supports standard bridge ONUs only; OLT-routed PPPoE is rejected
because its password is not stored for safe reprovisioning, and custom per-port VLAN settings are not copied. The worker
verifies the destination registration and confirms the ONU is no longer registered on the old source. If source cleanup
or verification fails, the move is marked failed and the audit log identifies the partial result so both OLTs can be checked.
ONU replacement is also an administrator-only, manually confirmed action from the configured ONU detail page. It is limited
to validated bridge services and a new SN detected unconfigured on the same OLT/PON; the old registration is removed only
after the replacement registration is verified.

## Requirements

- Linux (Ubuntu/Debian recommended) or Windows.
- Python 3.11 or later. Python 3.10 is not the documented/supported baseline.
- Network connectivity from the application host to the OLTs over Telnet and, when used, SNMP.
- Net-SNMP command-line tools (`snmpget` and `snmpbulkwalk`) for SNMP polling.
- Internet access or a Python package mirror to install dependencies.

SQLite is the default database for evaluation or small installations. PostgreSQL is supported and recommended for
production or larger deployments; the `asyncpg` driver is included in `requirements.txt`. Redis is optional.

## Install on Linux (Ubuntu/Debian)

The release ZIP contains a top-level `contrololt/` directory and a `deploy/contrololt.service` systemd unit. The
commands below install PostgreSQL locally and run the application as the separate, unprivileged Linux account
`contrololt`. The PostgreSQL login `noc` is a database role, not the Linux service account.

1. Install system packages, then confirm Python is at least 3.11:

   ```bash
   sudo apt update
   sudo apt install -y python3 python3-venv python3-pip unzip snmp postgresql openssl curl
   python3 --version
   ```

   Ubuntu 24.04 and Debian 12 provide a suitable Python version by default. Ubuntu 22.04 normally provides Python 3.10;
   use a trusted Python 3.11+ package source or a newer supported OS release before continuing if the version check is
   below 3.11.

2. From the directory containing `contrololt.zip`, extract the release. This creates `/opt/contrololt/`:

   ```bash
   sudo unzip contrololt.zip -d /opt
   ```

   Use a clean destination for a new installation. For an upgrade, back up the database and `data/` first; do not
   overwrite an active installation without a planned maintenance window.

3. Create the PostgreSQL role and database. This example generates a URL-safe hexadecimal password; keep it for the
   environment-file step below:

   ```bash
   DB_PASS="$(openssl rand -hex 24)"
   sudo -u postgres psql -v ON_ERROR_STOP=1 \
     -c "CREATE ROLE noc LOGIN PASSWORD '${DB_PASS}';" \
     -c "CREATE DATABASE contrololt OWNER noc;"
   ```

   These are one-time creation commands. If the role or database already exists, inspect it rather than rerunning the
   `CREATE` commands.

4. Create the dedicated Linux service account, writable data directory, and Python virtual environment:

   ```bash
   sudo useradd --system --home-dir /opt/contrololt --shell /usr/sbin/nologin contrololt
   sudo install -d -o contrololt -g contrololt /opt/contrololt/data
   sudo python3 -m venv /opt/contrololt/.venv
   sudo /opt/contrololt/.venv/bin/python -m pip install --upgrade pip
   sudo /opt/contrololt/.venv/bin/pip install -r /opt/contrololt/requirements.txt
   ```

5. Create the protected service environment file and save the generated initial admin password printed at the end:

   ```bash
   APP_SECRET="$(openssl rand -hex 48)"
   ADMIN_PASS="$(openssl rand -hex 16)"
   sudo tee /etc/contrololt.env >/dev/null <<EOF
   DATABASE_URL=postgresql+asyncpg://noc:${DB_PASS}@127.0.0.1:5432/contrololt
   SECRET_KEY=${APP_SECRET}
   ADMIN_USER=noc
   ADMIN_PASS=${ADMIN_PASS}
   DATA_DIR=/opt/contrololt/data
   TZ_NAME=Asia/Jakarta
   AUTOSAVE_TIME=02:30
   TELNET_IDLE_CLOSE=90
   EOF
   sudo chown root:root /etc/contrololt.env
   sudo chmod 600 /etc/contrololt.env
   printf 'Initial ControlOLT admin password: %s\n' "$ADMIN_PASS"
   ```

   Store that initial admin password securely. `SECRET_KEY` and `DATABASE_URL` must remain unchanged across restarts.
   The env file is read by systemd; the application does not automatically load `.env` files.

6. Install and start the systemd service:

   ```bash
   sudo install -m 644 /opt/contrololt/deploy/contrololt.service /etc/systemd/system/contrololt.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now contrololt
   sudo systemctl status contrololt --no-pager
   ```

   Check logs with `sudo journalctl -u contrololt -f`. Confirm the app is healthy with
   `curl http://127.0.0.1:8000/health`.

7. Open `http://SERVER-IP:8000` from an allowed network and sign in with user `noc` and the generated initial
   administrator password. The service listens on all interfaces at port 8000. For Internet-facing use, put it behind
   an HTTPS reverse proxy and restrict access with a firewall.

The unit is configured to run one Uvicorn process. Do not add multiple workers: polling and scheduled jobs run in-process.
Use `sudo systemctl restart contrololt` to restart after changing `/etc/contrololt.env`.

## Install on Windows (PowerShell)

Install Python 3.11 or later. Install Net-SNMP and add `snmpget` and `snmpbulkwalk` to `PATH` if using SNMP polling.
Open PowerShell in the extracted `contrololt` folder and run:

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

- The application does **not** automatically load a `.env` file. Set environment variables in the shell or service
  manager before starting the application.
- Without `DATABASE_URL`, SQLite is created automatically at `data/smartolt.db`.
- PostgreSQL is supported through `asyncpg`. Set `DATABASE_URL` in the form
  `postgresql+asyncpg://USER:PASSWORD@HOST:5432/DATABASE`. URL-escape reserved characters in passwords. The Linux
  installation above uses a generated hexadecimal password, which needs no URL escaping.
- The generated secret and other persistent application data are stored under `data/`. Back up this directory and
  protect it; it contains SQLite data and fallback secret files. With PostgreSQL, also back up the PostgreSQL database.
- Run a single Uvicorn process. Do not use multiple `--workers` processes; polling and scheduled jobs run in-process.
- Restrict access to the dashboard to trusted administrator networks. Use an HTTPS reverse proxy and set
  `HTTPS_ONLY=true` when serving over an untrusted network.
- Permit the application host to reach OLTs using the required Telnet and SNMP ports.
- `TELNET_IDLE_CLOSE` must be lower than the OLT's configured Telnet idle timeout.
- Full OLT configuration is synchronized to the database every hour by default (`FULL_SYNC_HOURS=1`).
  Status polling continues on its separate `POLL_INTERVAL`; successful dashboard edits update the database directly
  and do not trigger an immediate full sync.
- OLT Telnet sessions are separated by workload: polling, ONU checks/diagnostics, full sync, and configuration writes
  each use their own persistent session. Commands remain serialized within each workload, while the global session
  concurrency is still limited by `MAX_OLT_CONCURRENCY`.
- Back up `data/` and the PostgreSQL database before updating the application. Database columns and tables are added
  automatically at startup.
- Optionally set `REDIS_URL` to enable Redis pub/sub for real-time events.

## Server sizing for larger deployments

For a planning baseline around 50,000 ONUs across 50 OLTs, start with **8 vCPU, 16 GB RAM, and 200 GB SSD/NVMe**,
PostgreSQL on the same host or a low-latency database host, and a reliable 1 Gbps management network. A more comfortable
starting point is **16 vCPU, 32 GB RAM, and 500 GB NVMe**, especially when retaining graph history or serving many
concurrent dashboard users.

These are initial sizing estimates, not a performance guarantee or a tested hard minimum. Actual load depends heavily on
OLT model/firmware, enabled SNMP/CLI operations, polling interval, ONU churn, graph retention, and concurrent users.
In particular, full synchronization reads MAC information from active ONUs sequentially on each OLT, so the initial
sync can take significant time at this scale. Pilot with representative OLTs, monitor CPU, RAM, database size, and sync
duration, and tune polling/retention before onboarding all 50 OLTs. Keep sufficient extra disk capacity for backups.

## ZTE family notes

| Feature | C300/C320 (`legacy`) | C600/C620/C650/C680 (`c6xx`) |
|---|---|---|
| OLT/ONU interface names | `gpon-olt_1/2/3`, `gpon-onu_1/2/3:4` | `gpon_olt-1/2/3`, `gpon_onu-1/2/3:4`; service port `vport-1/2/3.4:1` |
| Bridging service | `service INT gemport 1 vlan V` | `service INT gemport 1 cos 0 vlan V` |
| PPPoE routing | Flow/PPPoE configuration | `service` and `wan-ip ipv4 mode pppoe`; requires the `PPPoE` VLAN profile |
| MAC address command | `show mac gpon onu <onu-if>` | `show mac interface <vport-if>` |

Firmware output and available commands may vary. Review commands and test changes on a lab ONU before enabling writes on
production OLTs. Use the dashboard audit log to review command results.
Use **Edit credentials** on a routing ONU to update only its PPPoE username/password; use **Update ONU mode** for VLAN or
routing/bridging changes.
Newly registered ONUs are provisioned with web management enabled using
`security-mgmt 212 state enable mode forward protocol web`. This registration command is included for both CLI families;
OLT firmware that uses a different `security-mgmt` syntax can override `reg_web_mgmt` per family in `data/profiles.json`.
ONU registration and dashboard renames set both the OLT `name` and `description` to the same value.

## Security

Keep application secrets, database files, and OLT credentials private. Do not publish `.env` files or the contents of
`data/`. Use a strong administrator password and serve the dashboard only on a trusted network or behind HTTPS.
