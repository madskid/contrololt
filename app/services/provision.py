"""OLT changes: command plan (preview) -> validation -> staged execution + rollback + audit.

Registration, speed, and VLAN templates follow real C300 configuration patterns (running-config lines can be replayed
as commands). Reboot, factory restore, Ethernet port lock, and `no onu` follow the supplied CLI references.
Unverified commands are flagged in the preview. Every line is validated against an allowlist."""
import re
from dataclasses import dataclass, field

from app.drivers import zte, zte_parsers as P


class PlanError(ValueError):
    pass


# ───────────────────────── input sanitization (prevent CLI command injection) ─────────────────────────
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def text(v, name: str, maxlen: int = 64, required: bool = False) -> str:
    v = ("" if v is None else str(v)).strip()
    if _CTRL.search(v) or "?" in v:
        raise PlanError(f"{name}: contains disallowed characters (control characters/'?')")
    if required and not v:
        raise PlanError(f"{name} is required")
    return v[:maxlen]


def ident(v, name: str, maxlen: int = 40) -> str:
    v = ("" if v is None else str(v)).strip()
    if not re.fullmatch(r"[\w.\-]{1,%d}" % maxlen, v):
        raise PlanError(f"{name}: use only letters, numbers, '_', '-', '.' (max {maxlen} characters)")
    return v


def serial(v) -> str:
    v = ("" if v is None else str(v)).strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{8,16}", v):
        raise PlanError("SN must contain 8-16 alphanumeric characters")
    return v


def vlan_id(v, name: str = "VLAN") -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise PlanError(f"{name} must be a number")
    if not 1 <= n <= 4094:
        raise PlanError(f"{name} must be between 1 and 4094")
    return n


def vlan_spec(v, name: str = "VLAN") -> str:
    v = ("" if v is None else str(v)).replace(" ", "")
    if not re.fullmatch(r"\d{1,4}(-\d{1,4})?(,\d{1,4}(-\d{1,4})?)*", v):
        raise PlanError(f"{name}: expected format such as 700 or 700,710-712")
    for tok in re.split(r"[,\-]", v):
        vlan_id(tok, name)
    return v


def mask(s: str) -> str:
    return re.sub(r"(password\s+)\S+", r"\1****", s, flags=re.I)


