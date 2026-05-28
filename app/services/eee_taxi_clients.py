"""Client master lookup for EEE-Taxi invoicing.

Maps client GSTIN -> entity name, address, and GST state details.
Add new entries to CLIENT_MASTER when a new GSTIN appears in the CSV.
"""
from __future__ import annotations

from dataclasses import dataclass

STATE_CODES: dict[str, str] = {
    "01": "Jammu and Kashmir",  "02": "Himachal Pradesh",
    "03": "Punjab",             "04": "Chandigarh",
    "05": "Uttarakhand",        "06": "Haryana",
    "07": "Delhi",              "08": "Rajasthan",
    "09": "Uttar Pradesh",      "10": "Bihar",
    "11": "Sikkim",             "12": "Arunachal Pradesh",
    "13": "Nagaland",           "14": "Manipur",
    "15": "Mizoram",            "16": "Tripura",
    "17": "Meghalaya",          "18": "Assam",
    "19": "West Bengal",        "20": "Jharkhand",
    "21": "Odisha",             "22": "Chhattisgarh",
    "23": "Madhya Pradesh",     "24": "Gujarat",
    "25": "Daman and Diu",      "26": "Dadra and Nagar Haveli",
    "27": "Maharashtra",        "28": "Andhra Pradesh",
    "29": "Karnataka",          "30": "Goa",
    "31": "Lakshadweep",        "32": "Kerala",
    "33": "Tamil Nadu",         "34": "Puducherry",
    "35": "Andaman and Nicobar Islands",
    "36": "Telangana",          "37": "Andhra Pradesh",
    "38": "Ladakh",
}

EEE_TAXI_STATE_CODE = "07"  # Delhi — EEE-Taxi's home state


@dataclass(frozen=True)
class ClientRecord:
    entity_name: str
    address: str
    gstin: str

    @property
    def state_code(self) -> str:
        return self.gstin[:2]

    @property
    def state_name(self) -> str:
        return STATE_CODES.get(self.state_code, "")


_DLF_A = "BUILDING NO.8, 8TH FLOOR, TOWER-B, DLF, CYBER CITY, Gurugram, Haryana, 122002"
_DLF_B = "Building No. 8, 8th Floor, Tower-B, DLF, Cyber City, Gurugram, Haryana, 122002"
_DLF_C = "Building No 8, Tower B, 8th Floor, DLF, Cyber City, Gurugram, Haryana, 122002"
_SUCHETA = "11A, 1st floor, Sucheta Bhawan, Vishnu Digamber Marg, New Delhi, Delhi, 110002"

