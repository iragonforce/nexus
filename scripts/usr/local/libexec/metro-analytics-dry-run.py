#!/usr/bin/env python3
import email
import hashlib
import imaplib
import json
import os
import re
import ssl
import subprocess
import traceback
import unicodedata
from collections import Counter
from decimal import Decimal
from email import policy
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

SENDER = "transaction@transaction-metro.ca"

CATEGORIES = {
    "GROCERY", "PRODUCE", "DAIRY", "BAKERY",
    "DELI", "MEAT", "FROZEN", "SEAFOOD",
    "GENERAL", "HOUSEHOLD"
}

PRICE = re.compile(r"^(.+?)\s+(-?\$?\d[\d,]*\.\d{2})$")
MULTI = re.compile(
    r"^(\d+)\s*@\s*\$?(\d+\.\d{2})\s+(\d+\.\d{2})$"
)

class HTMLText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        elif tag in ("br", "div", "p", "tr", "li"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)

def lines_from_html(html):
    parser = HTMLText()
    parser.feed(html)
    result = []

    for line in "".join(parser.parts).splitlines():
        line = "".join(
            c for c in line
            if unicodedata.category(c) != "Cf"
        )
        line = " ".join(line.split())
        if line:
            result.append(line)

    return result

def cents(value):
    number = Decimal(
        str(value).replace("$", "").replace(",", "").strip()
    )
    return int((number * 100).quantize(Decimal("1")))

def field(lines, name):
    pattern = re.compile(
        rf"^{name}\s*\$?([\d,]+\.\d{{2}})$", re.I
    )
    for line in lines:
        match = pattern.fullmatch(line)
        if match:
            return cents(match.group(1))
    raise ValueError(f"Missing {name}")

def parse_metro(lines):
    start = next(
        (i for i, line in enumerate(lines)
         if line in CATEGORIES),
        None
    )
    if start is None:
        raise ValueError("Item section missing")

    stop = next(
        (i for i in range(start, len(lines))
         if lines[i].startswith("SUBTOTAL")),
        None
    )
    if stop is None:
        raise ValueError("Subtotal missing")

    items = []
    discounts = []
    unknown = []
    category = None
    pending = None
    discount_label = None

    for line in lines[start:stop]:
        if line in CATEGORIES:
            category = line
            pending = None
            discount_label = None
            continue

        if (
            line.startswith(("Saving ", "POINTS"))
            or line == "Special Bonus Offer"
        ):
            continue

        if re.search(r"\bDiscount\s+\d+%", line, re.I):
            discount_label = line
            continue

        multi = MULTI.fullmatch(line)

        if multi and pending:
            quantity = int(multi.group(1))
            unit = cents(multi.group(2))
            total = cents(multi.group(3))

            if (
                quantity != pending[1]
                or quantity * unit != total
            ):
                unknown.append("Quantity or unit price mismatch")

            items.append({
                "name": pending[0],
                "category": category,
                "quantity": quantity,
                "line_total_cents": total
            })
            pending = None
            continue

        match = PRICE.fullmatch(line)

        if match:
            name = match.group(1).strip()
            value = cents(match.group(2))

            if value < 0:
                discounts.append({
                    "description": discount_label or name,
                    "amount_cents": value
                })
                discount_label = None
            elif name:
                items.append({
                    "name": name,
                    "category": category,
                    "quantity": 1,
                    "line_total_cents": value
                })
            continue

        match = re.fullmatch(r"\((\d+)\)(.+)", line)

        if match:
            pending = (
                match.group(2).strip(),
                int(match.group(1))
            )
            continue

        if re.search(r"\d+\.\d{2}", line):
            unknown.append(line[:80])

    subtotal = field(lines, "SUBTOTAL")
    total = field(lines, "TOTAL")

    tax = 0

    for line in lines[stop + 1:]:
        if line.startswith("TOTAL"):
            break
        if re.search(r"\b(HST|GST|PST)\b", line):
            values = re.findall(r"\d+\.\d{2}", line)
            if values:
                tax += cents(values[-1])

    calculated = (
        sum(i["line_total_cents"] for i in items)
        + sum(d["amount_cents"] for d in discounts)
    )

    store = next(
        (
            match.group(1)
            for line in lines
            if (match := re.search(
                r"\bStore\s*#\s*(\d+)", line, re.I
            ))
        ),
        None
    )

    body_dates = list(dict.fromkeys(
        match
        for line in lines
        for match in re.findall(
            r"\b(?:20\d{2}[-/]\d{1,2}[-/]\d{1,2}"
            r"|\d{1,2}[-/]\d{1,2}[-/]20\d{2})\b",
            line
        )
    ))[:3]

    return {
        "items": items,
        "discounts": discounts,
        "store": store,
        "body_dates": body_dates,
        "subtotal_cents": subtotal,
        "tax_cents": tax,
        "total_cents": total,
        "item_difference": calculated - subtotal,
        "total_difference": subtotal + tax - total,
        "unknown": unknown,
        "valid": (
            bool(items)
            and not unknown
            and abs(calculated - subtotal) <= 1
            and abs(subtotal + tax - total) <= 1
        )
    }