# Allowlist: every command line must match one of the patterns exactly.
_ALLOWED = [re.compile(x) for x in [
    r"interface (?:gpon[-_]olt[-_]\d+/\d+/\d+|gpon[-_]onu[-_]\d+/\d+/\d+:\d+|x?gei[-_]\d+/\d+/\d+"
    r"|vport[-_]\d+/\d+/\d+\.\d+:\d+)",
    r"interface eth eth_\d+/\d+ state (?:lock|unlock)",
    r"pon-onu-mng gpon[-_]onu[-_]\d+/\d+/\d+:\d+",
    r"onu \d{1,3} type \S+ sn [A-Za-z0-9]{8,16}", r"no onu \d{1,3}", r"(?:no )?shutdown",
    r"description [^\x00-\x1f?]{1,64}",
    r"tcont \d+ (?:name \S+ )?profile \S+",
    r"tcont \d+ gap mode\d+",
    r"gemport \d+ name \S+ (?:tcont \d+|unicast tcont \d+ dir (?:both|upstream|downstream))",
    r"gemport \d+ traffic-limit (?:upstream \S+ )?downstream \S+",
    r"encrypt \d+ enable downstream",
    r"switchport mode (?:hybrid|trunk|access) vport \d+",
    r"service-port \d+ vport \d+ user-vlan \d+ vlan \d+(?: svlan \d+)?",
    r"service-port \d+ user-vlan \d+ vlan \d+(?: svlan \d+)?",          # C6xx: di interface vport
    r"gemport \d+ tcont \d+",                                              # C6xx
    r"service \S+ gemport \d+(?: cos \d+)? vlan \d+",
    r"wan-ip ipv4 mode pppoe username \S+ password \S+ vlan-profile \S+ host \d+",
    r"vlan port eth_\d+/\d+ mode (?:hybrid def-vlan \d+|transparent|tag vlan \d+(?: pri(?:ority)? \d+)?|trunk vlan [\d,\-]+)",
    r"vlan port veip_\d+ mode hybrid",
    r"no vlan port eth_\d+/\d+ mode", r"no service-port \d+",
    r"flow mode \d+ tag-filter (?:vlan-filter|vid-filter) untag-filter (?:discard|transparent)",
    r"flow \d+ (?:pri|priority) \d+ (?:vlan|vid) \d+", r"gemport \d+ flow \d+", r"switchport-bind switch_\d+/\d+ iphost \d+",
    r"pppoe \d+ nat enable user \S+ password \S+",
    r"security-mgmt \d+ state enable mode forward protocol web",
    r"onu-vlan iphost \d+ up-mode add (?:up-pri|up-prio) \d+ (?:up-vlan|up-vid) \d+ down-mode transparent",
    r"vlan-filter-mode iphost \d+ tag-filter (?:vlan-filter|vid-filter) untag-filter (?:transparent|discard)",
    r"vlan-filter iphost \d+ (?:pri|priority) \d+ (?:vlan|vid) \d+",
    r"mgmt-ip \S+ \S+ vlan \d+ priority \d+ route \S+ \S+ \S+ host \d+",
    r"ip-host \d+ dhcp-enable (?:true|false)(?: ping-response (?:true|false))?(?: traceroute-response (?:true|false))?",
    r"reboot", r"restore factory", r"exit",
    r"vlan database", r"vlan \d[\d,\-]*", r"no vlan \d[\d,\-]*", r"name [^\x00-\x1f?]{1,64}",
    r"switchport vlan \d[\d,\-]* tag", r"no switchport vlan \d[\d,\-]* tag",
    r"gpon", r"profile tcont \S+ type \d+ fixed \d+", r"profile traffic \S+ sir \d+ pir \d+",
]]


def validate_commands(cmds: list[str]) -> list[str]:
    out = []
    if not cmds:
        raise PlanError("Command list is empty")
    for c in cmds:
        c = c.strip()
        if not c:
            continue
        if len(c) > 300 or _CTRL.search(c):
            raise PlanError(f"Invalid command: {mask(c)[:60]!r}")
        if not any(rx.fullmatch(c) for rx in _ALLOWED):
            raise PlanError(f"Command is not in the allowlist: {mask(c)[:80]!r}")
        out.append(c)
    if not out:
        raise PlanError("Command list is empty")
    return out


# ───────────────────────── rencana ─────────────────────────
@dataclass
class Plan:
    action: str
    title: str
    target: str
    commands: list[str]
    rollback: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    dangerous: bool = False
    meta: dict = field(default_factory=dict)

    def public(self) -> dict:
        return {"action": self.action, "title": self.title, "target": self.target, "commands": self.commands,
                "rollback": self.rollback, "warnings": self.warnings, "dangerous": self.dangerous, "meta": self.meta}


def _olt_if(olt, slot: int, pon: int) -> str:
    return zte.profile_for(olt.cli_family).olt_if.format(shelf=olt.shelf, slot=slot, pon=pon)


def _onu_if(olt, onu) -> str:
    return zte.profile_for(olt.cli_family).onu_if.format(shelf=onu.shelf, slot=onu.slot, pon=onu.pon, onu=onu.onu_number)


def _prof(olt):
    return zte.profile_for(olt.cli_family)


def _vport_if(olt, shelf, slot, pon, onu) -> str:
    return zte.vport_if(_prof(olt), shelf, slot, pon, onu)


def next_free(used: set[int], cap: int = 128) -> int:
    for n in range(1, cap + 1):
        if n not in used:
            return n
    raise PlanError(f"PON port is full ({cap} ONUs)")