_RAW: list[tuple[str, str, str]] = [
    # (gstin, entity_name, address)
    ("06AAHFP0187A1ZM", "PRICE WATERHOUSE & CO CHARTERED ACCOUNTANTS LLP", _DLF_A),

    ("06AAEFP1428R1ZW", "PRICE WATERHOUSE & CO LLP", _DLF_A),
    ("07AAEFP1428R3ZS", "PRICE WATERHOUSE & CO LLP", _DLF_A),
    ("27AAEFP1428R1ZS", "PRICE WATERHOUSE & CO LLP", _DLF_A),
    ("29AAEFP1428R2ZN", "PRICE WATERHOUSE & CO LLP", _DLF_A),
    ("33AAEFP1428R2ZY", "PRICE WATERHOUSE & CO LLP", _DLF_A),

    ("06AAFFP3698A1ZA", "PRICE WATERHOUSE CHARTERED ACCOUNTANTS LLP", _DLF_A),
    ("27AAFFP3698A1Z6", "PRICE WATERHOUSE CHARTERED ACCOUNTANTS LLP", _DLF_A),
    ("33AAFFP3698A2ZC", "PRICE WATERHOUSE CHARTERED ACCOUNTANTS LLP", _DLF_A),

    ("06AAEFP3641G1ZG", "PRICE WATERHOUSE LLP", _DLF_A),
    ("27AAEFP3641G2ZB", "PRICE WATERHOUSE LLP", _DLF_A),

    ("06AABCP9181H1Z8", "PRICEWATERHOUSECOOPERS PRIVATE LIMITED", _DLF_B),
    ("09AABCP9181H1Z2", "PRICEWATERHOUSECOOPERS PRIVATE LIMITED", _DLF_B),
    ("19AABCP9181H1Z1", "PRICEWATERHOUSECOOPERS PRIVATE LIMITED", _DLF_B),
    ("24AABCP9181H1ZA", "PRICEWATERHOUSECOOPERS PRIVATE LIMITED", _DLF_B),
    ("27AABCP9181H1Z4", "PRICEWATERHOUSECOOPERS PRIVATE LIMITED", _DLF_B),
    ("29AABCP9181H2ZZ", "PRICEWATERHOUSECOOPERS PRIVATE LIMITED", _DLF_B),
    ("33AABCP9181H2ZA", "PRICEWATERHOUSECOOPERS PRIVATE LIMITED", _DLF_B),
    ("36AABCP9181H1Z5", "PRICEWATERHOUSECOOPERS PRIVATE LIMITED", _DLF_B),

    ("06AAVFP1595L1ZF", "PricewaterhouseCoopers Professional Services LLP", _SUCHETA),
    ("07AAVFP1595L1ZD", "PricewaterhouseCoopers Professional Services LLP", _SUCHETA),
    ("09AAVFP1595L1Z9", "PricewaterhouseCoopers Professional Services LLP", _SUCHETA),
    ("19AAVFP1595L1Z8", "PricewaterhouseCoopers Professional Services LLP", _SUCHETA),
    ("27AAVFP1595L1ZB", "PricewaterhouseCoopers Professional Services LLP", _SUCHETA),
    ("29AAVFP1595L1Z7", "PricewaterhouseCoopers Professional Services LLP", _SUCHETA),
    ("36AAVFP1595L1ZC", "PricewaterhouseCoopers Professional Services LLP", _SUCHETA),

    ("06AARFG0505D1ZT", "PricewaterhouseCoopers Services LLP", _DLF_C),
    ("09AARFG0505D1ZN", "PricewaterhouseCoopers Services LLP", _DLF_C),
    ("19AARFG0505D1ZM", "PricewaterhouseCoopers Services LLP", _DLF_C),
    ("27AARFG0505D1ZP", "PricewaterhouseCoopers Services LLP", _DLF_C),
    ("29AARFG0505D1ZL", "PricewaterhouseCoopers Services LLP", _DLF_C),
    ("33AARFG0505D1ZW", "PricewaterhouseCoopers Services LLP", _DLF_C),
    ("36AARFG0505D1ZQ", "PricewaterhouseCoopers Services LLP", _DLF_C),
]

CLIENT_MASTER: dict[str, ClientRecord] = {
    gstin: ClientRecord(entity_name=name, address=addr, gstin=gstin)
    for gstin, name, addr in _RAW
}


class UnknownClientError(ValueError):
    """Raised when a GSTIN is not in CLIENT_MASTER."""


_ZONE_TO_STATE: dict[str, str] = {
    "delhi": "07", "airport": "07",
    "gurgaon": "06", "gurugram": "06", "haryana": "06",
    "noida": "09", "ghaziabad": "09",
    "maharashtra": "27", "mumbai": "27", "pune": "27",
    "bangalore": "29", "bengaluru": "29", "karnataka": "29",
    "chennai": "33", "tamil nadu": "33",
    "kolkata": "19", "west bengal": "19",
    "hyderabad": "36", "telangana": "36",
    "ahmedabad": "24", "gujarat": "24",
    "faridabad": "06",
}

_BY_NAME: dict[str, list[ClientRecord]] = {}
for _rec in CLIENT_MASTER.values():
    _key = _rec.entity_name.lower().strip()
    _BY_NAME.setdefault(_key, []).append(_rec)


def lookup_client_by_name(entity_name: str, drop_zone: str = "") -> ClientRecord:
    """Look up a client by entity name (case-insensitive), using drop_zone to
    pick the correct state GSTIN when the entity has entries for multiple states."""
    key = entity_name.lower().strip()
    records = _BY_NAME.get(key)
    if not records:
        raise UnknownClientError(
            f"Unknown entity: {entity_name!r}. "
            "Add its GSTIN to app/services/eee_taxi_clients.py CLIENT_MASTER."
        )
    if len(records) == 1:
        return records[0]
    state = _ZONE_TO_STATE.get(drop_zone.lower().strip())
    if state:
        for r in records:
            if r.gstin.startswith(state):
                return r
    return records[0]


def lookup_client(gstin: str) -> ClientRecord:
    gstin = gstin.strip().upper()
    if gstin not in CLIENT_MASTER:
        raise UnknownClientError(
            f"Unknown GSTIN: {gstin}. "
            "Add it to app/services/eee_taxi_clients.py CLIENT_MASTER."
        )
    return CLIENT_MASTER[gstin]


def is_local(client_gstin: str) -> bool:
    """True if client is in Delhi (same state as EEE-Taxi) -> Local invoice."""
    return client_gstin.strip()[:2] == EEE_TAXI_STATE_CODE
