import argparse
import configparser
import logging
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = SCRIPT_DIR / "rustic_report.conf"

log = logging.getLogger("send_report_mail")


def resolve_path(value: str, config_file: Path) -> Path:
    """Resolve an absolute path, or a relative path next to the config file."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else config_file.parent / path


def setup_logging(log_file: Path | None) -> None:
    """Log to file if configured, otherwise to stderr (never to stdout)."""
    handler: logging.Handler
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_file, encoding="utf-8")
    else:
        handler = logging.StreamHandler(sys.stderr)

    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def get_smtp_password(mail: configparser.SectionProxy, config_file: Path) -> str:
    """Return the SMTP password from smtp_password_file or smtp_password.

    If smtp_password_file is set, it takes precedence over smtp_password.
    """
    password_file_value = mail.get("smtp_password_file", "").strip()

    if not password_file_value:
        return mail["smtp_password"]

    if mail.get("smtp_password", "").strip():
        log.warning(
            "Both smtp_password and smtp_password_file are set; "
            "using smtp_password_file"
        )

    password_file = resolve_path(password_file_value, config_file)

    if os.name == "posix" and password_file.stat().st_mode & 0o077:
        log.warning(
            "Password file %s is accessible by group/others; "
            "consider chmod 600",
            password_file,
        )

    # Only strip the line ending, so passwords with spaces stay intact
    password = password_file.read_text(encoding="utf-8").rstrip("\r\n")
    if not password:
        raise ValueError(f"Password file is empty: {password_file}")
    return password


def build_message(
    mail: configparser.SectionProxy, report_file: Path, status: str
) -> EmailMessage:
    recipients = [r.strip() for r in mail["recipients"].split(",") if r.strip()]
    if not recipients:
        raise ValueError("No recipients configured")

    message = EmailMessage()
    message["Subject"] = mail.get(f"subject-{status}", "Rustic Backup Report")
    message["From"] = mail["sender"]
    message["To"] = ", ".join(recipients)
    message.set_content("Please view this report in an HTML-capable mail client.")
    message.add_alternative(
        report_file.read_text(encoding="utf-8"), subtype="html"
    )
    return message


def send_message(
    mail: configparser.SectionProxy, message: EmailMessage, config_file: Path
) -> None:
    server_name = mail["smtp_server"]
    port = mail.getint("smtp_port", fallback=587)
    security = mail.get("smtp_security", "starttls").strip().lower()

    if security not in ("starttls", "ssl"):
        raise ValueError(
            f"Invalid smtp_security: {security!r} (use starttls or ssl)"
        )

    context = ssl.create_default_context()

    if security == "ssl":
        smtp: smtplib.SMTP = smtplib.SMTP_SSL(
            server_name, port, timeout=30, context=context
        )
    else:
        smtp = smtplib.SMTP(server_name, port, timeout=30)

    with smtp:
        if security == "starttls":
            smtp.starttls(context=context)

        if mail.getboolean("smtp_auth", fallback=False):
            smtp.login(mail["smtp_user"], get_smtp_password(mail, config_file))

        smtp.send_message(message)


def main() -> int:
    parser = argparse.ArgumentParser(description="Send the Rustic HTML report by mail.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument(
        "--status",
        choices=("success", "failure"),
        default="success",
        help="Report type, selects the mail subject. Default: success",
    )
    args = parser.parse_args()

    config_file = Path(args.config).expanduser().resolve()

    config = configparser.ConfigParser(interpolation=None)
    config.read(config_file, encoding="utf-8")

    if "mail" not in config or "report" not in config:
        print("Missing [mail] or [report] section in config", file=sys.stderr)
        return 1

    mail = config["mail"]
    log_value = mail.get("log_file", "").strip()
    setup_logging(resolve_path(log_value, config_file) if log_value else None)

    try:
        report_file = resolve_path(config["report"]["output"], config_file)
        message = build_message(mail, report_file, args.status)
        send_message(mail, message, config_file)
    except (KeyError, ValueError, OSError, smtplib.SMTPException) as error:
        log.error("Sending mail failed: %s: %s", type(error).__name__, error)
        return 1

    log.info("Report mail (%s) sent", args.status)
    return 0


if __name__ == "__main__":
    sys.exit(main())