def bridge_lines(svc: str, vlan: int, eth_ports: int, prof: zte.Profile | None = None) -> list[str]:
    prof = prof or zte.PROFILES["legacy"]
    return [prof.reg_service.format(svc=svc, vlan=vlan)] + [
        prof.reg_eth_bridge.format(i=i, vlan=vlan) for i in range(1, max(1, int(eth_ports or 1)) + 1)]


def routing_lines(vlan: int, user: str, pw: str, prof: zte.Profile | None = None,
                  svc: str = "ServiceName") -> list[str]:
    """Pola PPPoE (OMCI) persis seperti ONU routing di config OLT Anda."""
    prof = prof or zte.PROFILES["legacy"]
    if prof.reg_routing_service and prof.reg_pppoe_wan:
        return [prof.reg_routing_service.format(svc=svc, vlan=vlan),
                prof.reg_pppoe_wan.format(user=user, pw=pw, vlan=vlan,
                                          vlan_profile=prof.pppoe_vlan_profile)]
    routing_filter = prof.reg_routing_filter
    priority_keyword = prof.reg_routing_priority_keyword
    vlan_keyword = prof.reg_routing_vlan_keyword
    return [f"flow mode 1 tag-filter {routing_filter} untag-filter discard",
            f"flow 1 {priority_keyword} 0 {vlan_keyword} {vlan}",
            "gemport 1 flow 1", "switchport-bind switch_0/1 iphost 1", f"pppoe 1 nat enable user {user} password {pw}",
            f"onu-vlan iphost 1 up-mode add up-pri 0 up-vlan {vlan} down-mode transparent",
            f"vlan-filter-mode iphost 1 tag-filter {routing_filter} untag-filter transparent",
            f"vlan-filter iphost 1 {priority_keyword} 0 {vlan_keyword} {vlan}"]


def pppoe_creds(p: dict) -> tuple[str, str]:
    user = ident(p.get("pppoe_user") or p.get("username"), "PPPoE username", 60) if (p.get("pppoe_user") or p.get("username")) else ""
    pw = text(p.get("pppoe_password") or p.get("password"), "PPPoE password", 64)
    if not user or not pw or re.search(r"\s", pw):
        raise PlanError("Routing mode: enter a PPPoE username and password (no spaces)")
    return user, pw


