"""Pure parsers (no I/O) for ZTE CLI output. Easy to test and adapt."""
import re
from typing import Optional

ONU_IF_RE = re.compile(r"gpon[-_]onu[-_](\d+)/(\d+)/(\d+):(\d+)", re.I)
OLT_IF_RE = re.compile(r"gpon[-_]olt[-_](\d+)/(\d+)/(\d+)", re.I)
VPORT_RE = re.compile(r"vport[-_](\d+)/(\d+)/(\d+)\.(\d+):(\d+)", re.I)     # C6xx: vport-1/5/1.3:1 = ONU 3 di 1/5/1, vport 1
SECTION_RE = re.compile(r"^(interface|pon-onu-mng)\s+(\S+)", re.I)

SNMP_PHASE = {1: "logging", 2: "LOS", 3: "syncMib", 4: "working", 5: "DyingGasp", 6: "authFailed", 7: "offline"}

# Recognized Phase State values. Used to locate the phase column in `show gpon onu state`
# (newer firmware adds a "Channel" column, e.g. `1(GPON)`, AFTER the phase state, so the last column is not the phase).
_OFFLINE_PHASES = {"offline", "off-line", "logging", "syncmib", "authfailed", "auth-failed", "unknown", "ready"}
_KNOWN_PHASES = {"working", "online", "los", "dyinggasp", "dying-gasp", "dying_gasp"} | _OFFLINE_PHASES
_CHANNEL_COL = re.compile(r"^\d+\(\w+\)$")          # 1(GPON), 2(XGSPON), ...


def phase_to_status(phase: str) -> str:
    p = (phase or "").strip().lower()
    if p in ("working", "online"):
        return "online"
    if p == "los":
        return "los"
    if p in ("dyinggasp", "dying-gasp", "dying_gasp"):
        return "power_fail"
    if p in _OFFLINE_PHASES:
        return "offline"
    return "unknown"     # Do not treat unknown text (e.g. "1(GPON)") as offline.


def extract_phase(cols: list[str]) -> str:
    """Select the Phase State column from columns following the ONU index.
    Order: (1) last column containing a recognized phase name, (2) column before Channel `n(XXX)`,
    (3) kolom terakhir."""
    for c in reversed(cols):
        if c.lower() in _KNOWN_PHASES:
            return c
    if len(cols) >= 2 and _CHANNEL_COL.match(cols[-1]):
        return cols[-2]
    return cols[-1] if cols else ""


def snmp_int(val: str) -> int | None:
    """Nilai SNMP integer: '4', 'INTEGER: 4', 'working(4)'."""
    m = re.search(r"\((-?\d+)\)", val or "") or re.search(r"-?\d+", val or "")
    if not m:
        return None
    return int(m.group(1) if m.re.groups else m.group())


