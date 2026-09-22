from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests


logger = logging.getLogger(__name__)


class ServiceNowError(RuntimeError):
    pass


class ServiceNowOutcomeUnknown(ServiceNowError):
    """The order may exist remotely; automatic resubmission is unsafe."""


@dataclass(frozen=True)
class ServiceNowRequest:
    number: str
    sys_id: str
    url: str
    status: str = "submitted"


DEFAULT_VARIABLE_MAP = {
    "short_description": "short_description",
    "requested_for": "requested_for",
    "customer": "customer",
    "environment_name": "environment_name",
    "environment_type": "environment_type",
    "setup_type": "setup_type",
    "production_classification": "production_classification",
    "aws_region": "aws_region",
    "requested_network_size": "requested_network_size",
    "dns_domain": "dns_domain",
    "target_date": "target_date",
    "requester": "requester",
    "architect": "architect",
    "devops_receiver": "devops_receiver",
    "jira_epic": "jira_epic",
    "praxis_draft": "draft_url",
    "rancher_target": "rancher_target",
    "internet_access_requirement": "internet_access_requirement",
    "account_purpose": "account_purpose",
    "required_access": "required_access",
    "technical_profile": "technical_profile",
    "connectivity_manifest": "connectivity_manifest",
    "business_justification": "business_justification",
}