def plan_register(olt, p: dict, ctx: dict) -> Plan:
    slot, pon = int(p["slot"]), int(p["pon"])
    sn, otype = serial(p.get("sn")), ident(p.get("onu_type"), "ONU type")
    name = text(p.get("name"), "Name", 64) or sn
    vlan = vlan_id(p.get("vlan"), "User VLAN")
    mode = p.get("mode") or "bridge"
    if mode not in ("bridge", "onu_pppoe", "routing"):
        raise PlanError("Mode must be bridge, onu_pppoe, or routing")
    svc = ident(p.get("service") or "INT", "Service name")
    ul, dl = ctx.get("ul_name"), ctx.get("dl_name")
    if not ul or not dl:
        raise PlanError("Select download and upload speed profiles")
    if ctx.get("sn_exists"):
        raise PlanError(f"SN {sn} is already registered in the dashboard ({ctx['sn_exists']})")
    n = next_free(ctx.get("used", set()))
    prof = _prof(olt)
    o_if, n_if = _olt_if(olt, slot, pon), prof.onu_if.format(shelf=olt.shelf, slot=slot, pon=pon, onu=n)
    sp = prof.reg_service_port.format(vlan=vlan)
    onu_cfg = [f"name {name}", f"description {name}", prof.reg_tcont.format(svc=svc, ul=ul)]
    if mode == "onu_pppoe":
        if not prof.reg_encrypt:
            raise PlanError("This OLT profile does not define the required ONU-PPPoE bridge commands")
    onu_cfg.append(prof.reg_gemport.format(svc=svc))
    if mode == "onu_pppoe":
        onu_cfg.append(prof.reg_encrypt)
    if prof.reg_dl_limit:
        onu_cfg.append(prof.reg_dl_limit.format(dl=dl))
    if prof.reg_hybrid:
        onu_cfg.append(prof.reg_hybrid)
    cmds = [f"interface {o_if}", f"onu {n} type {otype} sn {sn}", "exit", f"interface {n_if}", *onu_cfg]
    if prof.vport_if:      # C6xx: service-port is under the vport interface.
        cmds += ["exit", f"interface {_vport_if(olt, olt.shelf, slot, pon, n)}", sp, "exit"]
    else:
        cmds += [sp, "exit"]
    cmds.append(f"pon-onu-mng {n_if}")
    warns = []
    warns.append(f"Upload profile '{ul}' and download profile '{dl}' must exist on the OLT; this is checked automatically before sending commands.")
    if prof.vport_if and not prof.reg_dl_limit:
        warns.append(f"C6xx style: download limit ({dl}) is not sent because the command is not documented for C600; "
                     "only the upload profile (tcont) is applied. Set 'reg_dl_limit' in data/profiles.json if your firmware supports it.")
    if mode == "bridge":
        cmds += bridge_lines(svc, vlan, ctx.get("eth_ports"), prof)
    elif mode == "onu_pppoe":
        cmds += [prof.reg_service.format(svc=svc, vlan=vlan), prof.reg_veip_bridge]
        warns.append("Bridge mode: configure PPPoE WAN credentials from the ONU web interface; the OLT only passes the VLAN.")
    else:
        user, pw = pppoe_creds(p)
        if prof.reg_routing_service and prof.reg_pppoe_wan:
            cmds += routing_lines(vlan, user, pw, prof=prof, svc=svc)
        else:
            cmds += routing_lines(vlan, user, pw, prof=prof)
        warns.append("The routing template follows the PPPoE ONU example in your config; compare it with an existing routing ONU."
                     + (" Template C6xx mengikuti contoh konfigurasi Anda." if prof.vport_if else ""))
    if prof.reg_web_mgmt:
        cmds.append(prof.reg_web_mgmt)
    cmds.append("exit")
    if ctx.get("type_unknown"):
        warns.append(f"ONU type '{otype}' is not registered in the dashboard type list; make sure it exists on the OLT (`onu-type`).")
    if len(str(p.get("name") or "")) > 64:
        warns.append("Name is truncated to 64 characters for the description.")
    return Plan("register_onu", f"Register ONU {sn}", f"{n_if}", cmds,
                rollback=[f"interface {o_if}", f"no onu {n}", "exit"], warnings=warns,
                meta={"number": n, "slot": slot, "pon": pon, "sn": sn, "vlan": vlan,
                      "mode": "bridge" if mode == "onu_pppoe" else mode, "name": name})


def _onu_plan(action, title, onu, olt, cmds, dangerous=False, warns=None, meta=None) -> Plan:
    return Plan(action, title, _onu_if(olt, onu), cmds, warnings=warns or [], dangerous=dangerous, meta=meta or {})


def plan_delete(olt, onu, p, ctx) -> Plan:
    return _onu_plan("delete_onu", f"Delete ONU {onu.sn or onu.onu_number} from OLT", onu, olt,
                     [f"interface {_olt_if(olt, onu.slot, onu.pon)}", f"no onu {onu.onu_number}", "exit"], dangerous=True,
                     warns=["The ONU configuration will be deleted from the OLT. The subscriber will be disconnected."])


def plan_reboot(olt, onu, p, ctx) -> Plan:
    return _onu_plan("reboot_onu", "Reboot ONU", onu, olt, [f"pon-onu-mng {_onu_if(olt, onu)}", "reboot", "exit"])


def plan_rename(olt, onu, p, ctx) -> Plan:
    raw_name = ("" if p.get("name") is None else str(p.get("name"))).strip()
    if len(raw_name) > 64:
        raise PlanError("Name must be 64 characters or fewer to fit the OLT CLI limit")
    name = text(raw_name, "Name", 64, required=True)
    return _onu_plan("rename_onu", f"Rename ONU to {name}", onu, olt,
                     [f"interface {_onu_if(olt, onu)}", f"name {name}", f"description {name}", "exit"])