def parse_running_config(text: str) -> list[dict]:
    """List registered ONUs with name/description/VLAN/connection type from `show running-config`."""
    onus: dict[tuple, dict] = {}

    def config_value(value: str) -> str:
        value = value.strip()
        return "" if re.fullmatch(r"\*+", value) else value

    def entry(key: tuple) -> dict:
        if key not in onus:
            onus[key] = {"shelf": key[0], "slot": key[1], "pon": key[2], "onu_number": key[3],
                         "sn": "", "onu_type": "", "name": "", "description": "",
                         "vlan": None, "conn_type": "", "pppoe_user": "", "mode": "",
                         "mgmt_ip": "", "mgmt_vlan": None, "up_profile": "", "dl_profile": ""}
        return onus[key]

    kind, ctx = None, None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line == "!":
            kind = ctx = None
            continue
        m = SECTION_RE.match(line)
        if m:
            head, name = m.group(1).lower(), m.group(2)
            mo = OLT_IF_RE.fullmatch(name)
            if mo and head == "interface":
                kind, ctx = "olt", tuple(int(x) for x in mo.groups())
                continue
            mo = ONU_IF_RE.fullmatch(name)
            if mo:
                kind = "onu" if head == "interface" else "mng"
                ctx = tuple(int(x) for x in mo.groups())
                entry(ctx)
                continue
            mo = VPORT_RE.fullmatch(name)
            if mo and head == "interface":          # C6xx: service-port is under the vport interface, not the ONU interface.
                kind, ctx = "vport", tuple(int(x) for x in mo.groups()[:4])
                entry(ctx)
                continue
            kind = ctx = None
            continue
        if kind is None:
            continue
        if kind == "olt":
            mo = re.match(r"onu\s+(\d+)\s+type\s+(\S+)\s+sn\s+(\S+)", line, re.I)
            if mo:
                e = entry((*ctx, int(mo.group(1))))
                e["onu_type"], e["sn"] = mo.group(2), mo.group(3)
        elif kind == "onu":
            e = entry(ctx)
            mo = re.match(r"name\s+(.+)$", line, re.I)
            if mo:
                e["name"] = config_value(mo.group(1))
                continue
            mo = re.match(r"description\s+(.+)$", line, re.I)
            if mo:
                e["description"] = config_value(mo.group(1))
                continue
            if line.lower().startswith("service-port") and e["vlan"] is None:
                vl = re.findall(r"\bvlan\s+(\d+)", line, re.I)
                if vl:
                    e["vlan"] = int(vl[-1])
            mo = re.match(r"tcont\s+(\d+)\s+(?:name\s+\S+\s+)?profile\s+(\S+)", line, re.I)
            if mo and (not e["up_profile"] or mo.group(1) == "1"):
                e["up_profile"] = mo.group(2)
            mo = re.match(r"gemport\s+(\d+)\s+traffic-limit\s+.*?downstream\s+(\S+)", line, re.I)
            if mo and (not e["dl_profile"] or mo.group(1) == "1"):
                e["dl_profile"] = mo.group(2)
        elif kind == "vport":
            e = entry(ctx)
            if line.lower().startswith("service-port") and e["vlan"] is None:
                vl = re.findall(r"\bvlan\s+(\d+)", line, re.I)
                if vl:
                    e["vlan"] = int(vl[-1])
        elif kind == "mng":
            e = entry(ctx)
            low = line.lower()
            if low.startswith(("wan-ip", "wan ")):
                e["mode"] = "routing"
                if "mode pppoe" in low:
                    e["conn_type"] = "pppoe"
                    mo = re.search(r"username\s+(\S+)", line, re.I)
                    if mo and not re.fullmatch(r"\*+", mo.group(1)):
                        e["pppoe_user"] = mo.group(1)
                elif "mode static" in low:
                    e["conn_type"] = "static"
                elif "mode dhcp" in low:
                    e["conn_type"] = "dhcp"
            elif low.startswith("pppoe") and "user" in low:
                e["conn_type"], e["mode"] = "pppoe", "routing"
                mo = re.search(r"\buser\s+(\S+)", line, re.I)      # Password is intentionally not read.
                if mo and not re.fullmatch(r"\*+", mo.group(1)):
                    e["pppoe_user"] = mo.group(1)
            elif low.startswith("mgmt-ip"):
                mo = re.match(r"mgmt-ip\s+(\S+)\s+\S+\s+vlan\s+(\d+)", line, re.I)
                if mo:
                    e["mgmt_ip"], e["mgmt_vlan"] = mo.group(1), int(mo.group(2))
            elif low.startswith("vlan port") and not e["mode"]:
                e["mode"] = "bridge"
            elif low.startswith(("switchport-bind", "pppoe", "wan-ip")):
                e["mode"] = "routing"
            elif low.startswith("service") and e["vlan"] is None:
                mo = re.search(r"\bvlan\s+(\d+)", line, re.I)
                if mo:
                    e["vlan"] = int(mo.group(1))
    for e in onus.values():
        if e["conn_type"] and not e["mode"]:
            e["mode"] = "routing"
    return list(onus.values())


_STATE_LINE = re.compile(r"(?:gpon[-_]onu[-_])?(\d+)/(\d+)/(\d+):(\d+)\s+(.*)$", re.I)