class ServiceNowClient:
    """Small Service Catalog client; credentials and catalog mappings stay outside drafts."""

    def __init__(self) -> None:
        self.instance = (os.getenv("SERVICENOW_INSTANCE_URL") or "").rstrip("/")
        self.portal_url = os.getenv("SERVICENOW_PORTAL_URL") or self.instance
        self.client_id = os.getenv("SERVICENOW_CLIENT_ID", "")
        self.client_secret = os.getenv("SERVICENOW_CLIENT_SECRET", "")
        self.token_url = os.getenv("SERVICENOW_TOKEN_URL") or (
            f"{self.instance}/oauth_token.do" if self.instance else ""
        )
        self.timeout = float(os.getenv("SERVICENOW_TIMEOUT_SECONDS", "15"))

    def catalog_item_id(self, request_type: str) -> str:
        env_name = {
            "aws": "SERVICENOW_AWS_CATALOG_ITEM_ID",
            "ncr": "SERVICENOW_NCR_CATALOG_ITEM_ID",
        }.get(request_type)
        return os.getenv(env_name, "") if env_name else ""

    def configured(self, request_type: str) -> bool:
        return bool(
            self.instance
            and self.client_id
            and self.client_secret
            and self.catalog_item_id(request_type)
        )

    def configuration(self) -> dict[str, Any]:
        return {
            "instance": self.instance,
            "portal_url": self.portal_url,
            "oauth": bool(self.client_id and self.client_secret),
            "aws": self.configured("aws"),
            "ncr": self.configured("ncr"),
        }

    def _token(self) -> str:
        if not self.instance or not self.client_id or not self.client_secret:
            raise ServiceNowError("ServiceNow OAuth is not configured in Praxis.")
        try:
            response = requests.post(
                self.token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
                headers={"Accept": "application/json"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            body = response.json()
            token = str(body.get("access_token") or "") if isinstance(body, dict) else ""
        except (requests.RequestException, ValueError) as exc:
            logger.warning("ServiceNow OAuth failed", exc_info=True)
            raise ServiceNowError("ServiceNow OAuth failed. Check the integration configuration and try again.") from exc
        if not token:
            raise ServiceNowError("ServiceNow OAuth returned no access token.")
        return token

    def _variable_map(self, request_type: str) -> dict[str, str]:
        raw = os.getenv(f"SERVICENOW_{request_type.upper()}_VARIABLE_MAP", "")
        if not raw:
            return DEFAULT_VARIABLE_MAP
        try:
            mapping = json.loads(raw)
        except ValueError as exc:
            raise ServiceNowError(f"Invalid ServiceNow {request_type.upper()} variable map JSON.") from exc
        if not isinstance(mapping, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in mapping.items()
        ):
            raise ServiceNowError("ServiceNow variable maps must be JSON objects of catalog variable to Praxis field.")
        return {**DEFAULT_VARIABLE_MAP, **mapping}

    def build_variables(self, request_type: str, data: dict[str, Any], *, draft_url: str,
                        overrides: dict[str, Any] | None = None) -> dict[str, str]:
        """Validate the complete reviewed payload before acquiring a token or ordering.

        request_content is a LOCAL field. Its remote catalog name must be explicitly
        configured. Split mode requires every reviewed field to have a mapping.
        """
        # NCR catalog variables must reflect the reviewed fields only. Otherwise
        # hidden draft metadata and duplicate allocation fields leak back into SNOW.
        source = (dict(overrides) if request_type == 'ncr' and overrides is not None
                  else {**data, 'draft_url': draft_url, **(overrides or {})})
        if 'requested_for' in source:
            source['requester'] = source['requested_for']
        mapping = self._variable_map(request_type)
        variables = {remote: (str(source.get(local) or '') if local == 'request_content'
                              else str(source.get(local) or '').strip())
                     for remote, local in mapping.items()}
        variables = {key: value for key, value in variables.items() if value}
        try:
            limits = json.loads(os.getenv(f'SERVICENOW_{request_type.upper()}_FIELD_LIMITS') or '{}')
        except ValueError as exc:
            raise ServiceNowError('ServiceNow field limits must be valid JSON.') from exc
        if not isinstance(limits, dict) or not all(isinstance(k, str) and type(v) is int and v > 0 for k, v in limits.items()):
            raise ServiceNowError('ServiceNow field limits must map catalog variable names to positive character limits.')
        if request_type == 'ncr':
            mode = os.getenv('SERVICENOW_NCR_CONTENT_MODE', 'complete')
            if mode not in {'complete', 'fields'}:
                raise ServiceNowError('SERVICENOW_NCR_CONTENT_MODE must be complete or fields.')
            required = {'request_content'} if mode == 'complete' else set(overrides or {}) - {'request_content'}
            if not required or any(not str(source.get(key) or '').strip() for key in required):
                raise ServiceNowError('Complete reviewed networking content is required before submission.')
            missing = required - set(mapping.values())
            if missing:
                raise ServiceNowError('Confirm the real NCR catalog variable names in SERVICENOW_NCR_VARIABLE_MAP for: ' + ', '.join(sorted(missing)) + '. No request was submitted.')
            unknown_limits = set(variables) - set(limits)
            if unknown_limits:
                raise ServiceNowError('Confirm catalog character limits in SERVICENOW_NCR_FIELD_LIMITS for: ' + ', '.join(sorted(unknown_limits)) + '.')
        for remote, value in variables.items():
            if remote in limits and len(value) > limits[remote]:
                raise ServiceNowError(f'Rendered field {mapping[remote]} (catalog variable {remote}) has {len(value)} characters; supported limit is {limits[remote]}. Shorten or split the content; nothing was truncated or submitted.')
        return variables

    def create_catalog_request(
        self, request_type: str, data: dict[str, Any], *, draft_url: str,
        overrides: dict[str, Any] | None = None,
    ) -> ServiceNowRequest:
        item_id = self.catalog_item_id(request_type)
        if not item_id:
            raise ServiceNowError(f"The ServiceNow {request_type.upper()} catalog item is not configured.")
        variables = self.build_variables(request_type, data, draft_url=draft_url, overrides=overrides)
        token = self._token()
        try:
            response = requests.post(
                f"{self.instance}/api/sn_sc/servicecatalog/items/{quote(item_id, safe='')}/order_now",
                json={"sysparm_quantity": 1, "variables": variables},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            body = response.json()
            result = body.get("result") if isinstance(body, dict) else None
        except (requests.RequestException, ValueError) as exc:
            logger.warning("ServiceNow request creation failed", exc_info=True)
            raise ServiceNowOutcomeUnknown(
                "ServiceNow submission outcome is unknown. Check ServiceNow and attach the existing "
                "request before attempting any further submission."
            ) from exc
        if not isinstance(result, dict):
            raise ServiceNowOutcomeUnknown("ServiceNow returned an unrecognized result. Check the portal for the request.")
        number = str(result.get("request_number") or result.get("number") or "").strip()
        sys_id = str(result.get("request_id") or result.get("sys_id") or "").strip()
        if not number:
            raise ServiceNowOutcomeUnknown("ServiceNow returned no request number. Check the portal and attach the existing request.")
        record_url = (
            f"{self.instance}/nav_to.do?uri=sc_request.do%3Fsys_id%3D{quote(sys_id, safe='')}"
            if sys_id
            else self.instance
        )
        return ServiceNowRequest(number=number, sys_id=sys_id, url=record_url)
