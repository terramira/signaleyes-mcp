"""Bounded public OGC downloads. No account credentials or application settings here."""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import math
import socket
import ssl
import time
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

from shapely import from_wkt, get_coordinates
from shapely.errors import GEOSException
from shapely.geometry import mapping, shape

Protocol = Literal["auto", "wfs", "ogc_features"]
PAGE_SIZE = 500
MAX_FEATURES = 5_000
MAX_PAGES = 20
MAX_BYTES = 10 * 1024 * 1024
MAX_SECONDS = 60
CRS84 = "urn:ogc:def:crs:OGC:1.3:CRS84"
_WFS_QUERY_KEYS = {
    "service",
    "request",
    "version",
    "typename",
    "typenames",
    "outputformat",
    "srsname",
    "bbox",
    "count",
    "maxfeatures",
    "startindex",
    "resulttype",
    "f",
}


class ExternalDataError(ValueError):
    """An external source cannot be imported safely or completely."""


def public_url(url: str) -> str:
    """Reject credentials, fragments, non-HTTPS schemes and nonstandard ports."""
    try:
        parts = urlsplit(url)
        if (
            len(url) > 4096
            or parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.port not in (None, 443)
            or parts.fragment
            or any(ord(char) < 33 or ord(char) == 127 for char in url)
            or "\\" in url
        ):
            raise ExternalDataError(
                "Use a public HTTPS URL on port 443 without credentials or a fragment."
            )
        host = parts.hostname.encode("idna").decode("ascii").lower()
        authority = f"[{host}]" if ":" in host else host
        return urlunsplit(("https", authority, parts.path or "/", parts.query, ""))
    except (ValueError, UnicodeError) as exc:
        raise ExternalDataError("Invalid public HTTPS source URL.") from exc


