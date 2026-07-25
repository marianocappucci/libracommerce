from dataclasses import dataclass
from enum import StrEnum


class PartyType(StrEnum):
    PERSON = "person"
    ORGANIZATION = "organization"


class PartyRole(StrEnum):
    CUSTOMER = "customer"
    SUPPLIER = "supplier"
    EMPLOYEE = "employee"
    PROFESSIONAL = "professional"


@dataclass(frozen=True)
class Party:
    id: int | None
    party_type: PartyType
    display_name: str
    legal_name: str | None = None
    tax_id: str | None = None
    tax_id_type: str | None = None
    email: str | None = None
    phone: str | None = None
    active: bool = True
