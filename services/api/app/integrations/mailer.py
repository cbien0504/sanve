import asyncio
import io
from concurrent.futures import ProcessPoolExecutor
from email.message import EmailMessage
from email.utils import make_msgid

import aiosmtplib
import segno

from app.config import Settings


def _qr_png(data: str) -> bytes:
    buffer = io.BytesIO()
    segno.make(data, error="m").save(buffer, kind="png", scale=6, border=2)
    return buffer.getvalue()


class Mailer:
    def __init__(self, settings: Settings, pool: ProcessPoolExecutor) -> None:
        self._settings = settings
        # QR rendering is pure-Python CPU work: in threads it would hold the GIL and stall the event
        # loop, so it runs in worker processes instead.
        self._pool = pool

    async def qr_codes(self, codes: list[str]) -> list[bytes]:
        loop = asyncio.get_running_loop()
        return list(await asyncio.gather(*(loop.run_in_executor(self._pool, _qr_png, c) for c in codes)))

    async def send(self, message: EmailMessage) -> None:
        message["From"] = self._settings.mail_from
        await aiosmtplib.send(
            message, hostname=self._settings.smtp_host, port=self._settings.smtp_port, timeout=10
        )

    @staticmethod
    def new_message(to: str, subject: str) -> EmailMessage:
        message = EmailMessage()
        message["To"] = to
        message["Subject"] = subject
        message["Message-ID"] = make_msgid(domain="sanve.local")
        return message
