"""Owned, temporary Windows firewall exception for the booth phone only.

No changes occur on import. The launcher must run elevated for MQTT booth mode;
failure is fatal rather than falling back to a broad exception. The exact rule
name is printed so forced supervisor/OS termination can be cleaned up manually.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path

from bridge.mqtt_broker import private_ipv4


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _powershell(script: str):
    if os.name != "nt":
        raise RuntimeError("Automatic MQTT firewall management requires Windows")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError(
            "Scoped MQTT firewall operation failed. Run the launcher in Administrator "
            f"PowerShell; do not disable the firewall. {result.stderr.strip()}"
        )
    return result.stdout.strip()


def _python_image(python: Path) -> Path:
    """A Windows venv python.exe redirects to a different process image.

    Firewall -Program matches that image, not sys.executable. Resolve it with the
    configured interpreter without starting a listener or modifying the machine.
    """
    try:
        result = subprocess.run(
            [
                str(python),
                "-c",
                "import ctypes; b=ctypes.create_unicode_buffer(32768); "
                "n=ctypes.windll.kernel32.GetModuleFileNameW(None,b,len(b)); "
                "assert 0<n<len(b); print(b.value)",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(
            "Could not resolve the configured Python's Windows process image"
        ) from error
    image = Path(result.stdout.strip())
    if not image.is_absolute() or not image.is_file():
        raise RuntimeError("Could not resolve the configured Python's Windows process image")
    return image


class MqttFirewall:
    def __init__(self, local_ip: str, phone_ip: str, python: Path, *, port=1883):
        self.local_ip = private_ipv4(local_ip)
        self.phone_ip = private_ipv4(phone_ip)
        if self.local_ip == self.phone_ip:
            raise ValueError("Laptop and phone must have different fixed addresses")
        if not 1024 <= port <= 65535:
            raise ValueError("MQTT port must be between 1024 and 65535")
        self.port = port
        self.python = python.resolve()
        self.name = "PrismPolarMqtt-" + uuid.uuid4().hex
        self.owner = "prism-live launcher temporary rule " + uuid.uuid4().hex
        self.attempted = False
        self.scope = None

    def open(self):
        if self.attempted:
            raise RuntimeError("Firewall rule creation already attempted")
        program = _python_image(self.python)
        self.attempted = True
        # Resolve the interface and its CURRENT profile from the explicit local IP.
        # Public here may be a trusted private router labelled Public by Windows;
        # it never implies authorisation to run this on public/venue Wi-Fi.
        script = f"""
$ErrorActionPreference = 'Stop'
if (Get-NetFirewallRule -Name {_literal(self.name)} -ErrorAction SilentlyContinue) {{
    throw 'Rule already exists; refusing to adopt or replace it'
}}
$adapter = @(Get-NetIPAddress -AddressFamily IPv4 -IPAddress {_literal(self.local_ip)})
if ($adapter.Count -ne 1) {{ throw 'Local MQTT IP must identify exactly one interface' }}
$network = @(Get-NetConnectionProfile -InterfaceIndex $adapter[0].InterfaceIndex)
if ($network.Count -ne 1) {{ throw 'MQTT interface must have exactly one network profile' }}
$category = $network[0].NetworkCategory.ToString()
$profile = if ($category -eq 'DomainAuthenticated') {{ 'Domain' }} else {{ $category }}
if ($profile -notin @('Private', 'Public', 'Domain')) {{ throw 'Unsupported firewall profile' }}
New-NetFirewallRule -Name {_literal(self.name)} -DisplayName 'Prism Polar MQTT (temporary)' `
    -Description {_literal(self.owner)} -Direction Inbound -Action Allow -Enabled True `
    -Protocol TCP -LocalAddress {_literal(self.local_ip)} -LocalPort {self.port} `
    -RemoteAddress {_literal(self.phone_ip)} -InterfaceAlias $adapter[0].InterfaceAlias `
    -Profile $profile -Program {_literal(str(program))} | Out-Null
@{{name={_literal(self.name)}; interface=$adapter[0].InterfaceAlias;
    profile=$profile; program={_literal(str(program))}}} |
    ConvertTo-Json -Compress
"""
        self.scope = json.loads(_powershell(script))
        return self.scope

    def close(self):
        if not self.attempted:
            return
        # Check the unguessable ownership description before removal. Even a name
        # collision or a replaced rule can never cause deletion of somebody else's.
        _powershell(f"""
$ErrorActionPreference = 'Stop'
$rule = Get-NetFirewallRule -Name {_literal(self.name)} -ErrorAction SilentlyContinue
if ($rule) {{
    if ($rule.Description -ne {_literal(self.owner)}) {{
        throw 'Firewall rule ownership changed; refusing removal'
    }}
    $rule | Remove-NetFirewallRule
}}
""")
        self.attempted = False