def plan_restore(olt, onu, p, ctx) -> Plan:
    return _onu_plan("restore_onu", "Restore factory ONU", onu, olt, [f"pon-onu-mng {_onu_if(olt, onu)}", "restore factory", "exit"],
                     dangerous=True, warns=["The modem's WAN/WiFi settings will be restored to factory defaults."])


def plan_admin(enable: bool):
    """Disable an ONU by locking all Ethernet ports; the ONU remains registered and connected."""
    def f(olt, onu, p, ctx) -> Plan:
        n = max(1, int(ctx.get("eth_ports") or 4))
        state = "unlock" if enable else "lock"
        warns = []
        if ctx.get("eth_assumed"):
            warns.append(f"The Ethernet port count for this ONU type is unknown; assuming {n}. Adjust the preview or update Settings → ONU types.")
        return _onu_plan("enable_onu" if enable else "disable_onu", ("Enable" if enable else "Disable") + " ONU (port Ethernet)", onu, olt,
                         [f"pon-onu-mng {_onu_if(olt, onu)}"] + [f"interface eth eth_0/{i} state {state}" for i in range(1, n + 1)] + ["exit"],
                         dangerous=not enable, warns=warns + ([] if enable else ["All ONU Ethernet ports will be disabled; the subscriber will lose service until re-enabled."]))
    return f


def plan_eth(olt, onu, p, ctx) -> Plan:
    port, state = str(p.get("port") or ""), str(p.get("state") or "")
    if not re.fullmatch(r"eth_\d+/\d+", port) or state not in ("lock", "unlock"):
        raise PlanError("port must look like eth_0/1 and state must be lock/unlock")
    return _onu_plan("eth_state", f"{state.capitalize()} port {port}", onu, olt,
                     [f"pon-onu-mng {_onu_if(olt, onu)}", f"interface eth {port} state {state}", "exit"])


def plan_eth_config(olt, onu, p, ctx) -> Plan:
    """Configure ethernet port (dialog SmartOLT): Status Enabled/Port shutdown + Mode LAN/Access/Hybrid/Trunk/Transparent."""
    port = str(p.get("port") or "")
    if not re.fullmatch(r"eth_\d+/\d+", port):
        raise PlanError("port must look like eth_0/1")
    status, mode = str(p.get("status") or "enabled"), str(p.get("mode") or "")
    if status not in ("enabled", "shutdown"):
        raise PlanError("status must be enabled or shutdown")
    cmds, warns = [f"pon-onu-mng {_onu_if(olt, onu)}", f"interface eth {port} state {'unlock' if status == 'enabled' else 'lock'}"], []
    if mode:
        if mode == "hybrid":
            cmds.append(f"vlan port {port} mode hybrid def-vlan {vlan_id(p.get('vlan'), 'VLAN')}")
        elif mode == "transparent":
            cmds.append(f"vlan port {port} mode transparent")
        elif mode == "access":
            cmds.append(f"vlan port {port} mode tag vlan {vlan_id(p.get('vlan'), 'VLAN')}")
            warns.append("Access mode uses 'mode tag vlan' (not found in your config examples); review the preview.")
        elif mode == "trunk":
            cmds.append(f"vlan port {port} mode trunk vlan {vlan_spec(p.get('vlan'), 'VLAN')}")
            warns.append("Trunk mode uses 'mode trunk vlan' (not found in your config examples); review the preview.")
        elif mode == "lan":
            cmds.append(f"no vlan port {port} mode")
            warns.append("LAN (router) mode omits 'vlan port'; removal uses 'no vlan port … mode' (unverified).")
        else:
            raise PlanError("mode must be lan/access/hybrid/trunk/transparent")
    cmds.append("exit")
    return _onu_plan("eth_config", f"Configure ethernet port {port}", onu, olt, cmds, warns=warns)


