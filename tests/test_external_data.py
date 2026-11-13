"""Offline regression cases for public OGC imports. No real network or credentials."""

from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest
from shapely.geometry import shape

from signaleyes_mcp import external_data as core

BASE = "https://data.example.test"
AREA = "POLYGON ((4 52, 5 52, 5 53, 4 53, 4 52))"


def feature(x=4.5, y=52.5, feature_id="one"):
    return {
        "type": "Feature",
        "id": feature_id,
        "geometry": {"type": "Point", "coordinates": [x, y]},
        "properties": {"name": "Example"},
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://public.example",
        "https://u:p@public.example",
        "https://public.example:8443",
        "file:///etc/passwd",
        "https://public.example/#fragment",
        "https://public.example/\nheader",
    ],
)
def test_rejects_unsafe_urls(url):
    with pytest.raises(core.ExternalDataError):
        core.public_url(url)


def test_rejects_signed_source_links_and_cross_origin_paging():
    with pytest.raises(core.ExternalDataError):
        core.source_url(BASE + "/?api_key=not-a-real-key")
    with pytest.raises(core.ExternalDataError):
        core.same_origin(BASE, "https://different.example/items")
    assert core.same_origin(BASE + "/items?page=1", "?page=2") == BASE + "/items?page=2"


@pytest.mark.parametrize(
    "addresses",
    [
        ["127.0.0.1"],
        ["::1"],
        ["169.254.169.254"],
        ["10.0.0.1"],
        ["224.0.0.1"],
        ["8.8.8.8", "192.168.1.1"],
    ],
)
def test_dns_checks_every_address(monkeypatch, addresses):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, (address, 443)) for address in addresses],
    )
    with pytest.raises(core.ExternalDataError):
        core._public_addresses("public.example")


def test_rejects_xml_entities():
    with pytest.raises(core.ExternalDataError, match="DTD"):
        core._xml(b'<!DOCTYPE x [<!ENTITY e SYSTEM "file:///not-real">]><x>&e;</x>')


@pytest.mark.parametrize("status, body", [(302, b""), (200, b"too many bytes")])
def test_redirect_and_byte_limit_are_enforced(monkeypatch, status, body):
    class Connection:
        sock = None

        def __init__(self, *args):
            self.body = body

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return self

        def getheaders(self):
            return []

        def read(self, count):
            chunk, self.body = self.body[:count], self.body[count:]
            return chunk

        def close(self):
            pass

    Connection.status = status
    monkeypatch.setattr(core, "_public_addresses", lambda host: ["8.8.8.8"])
    monkeypatch.setattr(core, "_PinnedHTTPSConnection", Connection)
    reader = core.PublicReader()
    reader.remaining = 2
    with pytest.raises(core.ExternalDataError):
        reader.get(BASE)


def fake_source(monkeypatch, pages):
    calls = []

    class Reader:
        def json(self, url):
            calls.append(url)
            if "/items" not in url:
                return {"id": "zones"}
            return pages.pop(0)

    monkeypatch.setattr(core, "PublicReader", Reader)
    monkeypatch.setattr(
        core, "inspect_source", lambda *args: {"url": BASE, "protocol": "ogc_features"}
    )
    return calls


def test_collects_all_pages_clips_and_preserves_provenance(monkeypatch):
    polygon = feature(feature_id="polygon")
    polygon["geometry"] = {
        "type": "Polygon",
        "coordinates": [[[3.5, 52.2], [4.5, 52.2], [4.5, 52.8], [3.5, 52.8], [3.5, 52.2]]],
    }
    calls = fake_source(
        monkeypatch,
        [
            {
                "type": "FeatureCollection",
                "features": [polygon],
                "numberMatched": 3,
                "links": [{"rel": "next", "href": "?page=2"}],
            },
            {
                "type": "FeatureCollection",
                "features": [feature(), feature(9, 52)],
                "numberMatched": 3,
            },
        ],
    )
    result = core.download_source(BASE, "zones", AREA)
    assert result["complete"] and result["downloaded_count"] == 3 and result["feature_count"] == 2
    assert shape(result["geojson"]["features"][0]["geometry"]).bounds[0] == 4
    assert result["geojson"]["features"][0]["properties"]["_external_source"] == BASE
    assert calls[-1].endswith("?page=2")