def parse_onu_state(text: str) -> dict[tuple, str]:
    """`show gpon onu state` -> {(shelf,slot,pon,onu): phase_state}. Kolom phase dicari lewat extract_phase()."""
    res: dict[tuple, str] = {}
    for line in text.splitlines():
        m = _STATE_LINE.search(line.strip())
        if not m:
            continue
        cols = m.group(5).split()
        if cols:
            res[tuple(int(x) for x in m.groups()[:4])] = extract_phase(cols)
    return res


_RX_LINE = re.compile(r"(?:gpon[-_]onu[-_])?(\d+)/(\d+)/(\d+):(\d+)\s+(-?\d+(?:\.\d+)?)\s*\(?dbm", re.I)


def parse_onu_rx_list(text: str) -> dict[tuple, float]:
    """`show pon power onu-rx <gpon-olt>` -> {(shelf,slot,pon,onu): dBm}."""
    return {tuple(int(x) for x in m.groups()[:4]): float(m.group(5)) for m in _RX_LINE.finditer(text)}


def parse_power_attenuation(text: str) -> dict[str, Optional[float]]:
    res: dict[str, Optional[float]] = {k: None for k in
                                        ("onu_rx", "onu_tx", "olt_rx", "olt_tx", "att_up", "att_down")}
    for line in text.splitlines():
        low = line.strip().lower()
        pw = dict(re.findall(r"\b(rx|tx)\s*:?\s*(-?\d+(?:\.\d+)?)\s*\(?dbm", low))
        pw = {k: float(v) for k, v in pw.items()}
        att = re.search(r"(-?\d+(?:\.\d+)?)\s*\(db\)", low)
        if low.startswith("up"):
            res["olt_rx"], res["onu_tx"] = pw.get("rx"), pw.get("tx")
            res["att_up"] = float(att.group(1)) if att else None
        elif low.startswith("down"):
            res["olt_tx"], res["onu_rx"] = pw.get("tx"), pw.get("rx")
            res["att_down"] = float(att.group(1)) if att else None
    return res


_KV = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 _\-/()]{1,40}?)\s*:\s*(.*\S)?\s*$")


