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

# The firewall cmdlets alone took over 20 s on the booth laptop on 9 October 2026.
POWERSHELL_TIMEOUT_S = 120


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _powershell(script: str, action: str = "changing the temporary firewall rule"):
    if os.name != "nt":
        raise RuntimeError("Automatic MQTT firewall management requires Windows")
    advice = (
        "Run the launcher in Administrator PowerShell; do not disable the firewall or "
        "widen the rule."
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=POWERSHELL_TIMEOUT_S,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            f"Scoped firewall operation failed while {action}: PowerShell did not finish "
            f"within {POWERSHELL_TIMEOUT_S} s, so the rule's state is unknown. {advice}"
        ) from error
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(
            f"Scoped firewall operation failed while {action}: PowerShell could not be "
            f"run ({type(error).__name__}: {error}). {advice}"
        ) from error
    if result.returncode:
        # The operator needs PowerShell's own words: the cause is rarely elevation alone.
        said = "\n".join(
            part.strip() for part in (result.stderr, result.stdout) if part and part.strip()
        )
        raise RuntimeError(
            f"Scoped firewall operation failed while {action} "
            f"(PowerShell exit code {result.returncode}). {advice}\n"
            "PowerShell reported:\n" + (said or "(nothing on either output stream)")
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
    def __init__(
        self,
        local_ip: str,
        phone_ip: str,
        python: Path,
        *,
        port=1883,
        name_prefix="PrismPolarMqtt-",
        display_name="Prism Polar MQTT (temporary)",
    ):
        # The headset test link reuses this exact scope for its one WebSocket peer:
        # phone_ip is then the Quest's address and port the WebSocket port.
        self.local_ip = private_ipv4(local_ip)
        self.phone_ip = private_ipv4(phone_ip)
        if self.local_ip == self.phone_ip:
            raise ValueError("Laptop and remote device must have different fixed addresses")
        if not 1024 <= port <= 65535:
            raise ValueError("Firewall rule port must be between 1024 and 65535")
        self.port = port
        self.python = python.resolve()
        self.display_name = display_name
        self.name = name_prefix + uuid.uuid4().hex
        self.owner = "prism-live launcher temporary rule " + uuid.uuid4().hex
        self.attempted = False
        self.scope = None

    def open(self):
        if self.attempted:
            raise RuntimeError("Firewall rule creation already attempted")
        program = _python_image(self.python)
        self.attempted = True
        # One line, no backtick continuations: on the booth laptop (9 October 2026) a
        # continued New-NetFirewallRule passed through -Command never returned.
        create = (
            f"New-NetFirewallRule -Name {_literal(self.name)} "
            f"-DisplayName {_literal(self.display_name)} "
            f"-Description {_literal(self.owner)} -Direction Inbound -Action Allow "
            f"-Enabled True -Protocol TCP -LocalAddress {_literal(self.local_ip)} "
            f"-LocalPort {self.port} -RemoteAddress {_literal(self.phone_ip)} "
            f"-InterfaceAlias $alias -Profile $profile -Program {_literal(str(program))}"
        )
        # Resolve the interface and its CURRENT profile from the explicit local IP.
        # Public here may be a trusted private router labelled Public by Windows;
        # it never implies authorisation to run this on public/venue Wi-Fi.
        script = f"""
$ErrorActionPreference = 'Stop'
if (Get-NetFirewallRule -Name {_literal(self.name)} -ErrorAction SilentlyContinue) {{
    throw 'Rule already exists; refusing to adopt or replace it'
}}
$adapter = @(Get-NetIPAddress -AddressFamily IPv4 -IPAddress {_literal(self.local_ip)})
if ($adapter.Count -ne 1) {{
    throw "Local IP must identify exactly one interface; found $($adapter.Count)"
}}
$alias = $adapter[0].InterfaceAlias
# -InterfaceAlias is a wildcard pattern and hidden adapters are named like
# 'Local Area Connection* 2'. Refuse a name that could select a second interface.
$named = @(Get-NetIPInterface -AddressFamily IPv4 |
    Where-Object {{ $_.InterfaceAlias -like $alias }})
if ($named.Count -ne 1) {{
    throw "Interface name '$alias' selects $($named.Count) interfaces; refusing a wider rule"
}}
$index = $adapter[0].InterfaceIndex
$network = @(Get-NetConnectionProfile -InterfaceIndex $index -ErrorAction SilentlyContinue)
if ($network.Count -gt 1) {{ throw "Interface '$alias' has more than one network profile" }}
if ($network.Count -eq 1) {{
    $category = $network[0].NetworkCategory.ToString()
    $profile = if ($category -eq 'DomainAuthenticated') {{ 'Domain' }} else {{ $category }}
    if ($profile -notin @('Private', 'Public', 'Domain')) {{
        throw "Unsupported firewall profile '$category' on interface '$alias'"
    }}
}} else {{
    # Windows gives the Mobile Hotspot adapter no connection profile, so there is no
    # category to read. The rule stays bound to this one interface, both addresses,
    # the port and the program; only the profile condition is left unset.
    $profile = 'Any'
}}
{create} | Out-Null
@{{name={_literal(self.name)}; interface=$alias;
    profile=$profile; program={_literal(str(program))}}} |
    ConvertTo-Json -Compress
exit 0
"""
        self.scope = json.loads(_powershell(script, f"creating rule {self.name}"))
        return self.scope

    def close(self):
        if not self.attempted:
            return
        # Check the unguessable ownership description before removal. Even a name
        # collision or a replaced rule can never cause deletion of somebody else's.
        _powershell(
            f"""
$ErrorActionPreference = 'Stop'
$rule = Get-NetFirewallRule -Name {_literal(self.name)} -ErrorAction SilentlyContinue
if ($rule) {{
    if ($rule.Description -ne {_literal(self.owner)}) {{
        throw 'Firewall rule ownership changed; refusing removal'
    }}
    $rule | Remove-NetFirewallRule
}}
# A lookup that finds no rule leaves PowerShell's status false, which alone would end
# this script with exit code 1 and no message: nothing to remove is not a failure.
exit 0
""",
            f"removing rule {self.name}",
        )
        self.attempted = False