@pytest.mark.parametrize(
    "page",
    [
        {"type": "FeatureCollection", "features": [feature()], "numberMatched": 2},
        {
            "type": "FeatureCollection",
            "features": [feature()],
            "links": [{"rel": "next", "href": "https://elsewhere.example/page"}],
        },
        {
            "type": "FeatureCollection",
            "features": [feature()],
            "crs": {"properties": {"name": "EPSG:28992"}},
        },
    ],
)
def test_refuses_incomplete_or_wrong_crs_data(monkeypatch, page):
    fake_source(monkeypatch, [page])
    with pytest.raises(core.ExternalDataError):
        core.download_source(BASE, "zones", AREA)


def test_limit_never_returns_a_partial_dataset(monkeypatch):
    monkeypatch.setattr(core, "MAX_FEATURES", 1)
    fake_source(
        monkeypatch,
        [{"type": "FeatureCollection", "features": [feature(), feature(feature_id="two")]}],
    )
    with pytest.raises(core.ExternalDataError, match="features"):
        core.download_source(BASE, "zones", AREA)


def test_detects_repeated_pages(monkeypatch):
    page = {
        "type": "FeatureCollection",
        "features": [feature()],
        "links": [{"rel": "next", "href": "?page=2"}],
    }
    fake_source(monkeypatch, [page, page])
    with pytest.raises(core.ExternalDataError, match="repeats"):
        core.download_source(BASE, "zones", AREA)


def test_wfs_catalog_requires_geojson_and_version_two():
    xml = b"""<wfs:WFS_Capabilities xmlns:wfs="http://www.opengis.net/wfs/2.0"
      xmlns:ows="http://www.opengis.net/ows/1.1" version="2.0.0">
      <ows:OperationsMetadata><ows:Operation name="GetFeature"><ows:Parameter name="outputFormat">
      <ows:AllowedValues><ows:Value>application/json</ows:Value></ows:AllowedValues>
      </ows:Parameter></ows:Operation></ows:OperationsMetadata>
      <wfs:FeatureTypeList><wfs:FeatureType><wfs:Name>demo:zones</wfs:Name>
      <wfs:Title>Zones</wfs:Title></wfs:FeatureType></wfs:FeatureTypeList></wfs:WFS_Capabilities>"""
    reader = SimpleNamespace(get=lambda url: (xml, {}))
    catalog = core._wfs_catalog(reader, BASE)
    assert catalog["layers"][0]["id"] == "demo:zones"
    assert catalog["output_format"] == "application/json"
    reader.get = lambda url: (xml.replace(b"application/json", b"application/gml+xml"), {})
    with pytest.raises(core.ExternalDataError, match="GeoJSON"):
        core._wfs_catalog(reader, BASE)


def test_pdok_epsg4326_bounds_are_latitude_first():
    crs, bbox = core._wfs_spatial_params(
        {"crs": ["urn:ogc:def:crs:EPSG::4326"]}, (4.89, 52.37, 4.90, 52.38)
    )
    assert crs == "urn:ogc:def:crs:EPSG::4326"
    assert bbox == "52.37,4.89,52.38,4.9," + crs


def test_crs84_bounds_remain_longitude_first():
    crs, bbox = core._wfs_spatial_params({"crs": [core.CRS84]}, (4.89, 52.37, 4.90, 52.38))
    assert bbox == "4.89,52.37,4.9,52.38," + crs


def test_wfs_rejects_layers_without_supported_geographic_crs():
    with pytest.raises(core.ExternalDataError, match="advertise"):
        core._wfs_spatial_params({"crs": ["EPSG:28992"]}, (4, 52, 5, 53))


