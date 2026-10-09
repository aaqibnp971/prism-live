"""Firewall ownership and strict scope, without executing Windows changes."""

import json
from pathlib import Path

import pytest

from tools import mqtt_firewall


def test_firewall_creation_and_removal_are_exact_and_owned(monkeypatch):
    scripts = []
    rule = mqtt_firewall.MqttFirewall("192.168.1.201", "192.168.1.135", Path("python.exe"))

    def execute(script, *_):
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
        "-InterfaceAlias $alias",
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


def test_headset_link_rule_keeps_the_same_exact_scope(monkeypatch):
    scripts = []
    rule = mqtt_firewall.MqttFirewall(
        "192.168.0.125",
        "192.168.0.150",
        Path("python.exe"),
        port=8787,
        name_prefix="PrismQuestLink-",
        display_name="Prism Quest link (temporary)",
    )
    monkeypatch.setattr(mqtt_firewall, "_powershell", lambda s, *_: scripts.append(s) or "{}")
    monkeypatch.setattr(mqtt_firewall, "_python_image", lambda _: Path("C:/Python/python.exe"))
    rule.open()
    assert rule.name.startswith("PrismQuestLink-")
    for term in (
        "-LocalAddress '192.168.0.125'",
        "-RemoteAddress '192.168.0.150'",
        "-LocalPort 8787",
        "-Protocol TCP",
        "-DisplayName 'Prism Quest link (temporary)'",
        "-Program",
    ):
        assert term in scripts[0]


def test_failed_firewall_setup_still_permits_owned_cleanup(monkeypatch):
    rule = mqtt_firewall.MqttFirewall("192.168.1.201", "192.168.1.135", Path("python.exe"))
    scripts = []

    def execute(script, *_):
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


def test_adapter_without_a_network_profile_keeps_every_other_scope(monkeypatch):
    # The Mobile Hotspot adapter has no connection profile; only -Profile may relax.
    scripts = []
    rule = mqtt_firewall.MqttFirewall(
        "192.168.137.1", "192.168.137.171", Path("python.exe"), port=8787
    )
    monkeypatch.setattr(mqtt_firewall, "_powershell", lambda s, *_: scripts.append(s) or "{}")
    monkeypatch.setattr(mqtt_firewall, "_python_image", lambda _: Path("C:/Python/python.exe"))
    rule.open()
    script = scripts[0]
    lookup = script.index("Get-NetConnectionProfile")
    assert "-ErrorAction SilentlyContinue" in script[lookup : script.index("\n", lookup + 80)]
    assert "$profile = 'Any'" in script
    assert "refusing a wider rule" in script
    create = script[script.index("New-NetFirewallRule") :]
    for term in (
        "-LocalAddress '192.168.137.1'",
        "-RemoteAddress '192.168.137.171'",
        "-LocalPort 8787",
        "-InterfaceAlias $alias",
        "-Profile $profile",
        "-Program 'C:\\Python\\python.exe'",
    ):
        assert term in create
    assert "Any" not in create
    # A continued command line stalled PowerShell on the booth laptop.
    assert not any(line.rstrip().endswith("`") for line in script.splitlines())


@pytest.mark.skipif(mqtt_firewall.os.name != "nt", reason="Runs Windows PowerShell")
def test_removing_a_rule_that_was_never_created_is_not_a_failure():
    # Reads the firewall only: this name does not exist, so nothing can be removed.
    rule = mqtt_firewall.MqttFirewall("192.168.1.201", "192.168.1.135", Path("python.exe"))
    rule.attempted = True
    rule.close()
    assert not rule.attempted


@pytest.mark.skipif(mqtt_firewall.os.name != "nt", reason="Runs Windows PowerShell")
def test_failure_reports_what_powershell_said():
    with pytest.raises(RuntimeError) as failure:
        mqtt_firewall._powershell(
            "$ErrorActionPreference = 'Stop'; Write-Output 'half done'; throw 'the real cause'",
            "creating rule X",
        )
    message = str(failure.value)
    assert "the real cause" in message and "half done" in message
    assert "creating rule X" in message and "exit code 1" in message


def test_silent_or_unstartable_powershell_is_still_explained(monkeypatch):
    class Silent:
        returncode, stdout, stderr = 5, "", ""

    monkeypatch.setattr(mqtt_firewall.os, "name", "nt")
    monkeypatch.setattr(mqtt_firewall.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    monkeypatch.setattr(mqtt_firewall.subprocess, "run", lambda *a, **k: Silent())
    with pytest.raises(RuntimeError, match="exit code 5") as failure:
        mqtt_firewall._powershell("x")
    assert "nothing on either output stream" in str(failure.value)

    def refuse(*a, **k):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(mqtt_firewall.subprocess, "run", refuse)
    with pytest.raises(RuntimeError, match="Access is denied"):
        mqtt_firewall._powershell("x")
