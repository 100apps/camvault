from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from xml.etree import ElementTree as ET

import httpx
import pytest
from defusedxml import ElementTree as SafeET

from camvault.config import CameraConfig
from camvault.onvif import (
    SOAP12,
    TRT,
    OnvifClient,
    ProfileInfo,
    build_soap_envelope,
    choose_profile,
    local_name,
    resolve_camera_rtsp,
)


def test_ws_security_password_digest_is_standard_nonce_created_password() -> None:
    nonce = b"fixed-nonce"
    now = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)
    body = ET.Element(ET.QName(TRT, "GetProfiles"))
    payload = build_soap_envelope(
        endpoint="http://camera/onvif/media",
        action=f"{TRT}/GetProfiles",
        body=body,
        username="admin",
        password="p@ss",
        nonce=nonce,
        now=now,
    )
    root = SafeET.fromstring(payload)
    values = {local_name(item.tag): item.text for item in root.iter()}
    expected = base64.b64encode(
        hashlib.sha1(nonce + b"2026-09-04T12:00:00Z" + b"p@ss").digest()
    ).decode("ascii")
    assert values["Password"] == expected
    assert values["Nonce"] == base64.b64encode(nonce).decode("ascii")
    assert values["Created"] == "2026-09-04T12:00:00Z"


def _soap(body: str) -> bytes:
    return (
        f"<s:Envelope xmlns:s='{SOAP12}' xmlns:tds='http://www.onvif.org/ver10/device/wsdl' "
        f"xmlns:trt='{TRT}' xmlns:tt='http://www.onvif.org/ver10/schema'>"
        f"<s:Body>{body}</s:Body></s:Envelope>"
    ).encode()


@pytest.mark.asyncio
async def test_media1_resolution_and_wildcard_endpoint_repair() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        root = SafeET.fromstring(await request.aread())
        operation = next(
            local_name(item.tag)
            for item in root.iter()
            if local_name(item.tag)
            in {"GetServices", "GetCapabilities", "GetProfiles", "GetStreamUri"}
        )
        if operation == "GetServices":
            body = f"""
<tds:GetServicesResponse><tds:Service><tds:Namespace>{TRT}</tds:Namespace>
<tds:XAddr>http://0.0.0.0/onvif/media_service</tds:XAddr></tds:Service></tds:GetServicesResponse>"""
        elif operation == "GetCapabilities":
            body = "<tds:GetCapabilitiesResponse/>"
        elif operation == "GetProfiles":
            body = """
<trt:GetProfilesResponse>
 <trt:Profiles token='sub'><tt:Name>substream</tt:Name><tt:VideoEncoderConfiguration>
  <tt:Resolution><tt:Width>640</tt:Width><tt:Height>360</tt:Height></tt:Resolution>
 </tt:VideoEncoderConfiguration></trt:Profiles>
 <trt:Profiles token='main'><tt:Name>main</tt:Name><tt:VideoEncoderConfiguration>
  <tt:Resolution><tt:Width>1920</tt:Width><tt:Height>1080</tt:Height></tt:Resolution>
 </tt:VideoEncoderConfiguration></trt:Profiles>
</trt:GetProfilesResponse>"""
        else:
            body = "<trt:GetStreamUriResponse><trt:MediaUri><tt:Uri>rtsp://0.0.0.0:554/live/main</tt:Uri></trt:MediaUri></trt:GetStreamUriResponse>"
        return httpx.Response(
            200, content=_soap(body), headers={"content-type": "application/soap+xml"}
        )

    camera = CameraConfig(
        id="front",
        host="192.168.1.20",
        username="a@b",
        password="p/x",
    )
    uri, profile = await resolve_camera_rtsp(camera, transport=httpx.MockTransport(handler))
    assert profile is not None and profile.token == "main"
    assert uri == "rtsp://a%40b:p%2Fx@192.168.1.20:554/live/main"


def test_profile_selection_modes() -> None:
    profiles = [
        ProfileInfo("low", "Sub", 640, 360),
        ProfileInfo("high", "Main Stream", 1920, 1080),
    ]
    assert (
        choose_profile(
            profiles, CameraConfig(id="a", rtsp_url="rtsp://x", profile_name="main")
        ).token
        == "high"
    )
    assert (
        choose_profile(profiles, CameraConfig(id="b", rtsp_url="rtsp://x", profile_index=0)).token
        == "low"
    )
    assert choose_profile(profiles, CameraConfig(id="c", rtsp_url="rtsp://x")).token == "high"


@pytest.mark.asyncio
async def test_media2_get_profiles_requests_all_configurations() -> None:
    observed_type: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        root = SafeET.fromstring(await request.aread())
        operation = next(
            local_name(item.tag) for item in root.iter() if local_name(item.tag) in {"GetProfiles"}
        )
        assert operation == "GetProfiles"
        observed_type.append(
            next((item.text for item in root.iter() if local_name(item.tag) == "Type"), None)
        )
        body = """
<tr2:GetProfilesResponse xmlns:tr2='http://www.onvif.org/ver20/media/wsdl' xmlns:tt='http://www.onvif.org/ver10/schema'>
 <tr2:Profiles token='p1'><tr2:Name>Main</tr2:Name><tr2:Configurations>
  <tr2:VideoEncoder token='v1'><tt:Resolution><tt:Width>1280</tt:Width><tt:Height>720</tt:Height></tt:Resolution></tr2:VideoEncoder>
 </tr2:Configurations></tr2:Profiles>
</tr2:GetProfilesResponse>"""
        return httpx.Response(200, content=_soap(body))

    client = OnvifClient(
        device_service_url="http://192.168.1.20/onvif/device_service",
        transport=httpx.MockTransport(handler),
    )
    profiles = await client.get_profiles("http://192.168.1.20/onvif/media2", media2=True)
    assert observed_type == ["All"]
    assert profiles[0] == ProfileInfo(token="p1", name="Main", width=1280, height=720)
