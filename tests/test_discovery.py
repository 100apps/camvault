from __future__ import annotations

from camvault.discovery import build_probe_message, parse_probe_response


def test_probe_and_response_parser() -> None:
    assert b"NetworkVideoTransmitter" in build_probe_message()
    payload = b"""<?xml version='1.0'?>
<s:Envelope xmlns:s='http://www.w3.org/2003/05/soap-envelope'
 xmlns:d='http://schemas.xmlsoap.org/ws/2005/04/discovery'>
 <s:Body><d:ProbeMatches><d:ProbeMatch>
  <d:Scopes>onvif://www.onvif.org/type/video_encoder room/front</d:Scopes>
  <d:XAddrs>http://192.168.1.20/onvif/device_service</d:XAddrs>
 </d:ProbeMatch></d:ProbeMatches></s:Body>
</s:Envelope>"""
    devices = parse_probe_response(payload)
    assert len(devices) == 1
    assert devices[0].primary_xaddr == "http://192.168.1.20/onvif/device_service"
    assert "room/front" in devices[0].scopes