def database_rows(container, username, database, query):
    result = subprocess.run(
        [
            "/usr/bin/docker", "exec", container,
            "psql", "-X", "-A", "-t",
            "-v", "ON_ERROR_STOP=1",
            "-U", username, "-d", database,
            "-c", query
        ],
        capture_output=True,
        text=True,
        timeout=30
    )

    if result.returncode:
        raise RuntimeError(
            f"{container}: {result.stderr.strip()}"
        )

    return [
        json.loads(line)
        for line in result.stdout.splitlines()
        if line.strip()
    ]

def existing_records():
    analytics = database_rows(
        "finance-analytics-db",
        "finance_analytics",
        "finance_analytics",
        """
        SELECT json_build_object(
            'source_key', source_key
        )::text
        FROM purchase_receipts
        WHERE source = 'metro_email';
        """
    )

    wrangler = database_rows(
        "receipt-wrangler-db-1",
        "wrangler",
        "wrangler",
        """
        SELECT json_build_object(
            'id', id,
            'date',
              (date AT TIME ZONE 'America/Toronto')::date::text,
            'amount_cents', round(amount * 100)::bigint
        )::text
        FROM receipts
        WHERE name ILIKE '%metro%';
        """
    )

    return (
        {r["source_key"] for r in analytics},
        wrangler
    )

def credentials():
    raw = os.environ.get("PROTON_BRIDGE_IMAP", "").strip()

    if not raw:
        raise RuntimeError("Bridge credentials unavailable")

    try:
        data = json.loads(raw)
    except ValueError:
        data = raw

    username = "info@blinkwired.com"

    if isinstance(data, dict):
        fields = {
            str(k).lower(): v
            for k, v in data.items()
        }

        username = str(next(
            (
                fields[k] for k in (
                    "username", "user", "email",
                    "imap_username"
                )
                if fields.get(k)
            ),
            username
        ))

        password = str(next(
            (
                fields[k] for k in (
                    "password", "pass",
                    "imap_password", "imap_pass"
                )
                if fields.get(k)
            ),
            ""
        ))
    else:
        password = str(data)

    if not password:
        raise RuntimeError("Bridge password unavailable")

    return username, password