def plan_onu_mode(olt, onu, p, ctx) -> Plan:
    """Update ONU mode: WAN VLAN-ID, Routing/Bridging, PPPoE (OMCI, IPv4), username, and password."""
    vlan = vlan_id(p.get("wan_vlan"), "WAN VLAN-ID")
    mode = p.get("mode") or "routing"
    if mode not in ("routing", "bridge"):
        raise PlanError("ONU mode must be routing or bridge")
    if p.get("method", "omci") != "omci":
        raise PlanError("TR-069 config method is not supported yet (requires ACS); use OMCI")
    if p.get("ip_protocol", "ipv4") != "ipv4":
        raise PlanError("Dual-stack IPv4/IPv6 is not supported yet")
    if mode == "routing" and p.get("wan_mode", "pppoe") != "pppoe":
        raise PlanError("WAN DHCP/Static IP mode is not supported yet (no OLT config example is available); use PPPoE")
    svc = ident(p.get("service") or "INT", "Service name")
    n_if = _onu_if(olt, onu)
    prof = _prof(olt)
    sp_if = _vport_if(olt, onu.shelf, onu.slot, onu.pon, onu.onu_number) or n_if      # C6xx: service-port di interface vport
    cmds = [f"interface {sp_if}", "no service-port 1", prof.reg_service_port.format(vlan=vlan), "exit", f"pon-onu-mng {n_if}"]
    user = ""
    if mode == "routing":
        user, pw = pppoe_creds(p)
        cmds += routing_lines(vlan, user, pw, prof=prof, svc=svc)
    else:
        cmds += bridge_lines(svc, vlan, ctx.get("eth_ports"), prof)
    cmds.append("exit")
    return _onu_plan("onu_mode", "Update ONU mode", onu, olt, cmds, dangerous=True,
                     warns=["Changing the mode briefly interrupts service. Previous-mode lines (e.g. 'service'/'vlan port' when switching to routing) are not removed automatically; review the preview.",
                            "'no service-port 1' before recreating it is unverified in your config."],
                     meta={"vlan": vlan, "mode": mode, "user": user})


def plan_pppoe_credentials(olt, onu, p, ctx) -> Plan:
    if onu.mode != "routing":
        raise PlanError("PPPoE credentials can only be updated when the ONU is in routing mode")
    user, pw = pppoe_creds(p)
    prof = _prof(olt)
    if prof.reg_pppoe_wan:
        command = prof.reg_pppoe_wan.format(
            user=user, pw=pw, vlan=onu.vlan, vlan_profile=prof.pppoe_vlan_profile)
    else:
        command = f"pppoe 1 nat enable user {user} password {pw}"
    return _onu_plan("pppoe_credentials", "Update PPPoE credentials", onu, olt,
                     [f"pon-onu-mng {_onu_if(olt, onu)}", command, "exit"],
                     meta={"user": user})


def plan_apply_speed(olt, onu, p, ctx) -> Plan:
    if not ctx.get("ul_name") or not ctx.get("dl_name"):
        raise PlanError("Select download and upload speed profiles")
    prof = _prof(olt)
    cmds = [f"interface {_onu_if(olt, onu)}", f"tcont 1 profile {ctx['ul_name']}"]
    warns = ["The profiles must already exist on the OLT (use 'Push to OLT' in Speed profiles if needed)."]
    if prof.reg_dl_limit:
        cmds.append(prof.reg_dl_limit.format(dl=ctx["dl_name"]))
    else:
        warns.append("C6xx style: the download limit is not sent (the command is not documented for C600); only the upload profile is applied.")
    return _onu_plan("apply_speed", "Apply speed profile to OLT", onu, olt, cmds + ["exit"],
                     warns=warns,
                     meta={"dl_profile_id": p.get("dl_profile_id"), "ul_profile_id": p.get("ul_profile_id")})


