# utils/vpn_manager.py
import subprocess
from utils.logger import logger


class VPNManager:
    @staticmethod
    def is_vpn_active() -> bool:
        """Проверяет активность VPN на macOS через netstat"""
        try:
            # Ищем активные туннельные интерфейсы (utun, ppp, ipsec)
            result = subprocess.run(
                ['netstat', '-rn'],
                capture_output=True, text=True, timeout=5
            )
            
            vpn_keywords = ['utun', 'ppp', 'ipsec', 'vpn']
            for line in result.stdout.split('\n'):
                if any(kw in line.lower() for kw in vpn_keywords):
                    # Проверяем, что интерфейс действительно активен (есть маршрут)
                    parts = line.split()
                    if len(parts) >= 3 and parts[0] not in ['Destination', '']:
                        return True
            
            return False
            
        except Exception as e:
            logger.warning(f"VPNManager: Ошибка проверки VPN: {e}")
            return False