def main():
    print("=== METRO ANALYTICS DRY RUN ===", flush=True)

    imported, wrangler = existing_records()

    print("Existing analytics receipts:", len(imported))
    print("Receipt Wrangler Metro candidates:", len(wrangler))

    username, password = credentials()

    client = imaplib.IMAP4(
        "127.0.0.1", 1143, timeout=30
    )
    client.starttls(
        ssl_context=ssl._create_unverified_context()
    )
    client.login(username, password)

    try:
        status, _ = client.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError("Cannot open INBOX")

        status, result = client.status(
            "INBOX", "(UIDVALIDITY)"
        )

        if status != "OK" or not result:
            raise RuntimeError("UIDVALIDITY unavailable")

        value = result[0].decode()
        match = re.search(r"UIDVALIDITY\s+(\d+)", value)

        if not match:
            raise RuntimeError("Invalid UIDVALIDITY")

        uidvalidity = match.group(1)

        status, result = client.uid(
            "SEARCH", None, "FROM", f'"{SENDER}"'
        )

        if status != "OK":
            raise RuntimeError("IMAP search failed")

        uids = result[0].split() if result[0] else []

        print("Emails found:", len(uids))

        counts = Counter()
        seen = set()

        for uid in uids:
            uid_text = uid.decode()

            try:
                status, response = client.uid(
                    "FETCH", uid_text, "(BODY.PEEK[])"
                )

                if status != "OK":
                    raise RuntimeError("IMAP fetch failed")

                blobs = [
                    part[1]
                    for part in response
                    if isinstance(part, tuple)
                    and isinstance(part[1], bytes)
                ]

                if not blobs:
                    raise RuntimeError("Message missing")

                message = email.message_from_bytes(
                    blobs[0], policy=policy.default
                )

                sender = parseaddr(
                    str(message.get("From", ""))
                )[1].lower()

                if sender != SENDER:
                    raise RuntimeError("Sender mismatch")

                html = next(
                    (
                        part.get_content()
                        for part in message.walk()
                        if part.get_content_type() == "text/html"
                        and not part.get_filename()
                    ),
                    None
                )

                if not html:
                    raise RuntimeError("HTML body missing")

                parsed = parse_metro(lines_from_html(html))

                email_date = parsedate_to_datetime(
                    str(message["Date"])
                ).astimezone(
                    ZoneInfo("America/Toronto")
                ).date().isoformat()

                message_id = str(
                    message.get("Message-ID", "")
                ).strip().lower()

                if message_id:
                    source_key = (
                        "message-id:"
                        + hashlib.sha256(
                            message_id.encode()
                        ).hexdigest()
                    )
                else:
                    source_key = (
                        f"inbox:{uidvalidity}:{uid_text}"
                    )

                duplicate_mail_id = source_key in seen
                seen.add(source_key)

                wrangler_matches = [
                    row["id"]
                    for row in wrangler
                    if (
                        row["date"] == email_date
                        and int(row["amount_cents"])
                        == parsed["total_cents"]
                    )
                ]

                if not parsed["valid"]:
                    state = "REVIEW_PARSE"
                elif duplicate_mail_id:
                    state = "REVIEW_MAIL_ID"
                elif source_key in imported:
                    state = "ALREADY_IMPORTED"
                elif wrangler_matches:
                    state = "REVIEW_WRANGLER"
                else:
                    state = "NEW_VALIDATED"

                counts[state] += 1

                print(
                    f"UID={uid_text} "
                    f"DATE={email_date} "
                    f"ITEMS={len(parsed['items'])} "
                    f"DISCOUNTS={len(parsed['discounts'])} "
                    f"TOTAL=${parsed['total_cents']/100:.2f} "
                    f"STATUS={state}"
                )

                if parsed["body_dates"]:
                    print(
                        "  Receipt date candidates:",
                        ", ".join(parsed["body_dates"])
                    )

                if state.startswith("REVIEW"):
                    print(
                        "  Item difference (cents):",
                        parsed["item_difference"]
                    )
                    print(
                        "  Tax difference (cents):",
                        parsed["total_difference"]
                    )
                    print(
                        "  Wrangler matches:",
                        wrangler_matches
                    )
                    if parsed["unknown"]:
                        print(
                            "  Unparsed lines:",
                            parsed["unknown"][:3]
                        )

            except Exception as error:
                counts["ERROR"] += 1
                print(
                    f"UID={uid_text} ERROR="
                    f"{type(error).__name__}: {error}"
                )

        print("\n=== SUMMARY ===")

        for state in (
            "NEW_VALIDATED",
            "ALREADY_IMPORTED",
            "REVIEW_WRANGLER",
            "REVIEW_MAIL_ID",
            "REVIEW_PARSE",
            "ERROR"
        ):
            print(f"{state}: {counts[state]}")

        print("Email dates: PROVISIONAL purchase dates")
        print("Database writes: 0")
        print("Emails modified: 0")
        print("RESULT:", (
            "PASS"
            if not counts["REVIEW_PARSE"] and not counts["ERROR"]
            else "PARTIAL"
        ))

    finally:
        try:
            client.logout()
        except Exception:
            pass

if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("\n=== FULL ERROR ===")
        detail = traceback.format_exc()
        raw = os.environ.get("PROTON_BRIDGE_IMAP", "")

        if raw:
            detail = detail.replace(raw, "[REDACTED]")
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict):
                    for key, value in obj.items():
                        if key.lower() in (
                            "password", "pass",
                            "imap_password", "imap_pass"
                        ) and value:
                            detail = detail.replace(
                                str(value), "[REDACTED]"
                            )
            except ValueError:
                pass

        print(detail)
        print("RESULT: FAIL")