def plan_vlan_add(olt, onu, p, ctx) -> Plan:
    """Create a VLAN in the OLT database (`vlan database`); optionally tag selected uplink ports."""
    spec = vlan_spec(p.get("vlans"))
    name = text(p.get("name"), "VLAN name", 32)
    prof = _prof(olt)
    if name and not re.fullmatch(r"\d+", spec):
        raise PlanError("A VLAN name can only be specified for a single VLAN ID")
    if prof.vlan_db:
        cmds = ["vlan database", f"vlan {spec}", "exit"]
        if name:
            cmds += [f"vlan {spec}", f"name {name}", "exit"]
    else:                                              # C6xx: use `vlan N` directly in configuration mode.
        cmds = [f"vlan {spec}"] + ([f"name {name}"] if name else []) + ["exit"]
    ports = p.get("uplink_ports") or []
    if isinstance(ports, str):
        ports = [x for x in ports.split(",") if x]
    for port in ports:
        if not re.fullmatch(r"x?gei[-_]\d+/\d+/\d+", str(port)):
            raise PlanError(f"Invalid uplink port: {str(port)[:30]}")
        cmds += [f"interface {port}", f"switchport vlan {spec} tag", "exit"]
    return Plan("vlan_add", f"Add VLAN {spec}" + (f" + tag on {len(ports)} uplink ports" if ports else ""), olt.name, cmds,
                warnings=(["Creating a VLAN on C6xx uses 'vlan N' directly (not documented in the guide); review the preview."]
                          if not prof.vlan_db else
                          ["'vlan database' follows your OLT running-config structure; review the preview."]))


def plan_uplink_vlan(olt, onu, p, ctx) -> Plan:
    port, op = str(p.get("port") or ""), p.get("op") or "add"
    if not re.fullmatch(r"x?gei[-_]\d+/\d+/\d+", port) or op not in ("add", "remove"):
        raise PlanError("Uplink port must look like xgei_1/20/1 and operation must be add/remove")
    spec = vlan_spec(p.get("vlans"))
    return Plan("uplink_vlan", f"{'Add' if op == 'add' else 'Remove'} VLAN tag {spec} on {port}", port,
                [f"interface {port}", ("" if op == "add" else "no ") + f"switchport vlan {spec} tag", "exit"],
                dangerous=op == "remove",
                warnings=["Removing this uplink VLAN tag may disconnect all subscribers on that VLAN."] if op == "remove" else [])


def plan_push_speed(olt, onu, p, ctx) -> Plan:
    prof = ctx.get("profile")
    if not prof:
        raise PlanError("Speed profile not found")
    name, kbps = ident(prof["name"], "Profile name", 60), int(prof["kbps"])
    line = (f"profile tcont {name} type 1 fixed {kbps}" if prof["direction"] == "upload"
            else f"profile traffic {name} sir {kbps} pir {kbps}")
    return Plan("push_speed", f"Create profile {name} on OLT", olt.name, ["gpon", line, "exit"],
                warnings=["The 'gpon' mode command follows the running-config structure; review the preview."])


ONU_ACTIONS = {"delete_onu": plan_delete, "reboot_onu": plan_reboot, "restore_onu": plan_restore, "disable_onu": plan_admin(False),
               "enable_onu": plan_admin(True), "eth_state": plan_eth, "eth_config": plan_eth_config, "onu_mode": plan_onu_mode,
               "pppoe_credentials": plan_pppoe_credentials, "apply_speed": plan_apply_speed, "rename_onu": plan_rename}
OLT_ACTIONS = {"vlan_add": plan_vlan_add, "uplink_vlan": plan_uplink_vlan, "push_speed": plan_push_speed}


async def verify_speed_profiles(cli, olt, ul: str | None, dl: str | None) -> None:
    """Read-only check BEFORE changing configuration: selected upload (tcont) and download (traffic) profiles must exist on the OLT.
    If the profile list cannot be read (rejected command or unknown format), skip the check instead of blocking the action."""
    prof = _prof(olt)
    checks = [("upload (tcont)", prof.tcont_list_cmd, ul)]
    if prof.reg_dl_limit:
        checks.append(("download (traffic)", prof.traffic_list_cmd, dl))
    missing = []
    for label, cmd, name in checks:
        if not cmd or not name:
            continue
        r = await cli.run(cmd, overall=30, idle=10)
        names = P.parse_profile_names(r.text) if not r.error else []
        if names and name not in names:
            shown = ", ".join(names[:12]) + (" …" if len(names) > 12 else "")
            missing.append(f"{label} '{name}' (available on OLT: {shown})")
    if missing:
        raise PlanError("Speed profile(s) missing from the OLT -> " + "; ".join(missing) +
                        ". Select another profile or create it first in Settings → Speed profiles → Push to OLT. No changes were sent.")


