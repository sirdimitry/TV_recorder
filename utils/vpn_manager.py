# utils/vpn_manager.py
import platform
import subprocess
from utils.logger import logger


class VPNManager:
    @staticmethod
    def is_vpn_active() -> bool:
        """Проверяет активное VPN-подключение в macOS, Windows и Linux."""
        try:
            system = platform.system()
            if system == 'Darwin':
                services = subprocess.run(
                    ['scutil', '--nc', 'list'], capture_output=True, text=True, timeout=5
                )
                if '(Connected)' in services.stdout:
                    return True
                routes = subprocess.run(
                    ['netstat', '-rn'], capture_output=True, text=True, timeout=5
                )
                return any(
                    any(keyword in line.lower() for keyword in ('utun', 'ppp', 'ipsec'))
                    for line in routes.stdout.splitlines()
                )

            if system == 'Windows':
                command = (
                    "Get-NetAdapter | Where-Object {$_.Status -eq 'Up' -and "
                    "$_.InterfaceDescription -match 'VPN|TAP|TUN|WireGuard'} | "
                    "Select-Object -First 1 -ExpandProperty Name"
                )
                result = subprocess.run(
                    ['powershell', '-NoProfile', '-Command', command],
                    capture_output=True, text=True, timeout=5
                )
                return bool(result.stdout.strip())

            result = subprocess.run(['ip', 'link'], capture_output=True, text=True, timeout=5)
            return any(keyword in result.stdout.lower() for keyword in ('tun', 'tap', 'ppp'))
        except Exception as e:
            logger.warning(f"VPNManager: Ошибка проверки VPN: {e}")
            return False
