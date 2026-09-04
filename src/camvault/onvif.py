from __future__ import annotations

import base64
import hashlib
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin, urlsplit
from uuid import uuid4
from xml.etree import ElementTree as ET

import httpx
from defusedxml import ElementTree as SafeET

from camvault.config import CameraConfig
from camvault.security import inject_url_credentials, repair_rtsp_host, repair_url_host

logger = logging.getLogger(__name__)

SOAP12 = "http://www.w3.org/2003/05/soap-envelope"
WSA = "http://www.w3.org/2005/08/addressing"
WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
PASSWORD_DIGEST = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
)
BASE64_BINARY = (
    "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary"
)
TDS = "http://www.onvif.org/ver10/device/wsdl"
TRT = "http://www.onvif.org/ver10/media/wsdl"
TR2 = "http://www.onvif.org/ver20/media/wsdl"
TT = "http://www.onvif.org/ver10/schema"


class OnvifError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ServiceEndpoints:
    media1: str | None = None
    media2: str | None = None


@dataclass(frozen=True, slots=True)
class ProfileInfo:
    token: str
    name: str
    width: int | None = None
    height: int | None = None

    @property
    def pixels(self) -> int:
        return (self.width or 0) * (self.height or 0)


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def first_descendant_text(element: ET.Element, name: str) -> str | None:
    for child in element.iter():
        if local_name(child.tag) == name and child.text:
            value = child.text.strip()
            if value:
                return value
    return None


def _password_digest(nonce: bytes, created: str, password: str) -> str:
    raw = nonce + created.encode("utf-8") + password.encode("utf-8")
    return base64.b64encode(hashlib.sha1(raw).digest()).decode("ascii")


def build_soap_envelope(
    *,
    endpoint: str,
    action: str,
    body: ET.Element,
    username: str | None,
    password: str | None,
    clock_offset_seconds: int = 0,
    nonce: bytes | None = None,
    now: datetime | None = None,
) -> bytes:
    envelope = ET.Element(ET.QName(SOAP12, "Envelope"))
    header = ET.SubElement(envelope, ET.QName(SOAP12, "Header"))
    ET.SubElement(header, ET.QName(WSA, "Action")).text = action
    ET.SubElement(header, ET.QName(WSA, "MessageID")).text = f"urn:uuid:{uuid4()}"
    ET.SubElement(header, ET.QName(WSA, "To")).text = endpoint

    if username:
        nonce = nonce or os.urandom(20)
        current = now or datetime.now(UTC)
        current += timedelta(seconds=clock_offset_seconds)
        created = current.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

        security = ET.SubElement(header, ET.QName(WSSE, "Security"))
        token = ET.SubElement(security, ET.QName(WSSE, "UsernameToken"))
        token.set(ET.QName(WSU, "Id"), f"UsernameToken-{uuid4()}")
        ET.SubElement(token, ET.QName(WSSE, "Username")).text = username
        password_element = ET.SubElement(token, ET.QName(WSSE, "Password"))
        password_element.set("Type", PASSWORD_DIGEST)
        password_element.text = _password_digest(nonce, created, password or "")
        nonce_element = ET.SubElement(token, ET.QName(WSSE, "Nonce"))
        nonce_element.set("EncodingType", BASE64_BINARY)
        nonce_element.text = base64.b64encode(nonce).decode("ascii")
        ET.SubElement(token, ET.QName(WSU, "Created")).text = created

    envelope_body = ET.SubElement(envelope, ET.QName(SOAP12, "Body"))
    envelope_body.append(body)
    return ET.tostring(envelope, encoding="utf-8", xml_declaration=True)