# ───────────────────────── eksekusi ─────────────────────────
@dataclass
class ExecResult:
    ok: bool
    log: list[dict]
    rolled_back: bool = False
    message: str = ""
    replaced: tuple | None = None


async def run_plan(conn, commands: list[str], rollback: list[str] | None = None, pre=None) -> ExecResult:
    """Enter `configure terminal`, run commands one at a time, stop at the first error (then roll back
    if applicable), and ALWAYS exit with `end`. Try an alternative flow VLAN keyword only if the OLT explicitly
    rejects the original. `pre(cli)` is a read-only check before changing configuration.

    Safety: run rollback `no onu N` only if `onu N type … sn …` succeeded in this execution.
    If that command is rejected (for example, the ONU number is already in use) or its result is uncertain
    (timeout), do not roll back, to avoid deleting an ONU that was not created by this operation."""
    log: list[dict] = []
    async with zte.links.session(conn, wait=90) as cli:
        last = {}

        async def step(cmd: str, rb: bool = False):
            r = await cli.run(cmd, overall=40, idle=20, confirm=True)
            bad = r.error or not r.complete
            last["r"] = r
            entry = {"cmd": mask(cmd), "output": mask(r.text)[:600], "error": bad, "rollback": rb}
            log.append(entry)
            return bad

        def alternate_vlan_keyword(cmd: str) -> str | None:
            match = re.fullmatch(
                r"(flow \d+ (?:pri|priority) \d+|vlan-filter iphost \d+ (?:pri|priority) \d+) (vlan|vid) (\d+)",
                cmd.strip(),
            )
            if not match:
                return None
            keyword = "vid" if match.group(2) == "vlan" else "vlan"
            return f"{match.group(1)} {keyword} {match.group(3)}"

        if pre:
            repl = await pre(cli)
            if repl:
                commands, rollback = repl
        mo = next((re.fullmatch(r"no onu (\d+)", c.strip()) for c in (rollback or []) if c.strip().startswith("no onu ")), None)
        arm = re.compile(rf"onu {mo.group(1)} type \S+ sn \S+") if mo else None     # None = rollback tak butuh pengaman
        armed = arm is None
        ok, rolled, msg = True, False, "Done"
        try:
            if await step("configure terminal"):
                ok, msg = False, "Failed to enter configuration mode"
            else:
                for i, c in enumerate(commands):
                    bad = await step(c)
                    alternate = alternate_vlan_keyword(c) if bad and last["r"].error and last["r"].complete else None
                    if alternate:
                        log[-1]["fallback_to"] = mask(alternate)
                        bad = await step(alternate)
                        c, commands[i] = alternate, alternate
                    if bad:
                        ok, msg = False, f"Command rejected by OLT: {mask(c)}"
                        if arm and arm.fullmatch(c.strip()) and not last["r"].error:
                            msg += " (result is uncertain; check the ONU on the OLT manually)"
                        break
                    if arm and arm.fullmatch(c.strip()):
                        armed = True
                if not ok and rollback and not armed:
                    msg += " — rollback was not run: this action did not create the ONU, so there is nothing to undo"
                if not ok and rollback and armed:
                    # A failed command can leave the session in a sub-mode (e.g. ONU interface); the OLT rejects
                    # `interface …` there, so rollback must start from a clean configuration mode.
                    await step("end", rb=True)
                    await step("configure terminal", rb=True)
                    for c in rollback:
                        await step(c, rb=True)
                    rolled = True
        finally:
            try:
                await cli.run("end", overall=10, idle=5)
            except Exception:
                pass
    return ExecResult(ok, log, rolled, msg, replaced=(commands, rollback))
