from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer,
                        String, Text, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    """Naive UTC timestamp (consistent with SQLite and PostgreSQL)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class OLT(Base):
    __tablename__ = "olts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    ip: Mapped[str] = mapped_column(String(64), unique=True)
    model: Mapped[str] = mapped_column(String(50), default="C320")        # C300 / C320 / ... / custom text
    cli_family: Mapped[str] = mapped_column(String(10), default="legacy")  # legacy | c6xx (CLI interface naming style)
    shelf: Mapped[int] = mapped_column(Integer, default=1)

    telnet_port: Mapped[int] = mapped_column(Integer, default=23)
    telnet_user: Mapped[str] = mapped_column(String(100), default="")
    telnet_pass_enc: Mapped[str] = mapped_column(Text, default="")
    enable_pass_enc: Mapped[str] = mapped_column(Text, default="")
    snmp_community_enc: Mapped[str] = mapped_column(Text, default="")
    snmp_port: Mapped[int] = mapped_column(Integer, default=161)

    status: Mapped[str] = mapped_column(String(20), default="pending")    # pending|syncing|online|offline
    status_msg: Mapped[str] = mapped_column(String(300), default="")
    sys_descr: Mapped[str] = mapped_column(String(300), default="")
    uptime_s: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    poll_source: Mapped[str] = mapped_column(String(10), default="")       # snmp | cli
    sw_version: Mapped[str] = mapped_column(String(40), default="")
    rx_warn: Mapped[float] = mapped_column(Float, default=-30.0)           # ONU signal warning threshold (dBm)
    rx_crit: Mapped[float] = mapped_column(Float, default=-32.0)
    temp_warn: Mapped[float] = mapped_column(Float, default=45.0)          # OLT temperature warning threshold (°C)
    temp_crit: Mapped[float] = mapped_column(Float, default=55.0)
    write_enabled: Mapped[bool] = mapped_column(Boolean, default=False)   # Allow changes to the OLT from the dashboard

    sync_requested: Mapped[bool] = mapped_column(Boolean, default=True)
    last_poll_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_full_sync_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_save_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)          # Last successful save
    last_save_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_save_ok: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    last_save_msg: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    onus: Mapped[list["ONU"]] = relationship(back_populates="olt", cascade="all, delete-orphan",
                                             passive_deletes=True)


class ONU(Base):
    __tablename__ = "onus"
    __table_args__ = (
        UniqueConstraint("olt_id", "shelf", "slot", "pon", "onu_number", name="uq_onu_pos"),
        Index("ix_onu_olt_status", "olt_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    olt_id: Mapped[int] = mapped_column(ForeignKey("olts.id", ondelete="CASCADE"), index=True)
    shelf: Mapped[int] = mapped_column(Integer, default=1)
    slot: Mapped[int] = mapped_column(Integer)
    pon: Mapped[int] = mapped_column(Integer)
    onu_number: Mapped[int] = mapped_column(Integer)

    sn: Mapped[str] = mapped_column(String(32), default="", index=True)
    onu_type: Mapped[str] = mapped_column(String(50), default="")
    name: Mapped[str] = mapped_column(String(100), default="")
    description: Mapped[str] = mapped_column(String(200), default="")
    vlan: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    conn_type: Mapped[str] = mapped_column(String(10), default="")         # pppoe | static | dhcp
    pppoe_user: Mapped[str] = mapped_column(String(100), default="")       # Password is NOT stored

    status: Mapped[str] = mapped_column(String(16), default="unknown")     # online|offline|los|power_fail|unknown
    phase_state: Mapped[str] = mapped_column(String(24), default="")       # Raw value from the OLT
    rx_power: Mapped[Optional[float]] = mapped_column(Float, nullable=True)       # dBm at the ONU
    olt_rx_power: Mapped[Optional[float]] = mapped_column(Float, nullable=True)   # dBm at the OLT
    status_changed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Dashboard-owned data (not read from the OLT).
    zone_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    odb_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    address: Mapped[str] = mapped_column(String(300), default="")
    contact: Mapped[str] = mapped_column(String(100), default="")
    external_id: Mapped[str] = mapped_column(String(64), default="")
    auth_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    mode: Mapped[str] = mapped_column(String(10), default="")              # routing | bridge
    dl_profile_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ul_profile_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    distance_m: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cfg_name: Mapped[str] = mapped_column(String(100), default="")         # Name as written in the OLT configuration
    mgmt_ip: Mapped[str] = mapped_column(String(45), default="")
    mgmt_vlan: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    olt: Mapped[OLT] = relationship(back_populates="onus")


class OnuMacAddress(Base):
    __tablename__ = "onu_mac_addresses"
    __table_args__ = (UniqueConstraint("onu_id", "mac", name="uq_onu_mac"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    onu_id: Mapped[int] = mapped_column(ForeignKey("onus.id", ondelete="CASCADE"), index=True)
    mac: Mapped[str] = mapped_column(String(17), index=True)
    mac_compact: Mapped[str] = mapped_column(String(12), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Zone(Base):
    __tablename__ = "zones"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)


class Odb(Base):
    """ODB / splitter."""
    __tablename__ = "odbs"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    zone_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


class SpeedProfile(Base):
    __tablename__ = "speed_profiles"
    __table_args__ = (UniqueConstraint("name", "direction", name="uq_speed_name_dir"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60))
    direction: Mapped[str] = mapped_column(String(10))                     # download | upload
    kbps: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20), default="internet")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)


class OnuType(Base):
    __tablename__ = "onu_types"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60), unique=True)
    pon_type: Mapped[str] = mapped_column(String(10), default="GPON")
    channels: Mapped[str] = mapped_column(String(20), default="G")
    eth_ports: Mapped[int] = mapped_column(Integer, default=4)
    wifi_ports: Mapped[int] = mapped_column(Integer, default=0)
    voip_ports: Mapped[int] = mapped_column(Integer, default=0)
    catv: Mapped[int] = mapped_column(Integer, default=0)
    allow_custom: Mapped[bool] = mapped_column(Boolean, default=True)
    capability: Mapped[str] = mapped_column(String(30), default="Bridging/Routing")


class OltInfo(Base):
    """Cache OLT read results by type: cards | pon_cfg | uplinks | vlans (JSON)."""
    __tablename__ = "olt_info"
    __table_args__ = (UniqueConstraint("olt_id", "kind", name="uq_olt_info"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    olt_id: Mapped[int] = mapped_column(ForeignKey("olts.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    data: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class GraphSample(Base):
    __tablename__ = "graph_samples"
    __table_args__ = (Index("ix_graph_lookup", "olt_id", "metric", "key", "ts"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    olt_id: Mapped[int] = mapped_column(ForeignKey("olts.id", ondelete="CASCADE"))
    metric: Mapped[str] = mapped_column(String(20))      # if_in | if_out | pon_rx | onu_status | up_rx | up_tx | up_temp
    key: Mapped[str] = mapped_column(String(80))
    ts: Mapped[datetime] = mapped_column(DateTime)
    value: Mapped[float] = mapped_column(Float)


class AuditLog(Base):
    """Record of each change sent to an OLT (passwords are masked)."""
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    user: Mapped[str] = mapped_column(String(60), default="")
    olt_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    olt_name: Mapped[str] = mapped_column(String(100), default="")
    action: Mapped[str] = mapped_column(String(40))
    target: Mapped[str] = mapped_column(String(120), default="")
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    commands: Mapped[str] = mapped_column(Text, default="")
    output: Mapped[str] = mapped_column(Text, default="")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(20), default="read_only", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
