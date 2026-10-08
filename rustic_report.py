import argparse
import configparser
import html
import json
import logging
import re
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")

# ANSI SGR sequences such as ESC[0m, ESC[1m or ESC[31m. The ESC character is
# optional because some tools drop control characters when a log is saved.
ANSI_RE = re.compile(r"\x1b?\[([0-9;]+)m")

# Foreground colors, chosen to be readable on a dark code block.
ANSI_COLORS = {
    30: "#8a9299",
    31: "#ff6b6b",
    32: "#5fd38d",
    33: "#f0c35a",
    34: "#6fa8ff",
    35: "#d68bff",
    36: "#5fd3d3",
    37: "#f3f3f3",
    90: "#a0acb8",
    91: "#ff8f8f",
    92: "#7be3a5",
    93: "#ffd980",
    94: "#8fbaff",
    95: "#e3a8ff",
    96: "#80e3e3",
    97: "#ffffff",
}

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = SCRIPT_DIR / "rustic_report.conf"
DEFAULT_LOG_FILE = "./log/rustic_report.log"
MAX_ERROR_LINES = 30
DEFAULT_ERROR = "No error output found. Check the Rustic log for details."

log = logging.getLogger("rustic_report")


# -----------------------------------------------------------------------------
# Formatting helpers
# -----------------------------------------------------------------------------


def format_bytes_auto(value: Any) -> tuple[str, str]:
    """
    Format bytes as GB when >= 1 GB, otherwise as MB.

    Returns a tuple containing the formatted number and the unit.
    """
    if value is None or value == "":
        return "-", "GB"

    try:
        bytes_value = float(value)
    except (TypeError, ValueError):
        return "-", "GB"

    if bytes_value < 0:
        return "-", "GB"

    gb = 1000 ** 3

    if bytes_value < gb:
        return f"{bytes_value / (1000 ** 2):.2f}", "MB"

    return f"{bytes_value / gb:.2f}", "GB"


def format_bytes_with_unit(value: Any) -> str:
    """Format bytes dynamically as GB or MB, including the unit."""
    formatted, unit = format_bytes_auto(value)

    if formatted == "-":
        return "-"

    return f"{formatted} {unit}"


def format_number(value: Any) -> str:
    """Format an integer with thousands separator."""
    if value is None or value == "":
        return "-"

    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "-"


def format_duration(seconds: Any) -> tuple[str, str]:
    """
    Format duration in milliseconds below one second, otherwise HH:MM:SS.

    Returns a tuple containing the formatted value and the display label.
    """
    if seconds is None or seconds == "":
        return "-", "DURATION"

    try:
        total_seconds = float(seconds)
    except (TypeError, ValueError):
        return "-", "DURATION"

    if total_seconds < 0:
        return "-", "DURATION"

    if total_seconds < 1.0:
        milliseconds = round(total_seconds * 1000)
        return str(milliseconds), "DURATION (ms)"

    rounded_seconds = round(total_seconds)
    hours, remainder = divmod(rounded_seconds, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)

    return (
        f"{hours:02d}:{minutes:02d}:{remaining_seconds:02d}",
        "DURATION",
    )


def format_report_date() -> str:
    """Current local date/time for the report header."""
    return datetime.now().astimezone().strftime("%d %B %Y · %H:%M:%S")


def calculate_percent(value: int | None, total: int | None) -> str:
    """Calculate percentage of total files."""
    if value is None or total is None:
        return "-"

    if total == 0:
        return "0.0%"

    return f"{(value / total) * 100:.1f}%"


# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------


def setup_logging(log_file: Path) -> None:
    """
    Log to the configured log file.

    Errors are additionally written to stderr. Nothing is ever written to
    stdout, so the Rustic JSONL output stays clean.
    """
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.ERROR)
    console.setFormatter(formatter)
    log.addHandler(console)

    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
    except OSError as error:
        print(
            f"Warning: cannot write log file {log_file}: {error}",
            file=sys.stderr,
        )
    else:
        file_handler.setFormatter(formatter)
        log.addHandler(file_handler)

    log.setLevel(logging.INFO)


