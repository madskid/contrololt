import unittest
from unittest.mock import AsyncMock, patch

from app.drivers import cdata, hioso
from app.drivers.snmp import SnmpError, read_bridge_mac_table


class SnmpOnuMacTests(unittest.IsolatedAsyncioTestCase):
    async def test_qbridge_rows_map_mac_to_logical_onu_interface(self):
        client = type("Client", (), {})()
        client.walk = AsyncMock(side_effect=[
            {"42.3.4.5.6.7.8": "INTEGER: 7"},
            {"7": "INTEGER: 101"},
            {"101": "STRING: EPON0/1:23"},
        ])

        self.assertEqual(
            await read_bridge_mac_table(client),
            [("EPON0/1:23", "03:04:05:06:07:08")],
        )

    async def test_legacy_bridge_table_is_used_when_qbridge_is_empty(self):
        client = type("Client", (), {})()
        client.walk = AsyncMock(side_effect=[
            {},
            {"3.4.5.6.7.8": "INTEGER: 7"},
            {"7": "INTEGER: 101"},
            {"101": "STRING: epon 0/1 onu 23"},
        ])

        self.assertEqual(
            await read_bridge_mac_table(client),
            [("epon 0/1 onu 23", "03:04:05:06:07:08")],
        )

    async def test_cdata_maps_only_known_pon_onu_positions(self):
        rows = [("EPON0/1:23", "03:04:05:06:07:08")]
        cdata_client = type("Client", (), {"walk": AsyncMock(return_value=[])})()

        with patch.object(cdata, "read_bridge_mac_table", new=AsyncMock(return_value=rows)):
            self.assertEqual(
                await cdata.read_onu_macs(cdata_client, {(1, 1, 23): {}}),
                {(1, 1, 23): ["03:04:05:06:07:08"]},
            )

    async def test_hioso_dynamic_mac_table_maps_only_epon_onu_ports(self):
        output = """MAC                   VLAN    Port           MAC-Type
00:11:22:33:44:55     805     epon0/2/1      dynamic
00:11:22:33:44:66     805     epon 0/1/1     dynamic
00:11:22:33:44:77     805     xe0/0/2        dynamic
"""
        olt = object()
        onus = {(1, 1): {}, (2, 1): {}}

        with patch.object(hioso, "run_cli", new=AsyncMock(return_value=[output])) as run_cli:
            self.assertEqual(
                await hioso.read_onu_macs(olt, onus),
                {
                    (1, 1): ["00:11:22:33:44:66"],
                    (2, 1): ["00:11:22:33:44:55"],
                },
            )
        run_cli.assert_awaited_once_with(olt, ["show mac dynamic"])

    async def test_fdb_scan_errors_when_both_standard_tables_fail(self):
        client = type("Client", (), {})()
        client.walk = AsyncMock(side_effect=SnmpError("unsupported"))

        with self.assertRaises(SnmpError):
            await read_bridge_mac_table(client)

    async def test_fdb_scan_does_not_guess_missing_bridge_port_mapping(self):
        client = type("Client", (), {})()
        client.walk = AsyncMock(side_effect=[
            {"42.3.4.5.6.7.8": "INTEGER: 7"},
            {},
        ])

        with self.assertRaisesRegex(SnmpError, "bridge-port to interface-index"):
            await read_bridge_mac_table(client)

    def test_compact_and_spaced_epon_port_names_map_to_onus(self):
        from app.drivers.snmp import onu_position_from_ifname

        self.assertEqual(onu_position_from_ifname("EPON0/1/23"), (1, 23))
        self.assertEqual(onu_position_from_ifname("epon 0/1/23"), (1, 23))


if __name__ == "__main__":
    unittest.main()