class OnvifClient:
    def __init__(
        self,
        *,
        device_service_url: str,
        username: str | None = None,
        password: str | None = None,
        verify_tls: bool = False,
        timeout_seconds: float = 10.0,
        clock_offset_seconds: int = 0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.device_service_url = device_service_url
        self.username = username
        self.password = password
        self.verify_tls = verify_tls
        self.timeout_seconds = timeout_seconds
        self.clock_offset_seconds = clock_offset_seconds
        self.transport = transport

    async def _soap(self, endpoint: str, action: str, body: ET.Element) -> ET.Element:
        auth_attempts: list[httpx.Auth | None] = [None]
        if self.username:
            auth_attempts.extend(
                [
                    httpx.DigestAuth(self.username, self.password or ""),
                    httpx.BasicAuth(self.username, self.password or ""),
                ]
            )

        last_response: httpx.Response | None = None
        for auth in auth_attempts:
            payload = build_soap_envelope(
                endpoint=endpoint,
                action=action,
                body=body,
                username=self.username,
                password=self.password,
                clock_offset_seconds=self.clock_offset_seconds,
            )
            headers = {
                "Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"',
                "SOAPAction": f'"{action}"',
                "User-Agent": "CamVault/0.1",
            }
            async with httpx.AsyncClient(
                verify=self.verify_tls,
                timeout=httpx.Timeout(self.timeout_seconds),
                follow_redirects=False,
                transport=self.transport,
                auth=auth,
            ) as client:
                try:
                    response = await client.post(endpoint, content=payload, headers=headers)
                except httpx.HTTPError as exc:
                    raise OnvifError(f"ONVIF request failed: {exc}") from exc
            last_response = response
            if response.status_code == 401:
                continue
            if response.status_code >= 400:
                raise OnvifError(f"ONVIF HTTP error {response.status_code}")
            try:
                root = SafeET.fromstring(response.content)
            except Exception as exc:  # defusedxml raises several safe parser exceptions
                raise OnvifError("camera returned invalid ONVIF XML") from exc
            self._raise_for_fault(root)
            return root

        status = last_response.status_code if last_response is not None else "unknown"
        raise OnvifError(f"camera rejected ONVIF credentials (HTTP {status})")

    @staticmethod
    def _raise_for_fault(root: ET.Element) -> None:
        for element in root.iter():
            if local_name(element.tag) == "Fault":
                reason = first_descendant_text(element, "Text") or first_descendant_text(
                    element, "Reason"
                )
                raise OnvifError(f"ONVIF SOAP fault: {reason or 'unspecified fault'}")

    async def get_service_endpoints(self) -> ServiceEndpoints:
        media1: str | None = None
        media2: str | None = None

        get_services = ET.Element(ET.QName(TDS, "GetServices"))
        ET.SubElement(get_services, ET.QName(TDS, "IncludeCapability")).text = "false"
        try:
            root = await self._soap(
                self.device_service_url,
                f"{TDS}/GetServices",
                get_services,
            )
            media1, media2 = self._parse_services(root)
        except OnvifError as exc:
            logger.debug("GetServices failed, trying GetCapabilities: %s", exc)

        if not media1 or not media2:
            get_capabilities = ET.Element(ET.QName(TDS, "GetCapabilities"))
            ET.SubElement(get_capabilities, ET.QName(TDS, "Category")).text = "All"
            try:
                root = await self._soap(
                    self.device_service_url,
                    f"{TDS}/GetCapabilities",
                    get_capabilities,
                )
                cap_media1, cap_media2 = self._parse_capabilities(root)
                media1 = media1 or cap_media1
                media2 = media2 or cap_media2
            except OnvifError:
                if not media1 and not media2:
                    raise

        if not media1 and not media2:
            raise OnvifError("camera did not advertise an ONVIF Media or Media2 service")
        configured_host = urlsplit(self.device_service_url).hostname

        def normalize(endpoint: str | None) -> str | None:
            if not endpoint:
                return None
            if not urlsplit(endpoint).scheme:
                endpoint = urljoin(self.device_service_url, endpoint)
            return repair_url_host(endpoint, configured_host)

        return ServiceEndpoints(media1=normalize(media1), media2=normalize(media2))

    @staticmethod
    def _parse_services(root: ET.Element) -> tuple[str | None, str | None]:
        media1 = None
        media2 = None
        for service in root.iter():
            if local_name(service.tag) != "Service":
                continue
            namespace = first_descendant_text(service, "Namespace") or ""
            xaddr = first_descendant_text(service, "XAddr")
            if not xaddr:
                continue
            if namespace.rstrip("/") == TRT.rstrip("/"):
                media1 = xaddr
            elif namespace.rstrip("/") == TR2.rstrip("/"):
                media2 = xaddr
        return media1, media2

    @staticmethod
    def _parse_capabilities(root: ET.Element) -> tuple[str | None, str | None]:
        media1 = None
        media2 = None
        for element in root.iter():
            name = local_name(element.tag)
            if name not in {"Media", "Media2"}:
                continue
            xaddr = element.attrib.get("XAddr") or first_descendant_text(element, "XAddr")
            if not xaddr:
                continue
            if name == "Media2":
                media2 = xaddr
            elif name == "Media":
                media1 = xaddr
        return media1, media2

    async def get_profiles(self, endpoint: str, *, media2: bool) -> list[ProfileInfo]:
        namespace = TR2 if media2 else TRT
        body = ET.Element(ET.QName(namespace, "GetProfiles"))
        if media2:
            # Media2 defines a configuration-type filter; requesting All is the portable
            # way to receive complete profiles when no token is specified.
            ET.SubElement(body, ET.QName(TR2, "Type")).text = "All"
        root = await self._soap(endpoint, f"{namespace}/GetProfiles", body)
        profiles: list[ProfileInfo] = []
        seen: set[str] = set()
        for element in root.iter():
            if local_name(element.tag) not in {"Profiles", "Profile"}:
                continue
            token = element.attrib.get("token") or element.attrib.get("Token")
            if not token or token in seen:
                continue
            seen.add(token)
            name = first_descendant_text(element, "Name") or token
            width, height = self._find_largest_resolution(element)
            profiles.append(ProfileInfo(token=token, name=name, width=width, height=height))
        if not profiles:
            raise OnvifError("camera returned no ONVIF media profiles")
        return profiles

    @staticmethod
    def _find_largest_resolution(element: ET.Element) -> tuple[int | None, int | None]:
        candidates: list[tuple[int, int]] = []
        for child in element.iter():
            if local_name(child.tag) != "Resolution":
                continue
            width_text = first_descendant_text(child, "Width")
            height_text = first_descendant_text(child, "Height")
            try:
                if width_text and height_text:
                    candidates.append((int(width_text), int(height_text)))
            except ValueError:
                continue
        if not candidates:
            return None, None
        return max(candidates, key=lambda pair: pair[0] * pair[1])

    async def get_stream_uri(self, endpoint: str, token: str, *, media2: bool) -> str:
        if media2:
            body = ET.Element(ET.QName(TR2, "GetStreamUri"))
            ET.SubElement(body, ET.QName(TR2, "Protocol")).text = "RtspUnicast"
            ET.SubElement(body, ET.QName(TR2, "ProfileToken")).text = token
            action = f"{TR2}/GetStreamUri"
        else:
            body = ET.Element(ET.QName(TRT, "GetStreamUri"))
            setup = ET.SubElement(body, ET.QName(TRT, "StreamSetup"))
            ET.SubElement(setup, ET.QName(TT, "Stream")).text = "RTP-Unicast"
            transport = ET.SubElement(setup, ET.QName(TT, "Transport"))
            ET.SubElement(transport, ET.QName(TT, "Protocol")).text = "RTSP"
            ET.SubElement(body, ET.QName(TRT, "ProfileToken")).text = token
            action = f"{TRT}/GetStreamUri"

        root = await self._soap(endpoint, action, body)
        uri = first_descendant_text(root, "Uri")
        if not uri:
            raise OnvifError("camera did not return an RTSP URI")
        if not urlsplit(uri).scheme:
            uri = urljoin(endpoint, uri)
        if urlsplit(uri).scheme.lower() not in {"rtsp", "rtsps"}:
            raise OnvifError(f"camera returned a non-RTSP stream URI ({urlsplit(uri).scheme})")
        return uri


def choose_profile(profiles: Iterable[ProfileInfo], camera: CameraConfig) -> ProfileInfo:
    items = list(profiles)
    if not items:
        raise OnvifError("no media profiles available")

    if camera.profile_token:
        for profile in items:
            if profile.token == camera.profile_token:
                return profile
        raise OnvifError(f"profile token {camera.profile_token!r} was not found")

    if camera.profile_name:
        wanted = camera.profile_name.casefold()
        for profile in items:
            if profile.name.casefold() == wanted:
                return profile
        for profile in items:
            if wanted in profile.name.casefold():
                return profile
        available = ", ".join(profile.name for profile in items)
        raise OnvifError(
            f"profile name {camera.profile_name!r} was not found; available: {available}"
        )

    if camera.profile_index is not None:
        try:
            return items[camera.profile_index]
        except IndexError as exc:
            raise OnvifError(
                f"profile_index {camera.profile_index} is outside 0..{len(items) - 1}"
            ) from exc

    # Main streams are usually the highest-resolution profile. This is less vendor-specific
    # than assuming profile 0, while still falling back to the first profile if dimensions
    # are not advertised.
    return max(items, key=lambda profile: profile.pixels)


async def resolve_camera_rtsp(
    camera: CameraConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[str, ProfileInfo | None]:
    username = camera.resolved_username()
    password = camera.resolved_password()
    direct_url = camera.resolved_direct_rtsp_url()
    if direct_url:
        direct_url = repair_rtsp_host(direct_url, camera.host)
        return inject_url_credentials(direct_url, username, password), None

    client = OnvifClient(
        device_service_url=camera.device_service_url(),
        username=username,
        password=password,
        verify_tls=camera.verify_tls,
        clock_offset_seconds=camera.onvif_clock_offset_seconds,
        transport=transport,
    )
    endpoints = await client.get_service_endpoints()
    errors: list[str] = []

    # Media2 is preferred when advertised; Media1 is retained as a broad compatibility
    # fallback for Profile S cameras and older firmware.
    candidates = [(endpoints.media2, True), (endpoints.media1, False)]
    for endpoint, media2 in candidates:
        if not endpoint:
            continue
        try:
            profiles = await client.get_profiles(endpoint, media2=media2)
            profile = choose_profile(profiles, camera)
            uri = await client.get_stream_uri(endpoint, profile.token, media2=media2)
            uri = repair_rtsp_host(uri, camera.host)
            uri = inject_url_credentials(uri, username, password)
            return uri, profile
        except OnvifError as exc:
            errors.append(f"{'Media2' if media2 else 'Media'}: {exc}")

    raise OnvifError("unable to resolve an RTSP stream; " + "; ".join(errors))
