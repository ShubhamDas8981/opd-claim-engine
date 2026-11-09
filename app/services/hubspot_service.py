"""
Put this file at app/services/hubspot_service.py
Talks to HubSpot with the private-app token from .env (HUBSPOT_TOKEN).
All functions are synchronous: call them with asyncio.to_thread from FastAPI.
"""
import httpx
from app.core.config import settings

HS_API = "https://api.hubapi.com"


class HubSpotError(Exception):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def hs_request(method: str, path: str, **kwargs) -> httpx.Response:
    try:
        resp = httpx.request(
            method,
            f"{HS_API}{path}",
            headers={"Authorization": f"Bearer {settings.HUBSPOT_TOKEN}"},
            timeout=20,
            **kwargs,
        )
    except httpx.RequestError as e:
        raise HubSpotError(f"Network error: {type(e).__name__}", retryable=True) from e

    if resp.status_code == 429 or resp.status_code >= 500:
        raise HubSpotError(f"HubSpot {resp.status_code}: {resp.text[:300]}", retryable=True)
    if resp.status_code >= 400:
        raise HubSpotError(f"HubSpot {resp.status_code}: {resp.text[:300]}", retryable=False)
    return resp


def _search_contact(email: str):
    resp = hs_request(
        "POST",
        "/crm/v3/objects/contacts/search",
        json={
            "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}],
            "limit": 1,
        },
    )
    results = resp.json().get("results", [])
    return results[0]["id"] if results else None


def find_or_create_contact(email: str, name: str | None, phone: str | None) -> str:
    existing = _search_contact(email)
    if existing:
        return existing

    props = {"email": email}
    if name:
        first, _, last = name.partition(" ")
        props["firstname"] = first
        if last:
            props["lastname"] = last
    if phone:
        props["phone"] = phone

    try:
        return hs_request("POST", "/crm/v3/objects/contacts", json={"properties": props}).json()["id"]
    except HubSpotError as e:
        # two requests created the same contact at the same moment
        if "409" in str(e):
            again = _search_contact(email)
            if again:
                return again
        raise


def create_ticket(properties: dict) -> str:
    return hs_request("POST", "/crm/v3/objects/tickets", json={"properties": properties}).json()["id"]


def associate_ticket_with_contact(ticket_id: str, contact_id: str) -> None:
    hs_request("PUT", f"/crm/v4/objects/tickets/{ticket_id}/associations/default/contacts/{contact_id}")


def ticket_url(ticket_id: str) -> str:
    return f"https://{settings.HUBSPOT_APP_HOST}/contacts/{settings.HUBSPOT_PORTAL_ID}/record/0-5/{ticket_id}"