def parse_kv(text: str) -> dict[str, str]:
    """Baris 'Key: value' (detail-info dsb). Kunci duplikat: yang pertama dipakai."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = _KV.match(line)
        if m and m.group(2):
            out.setdefault(m.group(1).strip(), m.group(2).strip())
    return out


_UNIT = {"": 1, "k": 1e3, "m": 1e6, "g": 1e9}


def _rate(text: str, label: str) -> Optional[int]:
    """'Input rate : 1471 Bps 12 pps' -> bit/s. Huruf B besar = byte/s (x8), b kecil = bit/s."""
    m = re.search(r"(?i:" + label + r")\s+rate\s*:?\s*(\d+(?:\.\d+)?)\s*([kKmMgG]?)([Bb])ps", text)
    if not m:
        return None
    return int(float(m.group(1)) * _UNIT[m.group(2).lower()] * (8 if m.group(3) == "B" else 1))


def _bytes(text: str, label: str) -> Optional[int]:
    m = re.search(label + r"[^\n]*?(\d+)\s+bytes", text, re.I)
    return int(m.group(1)) if m else None


def parse_traffic(text: str) -> Optional[dict]:
    """`show interface gpon-onu_X`: 'Input rate : 1471 Bps 12 pps / Output rate: 2212 Bps 13 pps'."""
    res = {"in_bps": _rate(text, "input"), "out_bps": _rate(text, "output"),
           "in_bytes": _bytes(text, "input"), "out_bytes": _bytes(text, "output")}
    return res if any(v is not None for v in res.values()) else None


_HIST = re.compile(r"^\s*(\d+)\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:\s+(\S+))?\s*$")


def parse_onu_history(text: str) -> list[dict]:
    """Parse the 'Authpass Time / OfflineTime / Cause' table from `show gpon onu detail-info`."""
    rows = []
    for line in text.splitlines():
        m = _HIST.match(line)
        if m:
            off = None if m.group(3).startswith("0000") else m.group(3)
            rows.append({"n": int(m.group(1)), "authpass": m.group(2), "offline": off, "cause": m.group(4) or ""})
    return rows


def parse_system_group(text: str) -> dict:
    """`show system-group`: OLT uptime and description."""
    m = re.search(r"(\d+)\s*days?,\s*(\d+)\s*hours?,\s*(\d+)\s*minutes?", text, re.I)
    d = re.search(r"System Description:\s*(.+)", text)
    return {"uptime_s": (int(m.group(1)) * 86400 + int(m.group(2)) * 3600 + int(m.group(3)) * 60) if m else None,
            "description": d.group(1).strip() if d else ""}


def parse_optical_module(text: str) -> dict:
    """`show interface optical-module-info xgei_1/20/1`: Rx/Tx power, temperature, and wavelength."""
    def num(rx):
        m = re.search(rx, text, re.I)
        return float(m.group(1)) if m else None
    return {"rx": num(r"\bRxPower\s*:\s*(-?\d+(?:\.\d+)?)"), "tx": num(r"\bTxPower\s*:\s*(-?\d+(?:\.\d+)?)"),
            "temp": num(r"\bTemperature\s*:\s*(-?\d+(?:\.\d+)?)"), "wavelength": num(r"\bWavelength\s*:\s*(\d+)")}


# ───────────────────── ekstra: konfigurasi PON/uplink/VLAN, card, uncfg ─────────────────────
_UPLINK_RE = re.compile(r"(?:x?gei|ge|fei|smartgroup)[-_]\d+(?:/\d+){0,2}", re.I)


def _expand_vlans(spec: str, cap: int = 500) -> list[int]:
    out: list[int] = []
    for tok in re.split(r"[,\s]+", spec.strip()):
        if re.fullmatch(r"\d+", tok):
            out.append(int(tok))
        elif re.fullmatch(r"\d+-\d+", tok):
            a, b = (int(x) for x in tok.split("-"))
            if 0 < b - a <= cap:
                out.extend(range(a, b + 1))
    return out


def parse_olt_config_extras(text: str) -> dict:
    """Read PON descriptions/admin state, uplink ports (+tagged VLANs), and VLAN database from `show running-config`."""
    pon: dict[str, dict] = {}
    uplinks: dict[str, dict] = {}
    vlans: dict[int, dict] = {}
    speed: dict[str, dict] = {"download": {}, "upload": {}}
    onu_types: dict[str, dict] = {}
    cards: list[dict] = []
    section = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line == "!":
            section = None
            continue
        if not raw[0].isspace():
            ac = re.match(r"^add-card\s+rackno\s+\d+\s+shelfno\s+(\d+)\s+slotno\s+(\d+)\s+(\S+)", line, re.I)
            if ac:
                cards.append({"shelf": int(ac.group(1)), "slot": int(ac.group(2)), "type": ac.group(3)})
                continue
            if line.lower() in ("gpon", "pon"):
                section = ("defs", line.lower())
                continue
        if section and section[0] == "defs":
            if (d := re.match(r"profile\s+tcont\s+(\S+)\s+type\s+\d+(.*)$", line, re.I)):
                k = re.search(r"\b(?:fixed|maximum|assured)\s+(\d+)", d.group(2), re.I)
                if k:
                    speed["upload"][d.group(1)] = int(k.group(1))
            elif (d := re.match(r"profile\s+traffic\s+(\S+)\s+sir\s+(\d+)\s+pir\s+(\d+)", line, re.I)):
                speed["download"][d.group(1)] = int(d.group(3))
            elif (d := re.match(r"onu-type\s+(\S+)\s+gpon\b(.*)$", line, re.I)):
                onu_types.setdefault(d.group(1), {"eth": 0, "wifi": 0, "pots": 0, "rf": 0, "description": ""})
                dd = re.match(r"\s*description\s+(.+)$", d.group(2), re.I)
                if dd:      # C600: `... description 4FE,2POTS max-tcont 7 max-gemport 32 ...` -> stop before max-* parameters.
                    onu_types[d.group(1)]["description"] = re.split(r"\s+max-[\w-]+", dd.group(1))[0].strip()
            elif (d := re.match(r"onu-type-if\s+(\S+)\s+(eth|wifi|pots|rf)_\d+/\d+", line, re.I)):
                onu_types.setdefault(d.group(1), {"eth": 0, "wifi": 0, "pots": 0, "rf": 0, "description": ""})[d.group(2).lower()] += 1
            continue
        m = re.match(r"^interface\s+(\S+)", line, re.I)
        if m:
            name = m.group(1)
            mo = OLT_IF_RE.fullmatch(name)
            if mo:
                key = "{}/{}/{}".format(*mo.groups())
                pon.setdefault(key, {"description": "", "admin": "Enabled"})
                section = ("pon", key)
            elif _UPLINK_RE.fullmatch(name):
                uplinks.setdefault(name, {"description": "", "admin": "Enabled", "mode": "", "vlans": "", "pvid": "", "speed": ""})
                section = ("up", name)
            else:
                section = None
            continue
        mv = re.match(r"^vlan\s+(\d[\d,\-\s]*)$", line, re.I)
        if mv and section is None:
            ids = _expand_vlans(mv.group(1))
            for i in ids:
                vlans.setdefault(i, {"name": ""})
            section = ("vlan", ids)
            continue
        if section is None:
            continue
        kind, ctx = section
        if kind == "pon":
            d = re.match(r"description\s+(.+)$", line, re.I)
            if d:
                pon[ctx]["description"] = d.group(1).strip()
            elif line.lower() == "shutdown":
                pon[ctx]["admin"] = "Disabled"
        elif kind == "up":
            u = uplinks[ctx]
            d = re.match(r"description\s+(.+)$", line, re.I)
            if d:
                u["description"] = d.group(1).strip()
            elif (d := re.match(r"speed\s+(\d+)$", line, re.I)):
                u["speed"] = d.group(1)
            elif line.lower() == "shutdown":
                u["admin"] = "Disabled"
            elif (d := re.match(r"switchport\s+mode\s+(\S+)", line, re.I)):
                u["mode"] = d.group(1).lower()
            elif (d := re.match(r"switchport\s+vlan\s+(\S+)\s+tag", line, re.I)):
                u["vlans"] = (u["vlans"] + "," if u["vlans"] else "") + d.group(1)
            elif (d := re.match(r"switchport\s+pvid\s+(\d+)", line, re.I)):
                u["pvid"] = d.group(1)
        elif kind == "vlan":
            d = re.match(r"name\s+(.+)$", line, re.I)
            if d:
                for i in ctx:
                    vlans[i]["name"] = d.group(1).strip()
    return {"pon": pon, "uplinks": uplinks, "vlans": {str(k): v for k, v in sorted(vlans.items())},
            "speed": speed, "onu_types": onu_types, "cards": cards}


_CARD_RE = re.compile(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s+(\S+)\s+(\d+|-+)\s*(.*?)\s*$")


_CARD6_RE = re.compile(r"^\s*(\d+)\s+(\d+)\s+([A-Za-z][\w\-]*)\s+(\d+|-+)\s+(\S+)\s+([A-Za-z]\w*)\s*$")


def _card_status_ok(status: str) -> dict:
    su = status.upper()
    return {"online": su in ("INSERVICE", "STANDBY", "HWONLINE"),
            "role": "Main" if su == "INSERVICE" else "Standby" if su == "STANDBY" else ""}


def parse_cards(text: str) -> list[dict]:
    """`show card` -> slot, configured/installed type, port count, SW version, status, and role.
    Two formats: C300/C320 `Rack Shelf Slot CfgType RealType Port HardVer SoftVer Status` and
    C600 `Shelf Slot CfgType Port HardVer Status` (without RealType/SoftVer)."""
    rows = []
    for line in text.splitlines():
        m = _CARD_RE.match(line)
        if not m:
            m6 = _CARD6_RE.match(line)
            if m6:
                shelf, slot, cfg, ports, hw, status = m6.groups()
                rows.append({"shelf": int(shelf), "slot": int(slot), "type": cfg, "real_type": cfg,
                             "ports": int(ports) if ports.isdigit() else None, "hw": hw, "sw": "",
                             "status": status, **_card_status_ok(status)})
            continue
        rack, shelf, slot, cfg, real, ports, rest = m.groups()
        toks = rest.split()
        status = toks[-1] if toks else ""
        vers = toks[:-1]
        su = status.upper()
        rows.append({"shelf": int(shelf), "slot": int(slot), "type": cfg, "real_type": real,
                     "ports": int(ports) if ports.isdigit() else None,
                     "hw": vers[0] if len(vers) >= 2 else "", "sw": vers[-1] if vers else "",
                     "status": status, "online": su in ("INSERVICE", "STANDBY", "HWONLINE"),
                     "role": "Main" if su == "INSERVICE" else "Standby" if su == "STANDBY" else ""})
    return rows


_UNCFG_RE = re.compile(r"(?:gpon[-_]onu[-_])?(\d+)/(\d+)/(\d+):(\d+)\s+([A-Za-z0-9]{8,20})\b\s*(.*)$", re.I)


_UNCFG6_RE = re.compile(r"^\s*gpon[-_]olt[-_](\d+)/(\d+)/(\d+)\s+(?:(\S+)\s+)?([A-Za-z0-9]{8,20})\s*(.*)$", re.I)


def parse_uncfg(text: str) -> list[dict]:
    """Unregistered ONUs (SN + PON position). Two formats: `show gpon onu uncfg`
    (`gpon-onu_1/2/1:1  SN  state`) dan C600 `show pon onu uncfg` (`gpon_olt-1/5/1  Model  SN`, tanpa nomor ONU)."""
    rows = []
    for line in text.splitlines():
        m6 = _UNCFG6_RE.match(line)
        if m6:
            shelf, slot, pon, model, sn, rest = m6.groups()
            rows.append({"shelf": int(shelf), "slot": int(slot), "pon": int(pon), "index": 0, "sn": sn,
                         "state": "", "model": model or ""})
            continue
        m = _UNCFG_RE.search(line.strip())
        if m:
            shelf, slot, pon, idx, sn, rest = m.groups()
            rows.append({"shelf": int(shelf), "slot": int(slot), "pon": int(pon), "index": int(idx),
                         "sn": sn, "state": rest.split()[-1] if rest.split() else ""})
    return rows


def parse_eth_ports(text: str) -> list[dict]:
    """`show gpon remote-onu interface eth gpon-onu_X`: blok per port (Interface/Admin/Operate/Speed)."""
    ports, cur = [], None
    for line in text.splitlines():
        m = re.match(r"\s*Interface\s*:\s*(\S+)", line, re.I)
        if m:
            cur = {"port": m.group(1), "admin": "", "oper": "", "speed": ""}
            ports.append(cur)
            continue
        if cur is None:
            continue
        for key, rx in (("admin", r"Admin status\s*:\s*(\S+)"), ("oper", r"Operate status\s*:\s*(\S+)"),
                        ("speed", r"Speed status\s*:\s*(\S+)")):
            m = re.search(rx, line, re.I)
            if m:
                cur[key] = m.group(1)
    return ports


_MODE_LABEL = {"hybrid": "Hybrid", "tag": "Access", "trunk": "Trunk", "transparent": "Transparent"}


def parse_eth_modes(text: str) -> dict[str, dict]:
    """`vlan port eth_0/1 mode hybrid def-vlan 1067` / `mode transparent` -> {port: {mode, vlan}}.
    A port without a `vlan port` line is in LAN (router) mode."""
    out: dict[str, dict] = {}
    for m in re.finditer(r"vlan port (eth_\d+/\d+) mode (hybrid|tag|trunk|transparent)(?: def-vlan (\d+))?(?: vlan ([\d,\-]+))?", text):
        out[m.group(1)] = {"mode": _MODE_LABEL[m.group(2)], "vlan": m.group(3) or m.group(4) or ""}
    return out


# ───────────────────── MAC address ─────────────────────
_MAC_RE = re.compile(r"(?<![0-9A-Fa-f.:\-])(?:[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}\.[0-9A-Fa-f]{4}"
                     r"|[0-9A-Fa-f]{12}"
                     r"|(?:[0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2})(?![0-9A-Fa-f:\-])")
_MAC_TYPES = ("dynamic", "static", "secure", "blackhole", "permanent", "learned")


def format_mac(raw: str) -> str:
    """aabb.ccdd.eeff / aa-bb-cc-dd-ee-ff / aabbccddeeff -> AA:BB:CC:DD:EE:FF. Non-MAC values are returned unchanged."""
    h = re.sub(r"[^0-9A-Fa-f]", "", raw or "")
    if len(h) != 12 or re.search(r"[^0-9A-Fa-f.:\-\s]", raw):
        return raw
    return ":".join(h[i:i + 2] for i in range(0, 12, 2)).upper()


def parse_mac_table(text: str) -> list[dict]:
    """`show mac gpon onu ...` / `show mac interface vport-...` -> [{mac, vlan, type, info}]. OLT MAC formats may be
    xxxx.xxxx.xxxx or xx:xx:xx:xx:xx:xx; results are always XX:XX:XX:XX:XX:XX. Other columns are loosely inferred
    (VLAN = first number from 1-4094 after the MAC); the original row is retained in `info`."""
    rows, seen = [], set()
    for line in text.splitlines():
        m = _MAC_RE.search(line)
        if not m:
            continue
        rest = (line[:m.start()] + " " + line[m.end():]).split()
        vlan = next((int(t) for t in rest if t.isdigit() and 1 <= int(t) <= 4094), None)
        typ = next((t.capitalize() for t in rest if t.lower() in _MAC_TYPES), "")
        mac = format_mac(m.group())
        if (mac, vlan) in seen:
            continue
        seen.add((mac, vlan))
        info = " ".join(t for t in rest if not (t.isdigit() and int(t) == vlan) and t.capitalize() != typ)
        rows.append({"mac": mac, "vlan": vlan, "type": typ, "info": info})
    return rows


# ───────────────────── ONU types defined on the OLT ─────────────────────
def _type_entry(desc: str = "") -> dict:
    """Infer port count from descriptions such as '4ETH,2POTS,WIFI'; default to 4 Ethernet ports if unavailable."""
    eth = re.search(r"(\d+)\s*(?:ETH|LAN|FE|GE)\b", desc, re.I)
    pots = re.search(r"(\d+)\s*(?:POTS|VOIP|FXS)\b", desc, re.I)
    return {"eth": int(eth.group(1)) if eth else 4, "wifi": 1 if re.search(r"wifi|wlan", desc, re.I) else 0,
            "pots": int(pots.group(1)) if pots else 0, "rf": 1 if re.search(r"\b(?:rf|catv)\b", desc, re.I) else 0,
            "description": desc}


def parse_onu_types(text: str) -> dict[str, dict]:
    """`show onu-type gpon` -> {type_name: {eth, wifi, pots, rf, description}}.
    Firmware format is unverified, so accept two common forms: an 'ONU type name: X' block (+ 'Description: ...')
    and table rows 'X  gpon ...'. Ignore other text (return empty results rather than false values)."""
    out: dict[str, dict] = {}
    cur = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("-", "%")):
            continue
        m = re.match(r"(?i)onu[\s_-]*type(?:[\s_-]*name)?\s*[:=]\s*([A-Za-z0-9][\w\-.]{0,59})\s*$", line)
        if m:
            cur = m.group(1)
            out.setdefault(cur, _type_entry())
            continue
        m = re.match(r"(?i)name\s*:\s*([A-Za-z0-9][\w\-.]{0,59})\s*$", line)      # `show onu-type gpon NAME` (C600)
        if m:
            cur = m.group(1)
            out.setdefault(cur, _type_entry())
            continue
        m = re.match(r"(?i)description\s*[:=]\s*(.+)$", line)
        if m and cur:
            out[cur] = _type_entry(m.group(1).strip())
            continue
        m = re.match(r"^([A-Za-z0-9][\w\-.]{1,59})\s+gpon\b\s*(.*)$", line, re.I)
        if m and m.group(1).lower() not in ("onu", "type", "name", "pon"):
            cur = m.group(1)
            out.setdefault(cur, _type_entry(m.group(2).strip()))
    return out


def parse_profile_names(text: str) -> list[str]:
    """`show gpon profile tcont|traffic` -> profile names ('Profile name :200M'). Unknown format returns an empty list."""
    return re.findall(r"(?im)^\s*profile\s+name\s*:\s*(\S+)", text)
