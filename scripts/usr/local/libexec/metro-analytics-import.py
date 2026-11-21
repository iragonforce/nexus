#!/usr/bin/env python3
import email
import hashlib
import importlib.util
import imaplib
import json
import re
import ssl
import subprocess
import sys
import traceback
from collections import Counter
from datetime import datetime
from email import policy
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PARSER = "/usr/local/libexec/metro-analytics-dry-run.py"
SENDER = "transaction@transaction-metro.ca"

def load_parser():
    spec = importlib.util.spec_from_file_location(
        "metro_analytics_parser", PARSER
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def fetch_receipts(module):
    username, password = module.credentials()

    client = imaplib.IMAP4(
        "127.0.0.1", 1143, timeout=30
    )

    try:
        client.starttls(
            ssl_context=ssl._create_unverified_context()
        )
        client.login(username, password)

        status, _ = client.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError("INBOX unavailable")

        status, result = client.status(
            "INBOX", "(UIDVALIDITY)"
        )
        if status != "OK":
            raise RuntimeError("UIDVALIDITY unavailable")

        match = re.search(
            r"UIDVALIDITY\s+(\d+)",
            result[0].decode()
        )
        if not match:
            raise RuntimeError("Cannot determine UIDVALIDITY")

        uidvalidity = int(match.group(1))

        status, result = client.uid(
            "SEARCH", None, "FROM", f'"{SENDER}"'
        )
        if status != "OK":
            raise RuntimeError("Email search failed")

        uids = result[0].split() if result[0] else []

        records = []
        seen = set()

        for uid in uids:
            uid_number = int(uid)

            status, result = client.uid(
                "FETCH", uid, "(BODY.PEEK[])"
            )
            if status != "OK":
                raise RuntimeError(
                    f"UID {uid_number}: fetch failed"
                )

            blobs = [
                entry[1]
                for entry in result
                if isinstance(entry, tuple)
                and isinstance(entry[1], bytes)
            ]

            if not blobs:
                raise RuntimeError(
                    f"UID {uid_number}: empty message"
                )

            message = email.message_from_bytes(
                blobs[0], policy=policy.default
            )

            sender = parseaddr(
                str(message.get("From", ""))
            )[1].lower()

            if sender != SENDER:
                raise RuntimeError(
                    f"UID {uid_number}: sender mismatch"
                )

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
                raise RuntimeError(
                    f"UID {uid_number}: HTML body missing"
                )

            parsed = module.parse_metro(
                module.lines_from_html(html)
            )

            if not parsed["valid"]:
                raise RuntimeError(
                    f"UID {uid_number}: receipt validation failed"
                )

            email_date = parsedate_to_datetime(
                str(message["Date"])
            ).astimezone(
                ZoneInfo("America/Toronto")
            ).date()

            body_dates = {
                datetime.strptime(value, "%m/%d/%Y").date()
                for value in parsed["body_dates"]
            }

            if len(body_dates) != 1:
                raise RuntimeError(
                    f"UID {uid_number}: ambiguous receipt date"
                )

            purchase_date = next(iter(body_dates))

            if purchase_date != email_date:
                raise RuntimeError(
                    f"UID {uid_number}: receipt/email date mismatch"
                )

            message_id = str(
                message.get("Message-ID", "")
            ).strip()

            if message_id:
                source_key = (
                    "message-id:"
                    + hashlib.sha256(
                        message_id.lower().encode()
                    ).hexdigest()
                )
            else:
                source_key = (
                    f"inbox:{uidvalidity}:{uid_number}"
                )

            if source_key in seen:
                raise RuntimeError(
                    f"UID {uid_number}: duplicate email identity"
                )

            seen.add(source_key)

            records.append({
                "source_key": source_key,
                "message_id": message_id,
                "uid": uid_number,
                "uidvalidity": uidvalidity,
                "purchase_date": purchase_date.isoformat(),
                "store": parsed["store"],
                "subtotal_cents": parsed["subtotal_cents"],
                "tax_cents": parsed["tax_cents"],
                "total_cents": parsed["total_cents"],
                "items": parsed["items"],
                "discounts": parsed["discounts"],
                "source_data": {
                    "parser": "metro-v1",
                    "date_source": "receipt_body",
                    "email_date": email_date.isoformat(),
                    "item_difference_cents": parsed["item_difference"],
                    "total_difference_cents": parsed["total_difference"]
                }
            })

        return records

    finally:
        try:
            client.logout()
        except Exception:
            pass

def check_collisions(module, records):
    imported, wrangler = module.existing_records()

    analytics = module.database_rows(
        "finance-analytics-db",
        "finance_analytics",
        "finance_analytics",
        """
        SELECT json_build_object(
            'source', source,
            'source_key', source_key,
            'date', purchase_date::text,
            'total_cents', total_cents
        )::text
        FROM purchase_receipts
        WHERE merchant ILIKE '%METRO%'
          AND duplicate_of_id IS NULL;
        """
    )

    new = []
    signatures = set()

    for record in records:
        key = record["source_key"]

        if key in imported:
            continue

        signature = (
            record["purchase_date"],
            record["total_cents"],
            record["store"]
        )

        if signature in signatures:
            raise RuntimeError(
                "Two Metro emails have identical purchase signatures"
            )

        signatures.add(signature)

        for existing in wrangler:
            if (
                existing["date"] == record["purchase_date"]
                and int(existing["amount_cents"])
                == record["total_cents"]
            ):
                raise RuntimeError(
                    "Potential Receipt Wrangler duplicate: "
                    f"UID {record['uid']}, "
                    f"Wrangler receipt {existing['id']}"
                )

        for existing in analytics:
            if (
                existing["date"] == record["purchase_date"]
                and int(existing["total_cents"])
                == record["total_cents"]
            ):
                raise RuntimeError(
                    "Potential cross-source analytics duplicate: "
                    f"UID {record['uid']}"
                )

        new.append(record)

    return new

def import_records(records):
    if not records:
        print("No new receipts to insert")
        return

    payload = json.dumps(
        records,
        separators=(",", ":"),
        ensure_ascii=False
    ).encode().hex()

    sql = f"""
BEGIN;

WITH input AS (
    SELECT doc
    FROM jsonb_array_elements(
        convert_from(
            decode('{payload}', 'hex'),
            'UTF8'
        )::jsonb
    ) AS entry(doc)
),
created AS (
    INSERT INTO purchase_receipts (
        source,
        source_key,
        message_id,
        imap_uid,
        uidvalidity,
        merchant,
        store_code,
        purchase_date,
        currency,
        subtotal_cents,
        tax_cents,
        total_cents,
        status,
        source_data
    )
    SELECT
        'metro_email',
        doc->>'source_key',
        doc->>'message_id',
        (doc->>'uid')::bigint,
        (doc->>'uidvalidity')::bigint,
        'METRO',
        doc->>'store',
        (doc->>'purchase_date')::date,
        'CAD',
        (doc->>'subtotal_cents')::bigint,
        (doc->>'tax_cents')::bigint,
        (doc->>'total_cents')::bigint,
        'validated',
        doc->'source_data'
    FROM input
    ON CONFLICT (source, source_key) DO NOTHING
    RETURNING id, source_key
),
created_items AS (
    INSERT INTO receipt_items (
        receipt_id,
        line_number,
        raw_name,
        category,
        quantity,
        line_total_cents
    )
    SELECT
        r.id,
        item.ordinality::integer,
        item.value->>'name',
        item.value->>'category',
        (item.value->>'quantity')::numeric,
        (item.value->>'line_total_cents')::bigint
    FROM created r
    JOIN input p
      ON p.doc->>'source_key' = r.source_key
    CROSS JOIN LATERAL jsonb_array_elements(
        p.doc->'items'
    ) WITH ORDINALITY AS item(value, ordinality)
    RETURNING id
),
created_discounts AS (
    INSERT INTO receipt_adjustments (
        receipt_id,
        description,
        adjustment_type,
        amount_cents
    )
    SELECT
        r.id,
        adjustment.value->>'description',
        'basket_discount',
        (adjustment.value->>'amount_cents')::bigint
    FROM created r
    JOIN input p
      ON p.doc->>'source_key' = r.source_key
    CROSS JOIN LATERAL jsonb_array_elements(
        p.doc->'discounts'
    ) AS adjustment(value)
    RETURNING id
)
SELECT json_build_object(
    'created', (SELECT count(*) FROM created),
    'item_lines', (SELECT count(*) FROM created_items),
    'discounts', (SELECT count(*) FROM created_discounts)
)::text;

COMMIT;
"""

    result = subprocess.run(
        [
            "/usr/bin/docker", "exec", "-i",
            "finance-analytics-db",
            "psql", "-X", "-A", "-t",
            "-v", "ON_ERROR_STOP=1",
            "-U", "finance_analytics",
            "-d", "finance_analytics"
        ],
        input=sql,
        capture_output=True,
        text=True,
        timeout=60
    )

    if result.returncode:
        print("=== COMPLETE POSTGRESQL ERROR ===")
        print(result.stdout)
        print(result.stderr)
        raise RuntimeError("Analytics import transaction failed")

    print("=== POSTGRESQL IMPORT RESULT ===")
    print(result.stdout.strip())

def verify(module):
    rows = module.database_rows(
        "finance-analytics-db",
        "finance_analytics",
        "finance_analytics",
        """
        SELECT json_build_object(
            'receipts', COUNT(*),
            'total_cents', COALESCE(SUM(total_cents), 0),
            'item_lines', (
                SELECT COUNT(*)
                FROM receipt_items i
                JOIN purchase_receipts r
                    ON r.id = i.receipt_id
                WHERE r.source = 'metro_email'
            ),
            'discount_adjustments', (
                SELECT COUNT(*)
                FROM receipt_adjustments a
                JOIN purchase_receipts r
                    ON r.id = a.receipt_id
                WHERE r.source = 'metro_email'
            )
        )::text
        FROM purchase_receipts
        WHERE source = 'metro_email';
        """
    )

    if len(rows) != 1:
        raise RuntimeError("Unexpected database verification result")

    return rows[0]

def main():
    print("=== METRO ANALYTICS IMPORT ===", flush=True)

    if sys.argv[1:] != ["--commit"]:
        raise RuntimeError("Explicit --commit argument required")

    module = load_parser()
    records = fetch_receipts(module)

    print("Receipts retrieved:", len(records))

    if len(records) != 16:
        raise RuntimeError(
            "Expected 16 audited emails. "
            "Run the dry-run again before importing."
        )

    total = sum(r["total_cents"] for r in records)

    print("Validated total: ${:.2f}".format(total / 100))

    if total != 39974:
        raise RuntimeError(
            "Total differs from the audited CAD 399.74"
        )

    new = check_collisions(module, records)

    print("Already imported:", len(records) - len(new))
    print("New receipts:", len(new))
    print("Date verification: PASS")
    print("Cross-source duplicate check: PASS")

    import_records(new)

    report = verify(module)

    print("\n=== ANALYTICS DATABASE ===")
    print("Receipts:", report["receipts"])
    print("Item lines:", report["item_lines"])
    print("Discount adjustments:", report["discount_adjustments"])
    print(
        "Receipt total: ${:.2f}".format(
            int(report["total_cents"]) / 100
        )
    )

    if (
        int(report["receipts"]) != 16
        or int(report["total_cents"]) != 39974
    ):
        raise RuntimeError("Post-import verification mismatch")

    print("\n=== COMPLETE ===")
    print("Analytics import: PASS")
    print("Receipt Wrangler writes: NONE")
    print("Wealthfolio writes: NONE")
    print("Emails modified: NONE")
    print("Credentials disclosed: NO")

try:
    main()
except Exception:
    print("\n=== FULL ERROR ===")
    detail = traceback.format_exc()

    secret = __import__("os").environ.get(
        "PROTON_BRIDGE_IMAP", ""
    )
    if secret:
        detail = detail.replace(secret, "[REDACTED]")
        try:
            value = json.loads(secret)
            if isinstance(value, dict):
                for key, text in value.items():
                    if key.lower() in (
                        "password", "pass",
                        "imap_password", "imap_pass"
                    ) and text:
                        detail = detail.replace(
                            str(text), "[REDACTED]"
                        )
        except ValueError:
            pass

    print(detail)
    print("Analytics import: FAIL")
    sys.exit(1)