# -----------------------------------------------------------------------------
# Config handling
# -----------------------------------------------------------------------------


def load_config(config_file: Path) -> configparser.SectionProxy:
    """Load the report configuration from an INI-style config file."""
    parser = configparser.ConfigParser()

    if not config_file.is_file():
        raise FileNotFoundError(
            f"Config file not found: {config_file}"
        )

    parser.read(config_file, encoding="utf-8")

    if "report" not in parser:
        raise RuntimeError(
            f"Missing [report] section in config file: {config_file}"
        )

    return parser["report"]


def get_required_config(config: configparser.SectionProxy, key: str) -> str:
    """Read a required non-empty config value."""
    value = config.get(key, fallback="").strip()

    if not value:
        raise RuntimeError(
            f"Missing or empty required config value: [report] {key}"
        )

    return value


def resolve_config_path(value: str, config_file: Path) -> Path:
    """Resolve an absolute path, or a relative path next to the config file."""
    path = Path(value).expanduser()

    if path.is_absolute():
        return path

    return config_file.parent / path


def get_log_file(config: configparser.SectionProxy, config_file: Path) -> Path:
    """Get the log file path from config, or use the default log directory."""
    value = config.get("log_file", fallback="").strip() or DEFAULT_LOG_FILE

    return resolve_config_path(value, config_file)


def get_hostname(config: configparser.SectionProxy) -> str:
    """Get hostname from config, or detect it automatically."""
    value = config.get("hostname", fallback="auto").strip()

    if not value or value.lower() == "auto":
        return socket.gethostname()

    return value


