#!/usr/bin/env python3
import email
import imaplib
import json
import os
import re
import ssl
import traceback
import unicodedata
from collections import Counter
from decimal import Decimal
from email import policy
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser

SENDER = "noreply@noreply.timhortons.ca"

MONEY = re.compile(r"^\s*\$?\s*(\d[\d,]*\.\d{2})\s*$")
QUANTITY_ITEM = re.compile(r"^(\d{1,3})\s*([A-Za-z].+?)\s*$")
QUANTITY_INLINE = re.compile(
    r"^(\d{1,3})\s*([A-Za-z].*?)\s*\$\s*([\d,]+\.\d{2})\s*$"
)
LABEL_PRICE = re.compile(
    r"^(Subtotal|Tax|Total)\s*:?\s*\$?\s*([\d,]+\.\d{2})\s*$",
    re.I
)
LABEL_ONLY = re.compile(r"^(Subtotal|Tax|Total)\s*:?\s*$", re.I)
DISCOUNT = re.compile(
    r"\b(discount|rewards?|redeem|redemption|coupon|promo|offer)\b",
    re.I
)
NEGATIVE = re.compile(r"-\s*\$?\s*([\d,]+\.\d{2})\s*$")


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


def clean_lines(html):
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


def as_money(value):
    return Decimal(
        str(value).replace(",", "").replace("$", "")
    ).quantize(Decimal("0.01"))


def parse_tims(lines):
    start = next(
        (
            i for i, line in enumerate(lines)
            if re.fullmatch(r"Order\s+\d+", line, re.I)
        ),
        None
    )

    if start is None:
        raise ValueError("Missing English order number")

    order = re.search(r"\d+", lines[start]).group()

    end = next(
        (
            i for i in range(start + 1, len(lines))
            if lines[i].startswith((
                "Order placed to", "Commande ", "French:"
            ))
        ),
        len(lines)
    )

    receipt = lines[start:end]
    totals = {}
    subtotal_index = None

    for i, line in enumerate(receipt):
        match = LABEL_PRICE.match(line)

        if match:
            key = match.group(1).lower()

            if key not in totals:
                totals[key] = as_money(match.group(2))
                if key == "subtotal":
                    subtotal_index = i
            continue

        match = LABEL_ONLY.match(line)

        if match and i + 1 < len(receipt):
            price = MONEY.match(receipt[i + 1])

            if price:
                key = match.group(1).lower()

                if key not in totals:
                    totals[key] = as_money(price.group(1))
                    if key == "subtotal":
                        subtotal_index = i

    if set(totals) != {"subtotal", "tax", "total"}:
        raise ValueError("Missing Subtotal, Tax, or Total")

    if subtotal_index is None:
        raise ValueError("Subtotal position not found")

    # Skip order number, date, displayed total and customer heading.
    pos = 1

    if pos < len(receipt) and re.match(
        r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun),",
        receipt[pos]
    ):
        pos += 1

    if pos < len(receipt) and MONEY.fullmatch(receipt[pos]):
        pos += 1

    if (
        pos < len(receipt)
        and not re.match(r"^\d", receipt[pos])
        and not DISCOUNT.search(receipt[pos])
    ):
        pos += 1

    fragment = receipt[pos:subtotal_index]

    items = []
    modifiers = []
    discounts = []
    unknown = []

    i = 0

    while i < len(fragment):
        line = fragment[i]

        if re.search(
            r"discount|redeem|redemption|coupon|promo|offer",
            line, re.I
        ):
            negative = NEGATIVE.search(line)
            step = 1

            if negative:
                value = -as_money(negative.group(1))
            elif (
                i + 1 < len(fragment)
                and NEGATIVE.search(fragment[i + 1])
            ):
                value = -as_money(
                    NEGATIVE.search(fragment[i + 1]).group(1)
                )
                step = 2
            else:
                unknown.append("Unparsed discount: " + line[:80])
                i += 1
                continue

            discounts.append({
                "label": line[:80],
                "amount": str(value)
            })

            i += step
            continue

        quantity = None
        name = None
        price = None
        step = 1

        match = QUANTITY_INLINE.match(line)

        if match:
            quantity = int(match.group(1))
            name = match.group(2).strip()
            price = as_money(match.group(3))
        else:
            if (
                re.fullmatch(r"\d{1,3}", line)
                and i + 2 < len(fragment)
            ):
                quantity = int(line)
                name = fragment[i + 1]
                step = 2
            else:
                match = QUANTITY_ITEM.match(line)

                if match:
                    quantity = int(match.group(1))
                    name = match.group(2).strip()
                elif re.match(
                    r"^(No|Extra|Add|Less|Remove|Without)\b",
                    line, re.I
                ):
                    quantity = 1
                    name = line

            if name is not None:
                embedded = re.match(
                    r"^(.+?)\s*\$\s*([\d,]+\.\d{2})$",
                    name
                )

                if embedded:
                    name = embedded.group(1).strip()
                    price = as_money(embedded.group(2))
                elif (
                    i + step < len(fragment)
                    and (next_price := MONEY.match(
                        fragment[i + step]
                    ))
                ):
                    price = as_money(next_price.group(1))
                    step += 1

        if name and price is not None:
            target = (
                modifiers if price == 0 and items
                else items
            )

            target.append({
                "name": name,
                "quantity": quantity,
                "amount": str(price)
            })

            i += step
            continue

        if (
            re.search(r"\$?\d+\.\d{2}", line)
            or re.match(r"^\d", line)
        ):
            unknown.append("Unparsed item: " + line[:80])

        i += 1

    calculated = sum(
        (
            as_money(entry["amount"])
            for entry in items + modifiers + discounts
        ),
        Decimal("0")
    )

    difference = calculated - totals["subtotal"]
    tax_difference = (
        totals["subtotal"]
        + totals["tax"]
        - totals["total"]
    )

    valid = (
        bool(items)
        and not unknown
        and abs(difference) <= Decimal("0.01")
        and abs(tax_difference) <= Decimal("0.01")
    )

    return {
        "order": order,
        "item_count": len(items),
        "modifier_count": len(modifiers),
        "discount_count": len(discounts),
        "subtotal": str(totals["subtotal"]),
        "tax": str(totals["tax"]),
        "total": str(totals["total"]),
        "item_difference": str(difference),
        "tax_difference": str(tax_difference),
        "unknown": unknown,
        "valid": valid
    }