def source_url(url: str) -> tuple[str, bool]:
    """Accept public service links, stripping WFS request parameters we construct ourselves."""
    normalized = public_url(url)
    parts = urlsplit(normalized)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if any(key.lower() not in _WFS_QUERY_KEYS for key in query):
        raise ExternalDataError(
            "Use public service links without keys, tokens or custom query parameters."
        )
    is_wfs = any(
        key.lower() == "service" and value.lower() == "wfs" for key, value in query.items()
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", "")), is_wfs


def same_origin(base: str, link: str) -> str:
    target = public_url(urljoin(base, link))
    if urlsplit(target).netloc != urlsplit(base).netloc:
        raise ExternalDataError(
            "External paging and service links must stay on the original HTTPS host."
        )
    return target


def _public_addresses(host: str) -> list[str]:
    addresses = {
        str(entry[4][0]) for entry in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    }
    if not addresses or any(
        not ipaddress.ip_address(address).is_global
        or ipaddress.ip_address(address).is_multicast
        or ipaddress.ip_address(address).is_reserved
        for address in addresses
    ):
        raise ExternalDataError("Source resolves to a private, reserved or non-public address.")
    return sorted(addresses, key=lambda address: (":" in address, address))


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to the validated numeric address while verifying TLS for the real hostname."""

    def __init__(self, host: str, address: str, timeout: float) -> None:
        self.tls_context = ssl.create_default_context()
        super().__init__(host, timeout=timeout, context=self.tls_context)
        self.address = address

    def connect(self) -> None:
        family = socket.AF_INET6 if ":" in self.address else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            sock.connect((self.address, 443))
            self.sock = self.tls_context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


class PublicReader:
    """Read within a shared byte/time budget, pinning each connection after DNS validation."""

    def __init__(self) -> None:
        self.remaining = MAX_BYTES
        self.deadline = time.monotonic() + MAX_SECONDS

    def get(self, url: str) -> tuple[bytes, dict[str, str]]:
        parts = urlsplit(public_url(url))
        host = parts.hostname
        if host is None:
            raise ExternalDataError("Missing source hostname.")
        try:
            addresses = _public_addresses(host)
            remaining_time = self.deadline - time.monotonic()
            if remaining_time <= 0:
                raise ExternalDataError(
                    "External download exceeded its time limit. Select a smaller area."
                )
            connection = _PinnedHTTPSConnection(host, addresses[0], min(10.0, remaining_time))
            try:
                target = parts.path or "/"
                if parts.query:
                    target += "?" + parts.query
                connection.request(
                    "GET",
                    target,
                    headers={
                        "Accept": "application/geo+json, application/json, application/xml",
                        "Accept-Encoding": "identity",
                        "User-Agent": "SignalEyes-External-Import/1.0",
                    },
                )
                response = connection.getresponse()
                if response.status != 200:
                    raise ExternalDataError(
                        f"External HTTP {response.status}. Redirects are not followed."
                    )
                headers = {key.lower(): value for key, value in response.getheaders()}
                if headers.get("content-encoding", "identity").lower() != "identity":
                    raise ExternalDataError(
                        "Compressed external responses are not supported in this version."
                    )
                chunks = []
                while True:
                    remaining_time = self.deadline - time.monotonic()
                    if remaining_time <= 0:
                        raise ExternalDataError(
                            "External download exceeded its time limit. Select a smaller area."
                        )
                    if connection.sock is not None:
                        connection.sock.settimeout(min(10.0, remaining_time))
                    chunk = response.read(min(65_536, self.remaining + 1))
                    if not chunk:
                        break
                    self.remaining -= len(chunk)
                    if self.remaining < 0:
                        raise ExternalDataError(
                            "External download exceeds 10 MiB. Select a smaller area."
                        )
                    chunks.append(chunk)
                return b"".join(chunks), headers
            finally:
                connection.close()
        except (OSError, http.client.HTTPException) as exc:
            raise ExternalDataError("External service connection failed or timed out.") from exc

    def json(self, url: str) -> dict[str, Any]:
        body, headers = self.get(url)
        crs = headers.get("content-crs", "").strip("<>").lower()
        if crs and not crs.endswith(("/crs84", ":crs84")):
            raise ExternalDataError(
                "External GeoJSON must use longitude/latitude CRS84 coordinates."
            )
        try:
            data = json.loads(body)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise ExternalDataError("External service did not return valid GeoJSON/JSON.") from exc
        if not isinstance(data, dict):
            raise ExternalDataError("External JSON must be an object.")
        return data


def _xml(body: bytes) -> ET.Element:
    try:
        text = body.decode("utf-8-sig")
        if "<!doctype" in text.lower() or "<!entity" in text.lower():
            raise ExternalDataError("External XML cannot contain a DTD or entity declaration.")
        return ET.fromstring(text)  # noqa: S314 - DTDs and entities are rejected above; no remote schemas are loaded.
    except (UnicodeError, ET.ParseError) as exc:
        raise ExternalDataError("External capabilities must be valid UTF-8 XML.") from exc


def _query(base: str, **params: Any) -> str:
    return base + "?" + urlencode(params)


def _wfs_catalog(reader: PublicReader, base: str) -> dict[str, Any]:
    body, _ = reader.get(_query(base, service="WFS", version="2.0.0", request="GetCapabilities"))
    root = _xml(body)
    if root.tag.rsplit("}", 1)[-1] != "WFS_Capabilities" or root.get("version") != "2.0.0":
        raise ExternalDataError("This version requires a WFS 2.0.0 service.")
    formats: list[str] = []
    for operation in root.findall(".//{*}Operation"):
        if operation.get("name") == "GetFeature":
            for parameter in operation.findall(".//{*}Parameter"):
                if parameter.get("name", "").lower() == "outputformat":
                    formats.extend(
                        node.text.strip() for node in parameter.findall(".//{*}Value") if node.text
                    )
    formats.extend(
        node.text.strip() for node in root.findall(".//{*}OutputFormats/{*}Format") if node.text
    )
    output_format = next(
        (
            value
            for value in formats
            if value.lower() in ("application/json", "application/geo+json", "json")
        ),
        None,
    )
    if output_format is None:
        raise ExternalDataError(
            "WFS must advertise GeoJSON output. GML-only services are not supported yet."
        )
    layers = []
    for layer in root.findall(".//{*}FeatureTypeList/{*}FeatureType"):
        name = layer.findtext("{*}Name", "").strip()
        if name:
            layers.append(
                {
                    "id": name,
                    "title": layer.findtext("{*}Title", name)[:120],
                    "crs": [
                        node.text.strip()
                        for node in layer
                        if node.tag.rsplit("}", 1)[-1] in ("DefaultCRS", "OtherCRS") and node.text
                    ],
                }
            )
    return {"protocol": "wfs", "url": base, "layers": layers, "output_format": output_format}


def inspect_source(
    url: str, protocol: Protocol = "auto", reader: PublicReader | None = None
) -> dict[str, Any]:
    """Discover public layers. Returned titles are untrusted data, never instructions."""
    base, looks_wfs = source_url(url)
    reader = reader or PublicReader()
    if protocol not in ("auto", "wfs", "ogc_features"):
        raise ExternalDataError("Protocol must be auto, wfs or ogc_features.")
    if protocol == "wfs" or (
        protocol == "auto" and (looks_wfs or urlsplit(base).path.lower().endswith("wfs"))
    ):
        catalog = _wfs_catalog(reader, base)
        catalog["catalog_truncated"] = len(catalog["layers"]) > 100
        catalog["layers"] = catalog["layers"][:100]
        return catalog
    if protocol == "auto":
        body, _ = reader.get(base)
        if body.lstrip().startswith(b"<"):
            return inspect_source(base, "wfs", reader)
    collections = reader.json(base + "/collections")
    layers = collections.get("collections")
    if not isinstance(layers, list):
        raise ExternalDataError("Expected an OGC API Features base URL with /collections.")
    return {
        "protocol": "ogc_features",
        "url": base,
        "layers": [
            {"id": layer["id"], "title": str(layer.get("title", layer["id"]))[:120]}
            for layer in layers[:100]
            if isinstance(layer, dict) and isinstance(layer.get("id"), str)
        ],
        "catalog_truncated": len(layers) > 100
        or any(
            link.get("rel") == "next"
            for link in collections.get("links", [])
            if isinstance(link, dict)
        ),
    }


def _count(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def _wfs_spatial_params(layer: dict[str, Any], bounds: tuple[float, ...]) -> tuple[str, str]:
    systems = layer.get("crs", [])
    for value in systems:
        if value.lower().endswith(("/crs84", ":crs84")) or value.lower() == "crs:84":
            return value, ",".join(str(number) for number in bounds) + "," + value
    epsg4326 = "urn:ogc:def:crs:EPSG::4326"
    if any(
        value.lower()
        in (epsg4326.lower(), "epsg:4326", "http://www.opengis.net/def/crs/epsg/0/4326")
        for value in systems
    ):
        # WFS 2 EPSG:4326 bounding boxes are latitude-first; GeoJSON must still be CRS84.
        ordered = (bounds[1], bounds[0], bounds[3], bounds[2])
        return epsg4326, ",".join(str(number) for number in ordered) + "," + epsg4326
    raise ExternalDataError("WFS layer must advertise CRS84 or EPSG:4326 support.")


def download_source(
    url: str, layer: str, polygon: str, protocol: Protocol = "auto"
) -> dict[str, Any]:
    """Collect a complete bounded snapshot, then clip it to the active WGS84 polygon."""
    if not layer or len(layer) > 200:
        raise ExternalDataError("Choose a layer identifier returned by discovery.")
    try:
        area = from_wkt(polygon)
    except (TypeError, ValueError, GEOSException) as exc:
        raise ExternalDataError("Set a valid active WGS84 polygon before importing.") from exc
    if area.geom_type not in ("Polygon", "MultiPolygon") or area.is_empty or not area.is_valid:
        raise ExternalDataError("The active area must be a valid nonempty polygon.")
    if any(not math.isfinite(value) for value in area.bounds) or not (
        -180 <= area.bounds[0] < area.bounds[2] <= 180
        and -90 <= area.bounds[1] < area.bounds[3] <= 90
    ):
        raise ExternalDataError("The active area must use longitude/latitude coordinates.")
    reader = PublicReader()
    catalog = inspect_source(url, protocol, reader)
    base, protocol = catalog["url"], catalog["protocol"]
    if protocol == "wfs" and layer not in {entry["id"] for entry in catalog["layers"]}:
        catalog = _wfs_catalog(reader, base)
        if layer not in {entry["id"] for entry in catalog["layers"]}:
            raise ExternalDataError("Layer is not in the WFS catalog.")
    bbox = ",".join(str(value) for value in area.bounds)
    if protocol == "wfs":
        selected = next(entry for entry in catalog["layers"] if entry["id"] == layer)
        wfs_crs, wfs_bbox = _wfs_spatial_params(selected, area.bounds)
    item_url = base + "/collections/" + quote(layer, safe="") + "/items"
    if protocol == "ogc_features":
        metadata = reader.json(base + "/collections/" + quote(layer, safe=""))
        if metadata.get("id") != layer:
            raise ExternalDataError(
                "External collection metadata does not match the selected layer."
            )
        next_url = _query(item_url, bbox=bbox, limit=PAGE_SIZE)
    else:
        next_url = ""
    features, seen_features, seen_pages, seen_links = [], set(), set(), set()
    fetched = 0
    expected_matched = None
    imported_at = datetime.now(UTC).isoformat()
    for _ in range(MAX_PAGES):
        if protocol == "wfs":
            next_url = _query(
                base,
                service="WFS",
                version="2.0.0",
                request="GetFeature",
                typeNames=layer,
                srsName=wfs_crs,
                bbox=wfs_bbox,
                count=PAGE_SIZE,
                startIndex=fetched,
                outputFormat=catalog["output_format"],
            )
        if next_url in seen_links:
            raise ExternalDataError(
                "External pagination repeats a URL. No partial import was submitted."
            )
        seen_links.add(next_url)
        data = reader.json(same_origin(base, next_url))
        page = data.get("features")
        if data.get("type") != "FeatureCollection" or not isinstance(page, list):
            raise ExternalDataError("Expected a CRS84 GeoJSON FeatureCollection.")
        if data.get("crs"):
            crs = data["crs"]
            name = crs.get("properties", {}).get("name", "") if isinstance(crs, dict) else ""
            if not isinstance(name, str) or not name.lower().endswith(("/crs84", ":crs84")):
                raise ExternalDataError(
                    "External GeoJSON must use longitude/latitude CRS84 coordinates."
                )
        page_hash = hashlib.sha256(json.dumps(page, sort_keys=True).encode()).hexdigest()
        if page and page_hash in seen_pages:
            raise ExternalDataError(
                "External service repeats features instead of paging. Narrow the area."
            )
        seen_pages.add(page_hash)
        fetched += len(page)
        if fetched > MAX_FEATURES:
            raise ExternalDataError("Import exceeds 5,000 features. Select a smaller area.")
        for feature in page:
            if (
                not isinstance(feature, dict)
                or feature.get("type") != "Feature"
                or not isinstance(feature.get("properties"), (dict, type(None)))
            ):
                raise ExternalDataError("External service returned an invalid GeoJSON feature.")
            try:
                geometry = shape(feature.get("geometry"))
                coordinates = get_coordinates(geometry)
                if (
                    geometry.is_empty
                    or not geometry.is_valid
                    or any(not math.isfinite(float(value)) for row in coordinates for value in row)
                ):
                    raise ExternalDataError("External feature geometry is invalid or empty.")
                if any(
                    not (-180 <= float(x) <= 180 and -90 <= float(y) <= 90) for x, y in coordinates
                ):
                    raise ExternalDataError(
                        "External features do not use longitude/latitude coordinates."
                    )
                clipped = geometry.intersection(area)
            except (
                ValueError,
                TypeError,
                AttributeError,
                IndexError,
                KeyError,
                GEOSException,
            ) as exc:
                raise ExternalDataError("External service returned an invalid geometry.") from exc
            if clipped.is_empty:
                continue
            fingerprint = hashlib.sha256(json.dumps(feature, sort_keys=True).encode()).hexdigest()
            if fingerprint in seen_features:
                continue
            seen_features.add(fingerprint)
            properties = dict(feature.get("properties") or {})
            if any(str(key).startswith("_external_") for key in properties):
                raise ExternalDataError("Source uses reserved _external_ property names.")
            properties.update(
                {
                    "_external_feature_id": feature.get("id"),
                    "_external_source": base,
                    "_external_layer": layer,
                    "_external_imported_at": imported_at,
                }
            )
            features.append(
                {"type": "Feature", "geometry": mapping(clipped), "properties": properties}
            )
        matched = _count(data.get("numberMatched"))
        if matched is not None:
            if expected_matched is not None and matched != expected_matched:
                raise ExternalDataError(
                    "External result count changed during pagination. Retry the import."
                )
            expected_matched = matched
        if matched is not None and fetched > matched:
            raise ExternalDataError(
                "External result count changed during pagination. Retry the import."
            )
        if protocol == "wfs":
            if not page or matched == fetched:
                if matched is not None and fetched < matched:
                    raise ExternalDataError(
                        "External service stopped before all matching features were fetched."
                    )
                break
        else:
            links = [
                link.get("href")
                for link in data.get("links", [])
                if isinstance(link, dict) and link.get("rel") == "next"
            ]
            if not links:
                if matched is not None and fetched < matched:
                    raise ExternalDataError(
                        "External result is incomplete and has no next-page link."
                    )
                break
            if len(links) != 1 or not isinstance(links[0], str):
                raise ExternalDataError("External pagination has invalid next-page links.")
            next_url = same_origin(next_url, links[0])
    else:
        raise ExternalDataError("Import exceeds 20 pages. Select a smaller area.")
    return {
        "geojson": {"type": "FeatureCollection", "features": features},
        "feature_count": len(features),
        "downloaded_count": fetched,
        "source": base,
        "layer": layer,
        "protocol": protocol,
        "imported_at": imported_at,
        "complete": True,
    }
