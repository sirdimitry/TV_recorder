# core/notifier.py
import platform
import subprocess
from utils.logger import logger

try:
    from plyer import notification
    PLYER_AVAILABLE = True
except ImportError:
    PLYER_AVAILABLE = False


class Notifier:
    """Системные уведомления"""
    
    APP_NAME = "TV Recorder"
    
    def send(self, title: str, message: str, timeout: int = 10):
        """Отправляет системное уведомление"""
        logger.info(f"Уведомление: {title} — {message}")
        
        try:
            # В macOS встроенный osascript не требует дополнительных библиотек.
            # Plyer на этой платформе требует pyobjus и иначе печатает ошибку при
            # каждом уведомлении, хотя резервный способ уже надёжно работает.
            if platform.system() == "Darwin":
                self._fallback_notify(title, message)
            elif PLYER_AVAILABLE:
                notification.notify(
                    title=title,
                    message=message,
                    app_name=self.APP_NAME,
                    timeout=timeout
                )
            else:
                self._fallback_notify(title, message)
        except Exception as e:
            logger.error(f"Ошибка уведомления: {e}")
            self._fallback_notify(title, message)
    
    def _fallback_notify(self, title: str, message: str):
        """Резервный способ уведомлений через macOS osascript"""
        system = platform.system()
        
        if system == "Darwin":
            # Экранируем кавычки для AppleScript
            safe_title = title.replace('"', '\\"')
            safe_message = message.replace('"', '\\"')
            script = f'display notification "{safe_message}" with title "{safe_title}"'
            subprocess.run(['osascript', '-e', script], capture_output=True)
        elif system == "Linux":
            subprocess.run(['notify-send', title, message], capture_output=True)
        else:
            # Windows или другая система без plyer
            print(f"[NOTIFY] {title}: {message}")