def get_rustic_version(config: configparser.SectionProxy) -> str:
    """Get Rustic version from config, or detect it with `rustic --version`."""
    value = config.get("rustic_version", fallback="auto").strip()

    if value and value.lower() != "auto":
        return value

    try:
        result = subprocess.run(
            ["rustic", "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "-"

    if result.returncode != 0:
        return "-"

    version = result.stdout.strip()

    if not version:
        return "-"

    return version.splitlines()[0].strip() or "-"


# -----------------------------------------------------------------------------
# JSONL handling
# -----------------------------------------------------------------------------


def read_jsonl(json_file: Path) -> dict[str, Any]:
    """
    Read Rustic --json-progress JSONL output.

    The last 'summary' message is used for the report.
    """
    summary: dict[str, Any] | None = None

    with json_file.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, 1):
            line = line.strip()

            if not line:
                continue

            try:
                data = json.loads(line)
            except json.JSONDecodeError as error:
                log.warning("Invalid JSON on line %d: %s", line_number, error)
                continue

            if not isinstance(data, dict):
                continue

            if data.get("message_type") == "summary":
                summary = data

    if summary is None:
        raise RuntimeError("No Rustic summary found in JSONL file.")

    return summary


def load_report_data(json_file: Path, status: str) -> dict[str, Any]:
    """
    Read the JSONL file for the report.

    A failed backup usually leaves the JSONL empty, so a failure report
    continues with empty data instead of aborting.
    """
    try:
        return read_jsonl(json_file)
    except (OSError, RuntimeError) as error:
        if status == "success":
            raise

        log.info("No usable JSONL data for the failure report: %s", error)

        return {}


# -----------------------------------------------------------------------------
# Rustic error log handling
# -----------------------------------------------------------------------------


def strip_ansi(text: str) -> str:
    """Remove ANSI color sequences from text."""
    return ANSI_RE.sub("", text)


def wrap_ansi_chunk(chunk: str, color: str | None, bold: bool) -> str:
    """HTML-escape a text chunk and wrap it in a styled span if needed."""
    escaped = html.escape(chunk, quote=True)
    styles = []

    if color:
        styles.append(f"color:{color}")

    if bold:
        styles.append("font-weight:bold")

    if not styles or not escaped:
        return escaped

    return f'<span style="{";".join(styles)}">{escaped}</span>'


def ansi_to_html(text: str) -> str:
    """
    Convert ANSI color and bold sequences into HTML <span> elements.

    The text is HTML-escaped. Unsupported sequences are ignored.
    """
    parts: list[str] = []
    color: str | None = None
    bold = False
    position = 0

    for match in ANSI_RE.finditer(text):
        parts.append(wrap_ansi_chunk(text[position:match.start()], color, bold))
        position = match.end()

        for code_text in match.group(1).split(";"):
            code = int(code_text) if code_text else 0

            if code == 0:
                color = None
                bold = False
            elif code == 1:
                bold = True
            elif code == 22:
                bold = False
            elif code == 39:
                color = None
            elif code in ANSI_COLORS:
                color = ANSI_COLORS[code]

    parts.append(wrap_ansi_chunk(text[position:], color, bold))

    return "".join(parts)


def read_error_text(error_file: Path) -> str:
    """
    Read the Rustic error log for the failure report.

    [INFO] lines are removed, runs of blank lines are collapsed and only the
    last MAX_ERROR_LINES lines are kept. ANSI sequences are kept in the
    returned text so they can be rendered later.
    """
    try:
        text = error_file.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        log.warning("Cannot read error log %s: %s", error_file, error)
        return DEFAULT_ERROR

    lines = [
        line
        for line in text.splitlines()
        if not strip_ansi(line).lstrip().startswith("[INFO]")
    ]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()

    if not strip_ansi(text).strip():
        log.warning("No error output found in %s", error_file)
        return DEFAULT_ERROR

    return "\n".join(text.splitlines()[-MAX_ERROR_LINES:])


# -----------------------------------------------------------------------------
# Safe value helpers
# -----------------------------------------------------------------------------


def optional_int(data: dict[str, Any], key: str) -> int | None:
    """Return an integer from the dictionary, or None if missing/invalid."""
    value = data.get(key)

    if value is None or value == "":
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def text_value(value: Any) -> str:
    """Return a string value or '-' when it is unavailable."""
    if value is None or value == "":
        return "-"

    return str(value)


# -----------------------------------------------------------------------------
# Template values
# -----------------------------------------------------------------------------


def build_values(
    summary: dict[str, Any],
    hostname: str,
    rustic_version: str,
    status: str,
    error_text: str,
) -> dict[str, str]:
    """Convert Rustic JSONL summary data into HTML template values."""

    files_new = optional_int(summary, "files_new")
    files_changed = optional_int(summary, "files_changed")
    files_unmodified = optional_int(summary, "files_unmodified")

    files_total = optional_int(summary, "total_files_processed")

    if (
        files_total is None
        and files_new is not None
        and files_changed is not None
        and files_unmodified is not None
    ):
        files_total = files_new + files_changed + files_unmodified

    duration, duration_label = format_duration(summary.get("total_duration"))
    data_processed, data_processed_unit = format_bytes_auto(
        summary.get("total_bytes_processed")
    )
    data_added, data_added_unit = format_bytes_auto(
        summary.get("data_added")
    )

    return {
        # Header
        "hostname": text_value(hostname),
        "report_date": format_report_date(),

        # Status
        "status": status,
        "status_label": "SUCCESS" if status == "success" else "FAILED",
        "error_message": text_value(error_text),

        # Backup summary
        "duration": duration,
        "duration_label": duration_label,
        "data_processed": data_processed,
        "data_processed_unit": data_processed_unit,
        "data_added": data_added,
        "data_added_unit": data_added_unit,
        "files_total": format_number(files_total),

        # File statistics
        "files_new": format_number(files_new),
        "files_new_percent": calculate_percent(files_new, files_total),
        "files_changed": format_number(files_changed),
        "files_changed_percent": calculate_percent(files_changed, files_total),
        "files_unmodified": format_number(files_unmodified),
        "files_unmodified_percent": calculate_percent(
            files_unmodified, files_total
        ),

        # Backup details
        "snapshot_id": text_value(summary.get("snapshot_id")),
        "data_added_repository": format_bytes_with_unit(
            summary.get("data_added_packed")
        ),

        # Footer
        "rustic_version": text_value(rustic_version),
    }


# -----------------------------------------------------------------------------
# HTML template rendering
# -----------------------------------------------------------------------------


def render_template(
    template: str,
    values: dict[str, str],
    html_values: dict[str, str] | None = None,
) -> str:
    """
    Replace {{ variable }} placeholders in HTML.

    Values are HTML-escaped. Values in html_values are inserted unchanged and
    must already be safe HTML. Any unknown placeholder is replaced with '-'.
    """
    raw_values = html_values or {}

    def replace_placeholder(match: re.Match[str]) -> str:
        key = match.group(1)

        if key in raw_values:
            return raw_values[key]

        value = values.get(key, "-")
        return html.escape(str(value), quote=True)

    return PLACEHOLDER_RE.sub(replace_placeholder, template)


# -----------------------------------------------------------------------------
# Mail
# -----------------------------------------------------------------------------


def send_report_mail(config_file: Path, status: str) -> None:
    """Call send_report_mail.py if [mail] sendmail = true."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(config_file, encoding="utf-8")

    if not parser.getboolean("mail", "sendmail", fallback=False):
        log.info("Mail sending is disabled ([mail] sendmail = false)")
        return

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_DIR / "send_report_mail.py"),
            "--config",
            str(config_file),
            "--status",
            status,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Mail sending failed (exit code {result.returncode}): "
            f"{result.stderr.strip() or 'see send_report_mail log'}"
        )

    log.info("Mail script finished successfully")


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------


def create_report(
    status: str,
    config: configparser.SectionProxy,
    config_file: Path,
) -> None:
    """Create the HTML report for the given status and send it if enabled."""
    log.info("Creating %s report", status)

    json_file = resolve_config_path(
        get_required_config(config, "jsonl_file"),
        config_file,
    )
    template_file = resolve_config_path(
        get_required_config(config, f"template-{status}"),
        config_file,
    )
    output_file = resolve_config_path(
        get_required_config(config, "output"),
        config_file,
    )

    error_text = ""

    if status == "failure":
        error_file = resolve_config_path(
            get_required_config(config, "error_log_file"),
            config_file,
        )
        error_text = read_error_text(error_file)

    hostname = get_hostname(config)
    rustic_version = get_rustic_version(config)

    summary = load_report_data(json_file, status)

    with template_file.open("r", encoding="utf-8") as file:
        template = file.read()

    values = build_values(
        summary=summary,
        hostname=hostname,
        rustic_version=rustic_version,
        status=status,
        error_text=strip_ansi(error_text),
    )
    html_values = {
        "error_message_html": ansi_to_html(error_text) if error_text else "-",
    }

    html_output = render_template(template, values, html_values)

    output_file.parent.mkdir(parents=True, exist_ok=True)

    with output_file.open("w", encoding="utf-8") as file:
        file.write(html_output)

    log.info("HTML report created: %s", output_file)

    send_report_mail(config_file, status)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Generate an HTML report from Rustic --json-progress JSONL output "
            "using rustic_report.conf."
        )
    )

    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG),
        help=(
            "Path to config file. Default: rustic_report.conf next to this script"
        ),
    )
    parser.add_argument(
        "--status",
        choices=("success", "failure"),
        default="success",
        help=(
            "Report type: selects the template and the mail subject. "
            "Default: success"
        ),
    )

    args = parser.parse_args()
    config_file = Path(args.config).expanduser().resolve()

    try:
        config = load_config(config_file)
    except (OSError, RuntimeError, configparser.Error) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    setup_logging(get_log_file(config, config_file))

    try:
        create_report(args.status, config, config_file)
    except (OSError, RuntimeError, ValueError) as error:
        log.error("Creating %s report failed: %s", args.status, error)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
