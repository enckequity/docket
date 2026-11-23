"""Local outbox: what would be emailed to the customer, written to disk. SMTP is a later opt-in."""

from __future__ import annotations

from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path


def envelope(business: dict, customer: dict, sender: str | None = None) -> dict[str, str]:
    """From / Reply-To / To for an outbox message. From is the 'sender_email' setting, else the business email,
    under the business name; with no address at all there is no From (the owner's mail app fills it in). No
    placeholder addresses: a header is left out rather than invented."""
    if sender is None:
        from .settings import setting
        sender = setting("sender_email")
    name, reply = str(business.get("name") or ""), str(business.get("email") or "").strip()
    address = (sender or "").strip() or reply
    out: dict[str, str] = {}
    if address:
        out["From"] = formataddr((name, address)) if name else address
    if reply and reply != address:
        out["Reply-To"] = formataddr((name, reply)) if name else reply
    if customer.get("email"):
        out["To"] = formataddr((str(customer.get("name") or ""), str(customer["email"])))
    return out


def write_outbox(root: Path, *, number: str, version: int, business: dict, customer: dict, subject: str,
                 body: str, attachments: dict[str, bytes] | None = None) -> Path:
    folder = Path(root) / f"{number}-v{version}"
    folder.mkdir(parents=True, exist_ok=True)
    msg = EmailMessage()
    for header, value in envelope(business, customer).items():
        msg[header] = value
    msg["Subject"] = subject
    msg.set_content(body)
    for name, data in (attachments or {}).items():
        maintype, subtype = ("application", "pdf") if name.endswith(".pdf") else ("application", "octet-stream")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
        (folder / name).write_bytes(data)
    (folder / "message.eml").write_bytes(bytes(msg))
    (folder / "message.txt").write_text(body, encoding="utf-8")
    return folder