def test_wfs_download_pages_with_pdok_axis_order_and_provenance(monkeypatch):
    from urllib.parse import parse_qs, urlsplit

    capabilities = b"""<WFS_Capabilities version="2.0.0">
      <FeatureTypeList><FeatureType><Name>demo:zones</Name>
      <DefaultCRS>urn:ogc:def:crs:EPSG::4326</DefaultCRS>
      <OutputFormats><Format>application/json</Format></OutputFormats>
      </FeatureType></FeatureTypeList></WFS_Capabilities>"""
    requests = []
    pages = [
        {
            "type": "FeatureCollection",
            "features": [feature()],
            "numberMatched": "2",
            "crs": {"properties": {"name": core.CRS84}},
        },
        {
            "type": "FeatureCollection",
            "features": [feature(4.6, 52.6, "two")],
            "numberMatched": "2",
        },
    ]

    class Reader:
        def get(self, url):
            return capabilities, {}

        def json(self, url):
            requests.append(parse_qs(urlsplit(url).query))
            return pages.pop(0)

    monkeypatch.setattr(core, "PublicReader", Reader)
    result = core.download_source(BASE + "?service=WFS", "demo:zones", AREA)
    assert result["complete"] and result["feature_count"] == 2
    assert result["downloaded_count"] == 2
    assert [query["startIndex"] for query in requests] == [["0"], ["1"]]
    assert requests[0]["bbox"] == ["52.0,4.0,53.0,5.0,urn:ogc:def:crs:EPSG::4326"]
    assert requests[0]["srsName"] == ["urn:ogc:def:crs:EPSG::4326"]
    assert result["geojson"]["features"][1]["properties"]["_external_feature_id"] == "two"
    assert [feature["id"] for feature in result["geojson"]["features"]] == ["one", "two"]


def test_ogc_discovery_autodetects_json_and_reports_catalog_pagination():
    calls = []

    def read_json(url):
        calls.append(url)
        return {
            "collections": [{"id": "zones", "title": "Zones"}],
            "links": [{"rel": "next", "href": "?page=2"}],
        }

    reader = SimpleNamespace(get=lambda url: (b'{"links": []}', {}), json=read_json)
    result = core.inspect_source(BASE, reader=reader)
    assert calls == [BASE + "/collections"]
    assert result["protocol"] == "ogc_features"
    assert result["layers"] == [{"id": "zones", "title": "Zones"}]
    assert result["catalog_truncated"] is True


@pytest.mark.parametrize(
    "body,headers,message",
    [
        (
            b'{"features": []}',
            {"content-crs": "<http://www.opengis.net/def/crs/EPSG/0/28992>"},
            "CRS84",
        ),
        (b"not JSON", {}, "valid GeoJSON/JSON"),
        (b"[]", {}, "must be an object"),
    ],
)
def test_public_reader_rejects_invalid_json_and_response_crs(monkeypatch, body, headers, message):
    reader = core.PublicReader()
    monkeypatch.setattr(reader, "get", lambda url: (body, headers))
    with pytest.raises(core.ExternalDataError, match=message):
        reader.json(BASE)


def test_public_reader_accepts_crs84_json(monkeypatch):
    reader = core.PublicReader()
    monkeypatch.setattr(
        reader, "get", lambda url: (b'{"features": []}', {"content-crs": core.CRS84})
    )
    assert reader.json(BASE) == {"features": []}


def test_tls_uses_validated_ip_but_verifies_provider_hostname(monkeypatch):
    from unittest.mock import Mock

    sock = Mock()
    context = Mock()
    monkeypatch.setattr(core.ssl, "create_default_context", lambda: context)
    monkeypatch.setattr(core.socket, "socket", lambda *args: sock)
    connection = core._PinnedHTTPSConnection("provider.example", "8.8.8.8", 3)
    connection.connect()
    sock.connect.assert_called_once_with(("8.8.8.8", 443))
    context.wrap_socket.assert_called_once_with(sock, server_hostname="provider.example")
    context.wrap_socket.side_effect = OSError("TLS handshake failed")
    with pytest.raises(OSError):
        connection.connect()
    sock.close.assert_called_once()
