"""Firewall ownership and strict scope, without executing Windows changes."""

import json
from pathlib import Path

import pytest

from tools import mqtt_firewall


def test_firewall_creation_and_removal_are_exact_and_owned(monkeypatch):
    scripts = []
    rule = mqtt_firewall.MqttFirewall("192.168.1.201", "192.168.1.135", Path("python.exe"))

    def execute(script):
        scripts.append(script)
        return json.dumps({"name": rule.name, "interface": "Wi-Fi", "profile": "Public"})

    monkeypatch.setattr(mqtt_firewall, "_powershell", execute)
    monkeypatch.setattr(mqtt_firewall, "_python_image", lambda _: Path("C:/Python/python.exe"))
    rule.close()
    assert not scripts
    rule.open()
    rule.close()
    create, remove = scripts
    for term in (
        "-LocalAddress '192.168.1.201'",
        "-RemoteAddress '192.168.1.135'",
        "-LocalPort 1883",
        "-Protocol TCP",
        "-InterfaceAlias $adapter[0].InterfaceAlias",
        "-Profile $profile",
        "-Program",
        "Get-NetConnectionProfile",
        rule.name,
        rule.owner,
    ):
        assert term in create
    assert "C:\\Python\\python.exe" in create
    assert "refusing to adopt or replace" in create
    assert "Set-NetConnectionProfile" not in create
    assert rule.name in remove and rule.owner in remove
    assert "Description -ne" in remove
    assert "refusing removal" in remove
    assert "*" not in remove
    assert not rule.attempted


def test_failed_firewall_setup_still_permits_owned_cleanup(monkeypatch):
    rule = mqtt_firewall.MqttFirewall("192.168.1.201", "192.168.1.135", Path("python.exe"))
    scripts = []

    def execute(script):
        scripts.append(script)
        if len(scripts) == 1:
            raise RuntimeError("creation result lost")
        return ""

    monkeypatch.setattr(mqtt_firewall, "_powershell", execute)
    monkeypatch.setattr(mqtt_firewall, "_python_image", lambda value: value)
    with pytest.raises(RuntimeError):
        rule.open()
    rule.close()
    assert len(scripts) == 2


@pytest.mark.parametrize("address", ["0.0.0.0", "127.0.0.1", "8.8.8.8", "192.168.1.0/24", "::1"])
def test_firewall_rejects_wide_or_non_private_scope(address):
    with pytest.raises(ValueError):
        mqtt_firewall.MqttFirewall("192.168.1.201", address, Path("python.exe"))
