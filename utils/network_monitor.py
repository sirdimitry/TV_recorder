"""Проверка фактического доступа в интернет без блокировки интерфейса."""

import requests


class NetworkMonitor:
    CHECK_URLS = (
        'https://www.apple.com/library/test/success.html',
        'https://www.cloudflare.com/cdn-cgi/trace',
    )

    @classmethod
    def is_internet_available(cls) -> bool:
        for url in cls.CHECK_URLS:
            try:
                response = requests.get(url, timeout=3)
                if response.status_code < 500:
                    return True
            except requests.RequestException:
                continue
        return False
