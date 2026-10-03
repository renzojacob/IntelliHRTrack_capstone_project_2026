import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend


class BrevoAPIEmailBackend(BaseEmailBackend):
    api_url = "https://api.brevo.com/v3/smtp/email"

    def send_messages(self, email_messages):
        if not email_messages:
            return 0

        api_key = getattr(settings, "BREVO_API_KEY", "")

        if not api_key:
            if self.fail_silently:
                return 0

            raise RuntimeError(
                "BREVO_API_KEY is missing from the environment variables."
            )

        sent_count = 0

        for message in email_messages:
            if not message.to:
                continue

            payload = {
                "sender": {
                    "name": settings.BREVO_SENDER_NAME,
                    "email": settings.BREVO_SENDER_EMAIL,
                },
                "to": [
                    {"email": recipient}
                    for recipient in message.to
                ],
                "subject": message.subject,
                "textContent": message.body,
            }

            if message.cc:
                payload["cc"] = [
                    {"email": recipient}
                    for recipient in message.cc
                ]

            if message.bcc:
                payload["bcc"] = [
                    {"email": recipient}
                    for recipient in message.bcc
                ]

            for alternative in getattr(message, "alternatives", []):
                content = getattr(alternative, "content", None)
                mimetype = getattr(alternative, "mimetype", None)

                if content is None and isinstance(alternative, (tuple, list)):
                    content = alternative[0]
                    mimetype = alternative[1]

                if mimetype == "text/html":
                    payload["htmlContent"] = content
                    break

            request = Request(
                self.api_url,
                data=json.dumps(payload).encode("utf-8"),
                method="POST",
                headers={
                    "accept": "application/json",
                    "api-key": api_key,
                    "content-type": "application/json",
                },
            )

            try:
                with urlopen(request, timeout=20) as response:
                    if 200 <= response.status < 300:
                        sent_count += 1
                    elif not self.fail_silently:
                        raise RuntimeError(
                            f"Brevo returned HTTP status {response.status}."
                        )

            except HTTPError as error:
                details = error.read().decode("utf-8", errors="replace")

                if not self.fail_silently:
                    raise RuntimeError(
                        f"Brevo rejected the email request: "
                        f"HTTP {error.code}. {details}"
                    ) from error

            except (URLError, OSError) as error:
                if not self.fail_silently:
                    raise RuntimeError(
                        f"Could not connect to Brevo: {error}"
                    ) from error

        return sent_count
        