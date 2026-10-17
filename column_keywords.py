"""
Header-row and column-name detection keywords for master vendor sheets.

Edit these lists when new sheet layouts appear — parsers import from here.
"""

# A row is treated as the header when it matches at least this many keywords.
HEADER_MATCH_THRESHOLD = 3

# Keywords / phrases that indicate a header row (matched case-insensitively
# against cell text after normalizing whitespace).
HEADER_DETECTION_KEYWORDS = [
    "sr.no",
    "sr no",
    "s.no",
    "vendor name",
    "vendor",
    "contact person",
    "address",
    "city",
    "state",
    "pincode",
    "pin code",
    "mobile",
    "phone",
    "email",
    "email id",
    "pan",
    "pan no",
    "gst",
    "gst no",
    "msme",
    "msme no",
    "rfp",
    "rfp no",
    "rpf",
    "rpf no",
    "bank name",
    "beneficiary",
    "account no",
    "account number",
    "ifsc",
]

# Map: database column -> list of header keywords (first match wins per column).
# Matching is substring-based on normalized header text.
COLUMN_DETECTION_KEYWORDS = {
    "sr_no": [
        "sr.no",
        "sr no",
        "s.no",
        "s no",
        "serial",
        "sl.no",
        "sl no",
    ],
    "name": [
        "vendor name",
        "vendor",
        "party name",
        "supplier name",
        "company name",
    ],
    "contact_person": [
        "contact person",
        "contact name",
        "contact",
        "person name",
    ],
    "address": [
        "address",
        "addr",
        "registered address",
    ],
    "city": [
        "city",
        "town",
    ],
    "state": [
        "state",
        "province",
    ],
    "pincode": [
        "pincode",
        "pin code",
        "pin",
        "zip",
        "postal code",
    ],
    "phone": [
        "mobile-1",
        "mobile 1",
        "mobile1",
        "mobile",
        "phone",
        "contact no",
        "contact number",
        "cellphone",
    ],
    "email": [
        "email id",
        "email",
        "e-mail",
        "mail id",
    ],
    "pan": [
        "pan no",
        "pan number",
        "pan",
    ],
    "gst": [
        "gst no",
        "gstin",
        "gst number",
        "gst",
    ],
    "msme": [
        "msme no",
        "msme number",
        "udyam",
        "msme",
    ],
    "rfp_no": [
        "rfp no",
        "rfp number",
        "rfp",
        "rpf no",
        "rpf number",
        "rpf",
        "tender no",
        "tender number",
    ],
    "bank_name": [
        "bank name",
        "bank",
    ],
    "beneficiary_name": [
        "beneficiary name",
        "beneficiary",
        "bene name",
    ],
    "account_no": [
        "account no",
        "account number",
        "a/c no",
        "ac no",
        "account",
    ],
    "ifsc_code": [
        "ifsc code",
        "ifsc",
    ],
}

# Ordered DB columns used for INSERT (excludes id / timestamps).
VENDOR_DATA_COLUMNS = list(COLUMN_DETECTION_KEYWORDS.keys())