def credentials():
    raw = os.environ.get("PROTON_BRIDGE_IMAP", "")

    if not raw.strip():
        raise RuntimeError("Bridge credential not injected")

    try:
        data = json.loads(raw)
    except ValueError:
        data = raw

    username = "info@blinkwired.com"

    if isinstance(data, dict):
        fields = {
            str(key).lower(): value
            for key, value in data.items()
        }

        username = str(
            fields.get("username")
            or fields.get("user")
            or fields.get("email")
            or username
        )

        password = next(
            (
                str(fields[key])
                for key in (
                    "password", "pass",
                    "imap_password", "imap_pass"
                )
                if fields.get(key)
            ),
            ""
        )
    else:
        password = str(data).strip()

    if not password:
        raise RuntimeError("Bridge password unavailable")

    return username, password


def main():
    print("=== TIM HORTONS PARSER V2 AUDIT ===", flush=True)

    username, password = credentials()

    client = imaplib.IMAP4("127.0.0.1", 1143)
    client.starttls(
        ssl_context=ssl._create_unverified_context()
    )

    client.login(username, password)

    status, _ = client.select("INBOX", readonly=True)

    if status != "OK":
        raise RuntimeError("Cannot open INBOX")

    status, result = client.uid(
        "SEARCH", None, "FROM", f'"{SENDER}"'
    )

    if status != "OK":
        raise RuntimeError("IMAP search failed")

    uids = result[0].split() if result[0] else []

    print("Messages found:", len(uids))
    print("IMAP mode: READ-ONLY")

    counts = Counter()

    for uid in uids:
        uid_text = uid.decode()
        counts["searched"] += 1

        try:
            status, result = client.uid(
                "FETCH", uid_text, "(BODY.PEEK[])"
            )

            if status != "OK":
                raise RuntimeError("IMAP fetch failed")

            blobs = [
                entry[1]
                for entry in result
                if isinstance(entry, tuple)
                and isinstance(entry[1], bytes)
            ]

            if not blobs:
                raise RuntimeError("Message not found")

            message = email.message_from_bytes(
                blobs[0], policy=policy.default
            )

            sender = parseaddr(
                str(message.get("From", ""))
            )[1].lower()

            if sender != SENDER:
                counts["sender_mismatch"] += 1
                continue

            subject = str(message.get("Subject", ""))

            if not re.search(
                r"receipt|thanks for your order",
                subject, re.I
            ):
                counts["non_receipt_skipped"] += 1
                continue

            html = None

            for part in message.walk():
                if (
                    part.get_content_type() == "text/html"
                    and not part.get_filename()
                ):
                    html = part.get_content()
                    break

            if html is None:
                raise ValueError("No HTML receipt body")

            parsed = parse_tims(clean_lines(html))

            email_date = parsedate_to_datetime(
                str(message["Date"])
            ).date().isoformat()

            state = "PASS" if parsed["valid"] else "MISMATCH"

            print(
                f"UID={uid_text} "
                f"DATE={email_date} "
                f"ORDER={parsed['order']} "
                f"ITEMS={parsed['item_count']} "
                f"MODIFIERS={parsed['modifier_count']} "
                f"DISCOUNTS={parsed['discount_count']} "
                f"TOTAL={parsed['total']} "
                f"STATUS={state}"
            )

            if not parsed["valid"]:
                print(
                    "  Item difference:",
                    parsed["item_difference"]
                )
                print(
                    "  Tax difference:",
                    parsed["tax_difference"]
                )
                print(
                    "  Unparsed lines:",
                    parsed["unknown"][:5]
                )

            counts[state] += 1

        except Exception as error:
            counts["ERROR"] += 1

            print(
                f"UID={uid_text} STATUS=ERROR "
                f"{type(error).__name__}: {error}"
            )

    client.logout()

    print("\n=== SUMMARY ===")

    for key in (
        "searched",
        "PASS",
        "MISMATCH",
        "ERROR",
        "non_receipt_skipped",
        "sender_mismatch"
    ):
        print(f"{key}: {counts[key]}")

    print("Ready for import:", counts["PASS"])
    print("Emails modified: NO")
    print("Receipts created: NO")
    print("Wealthfolio modified: NO")
    print("Credentials disclosed: NO")

    if counts["MISMATCH"] or counts["ERROR"]:
        print("RESULT: PARTIAL")
    else:
        print("RESULT: PASS")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("\n=== FULL ERROR ===")
        detail = traceback.format_exc()
        raw = os.environ.get("PROTON_BRIDGE_IMAP", "")

        if raw:
            detail = detail.replace(raw, "[REDACTED]")

        print(detail)
        print("RESULT: FAIL")
