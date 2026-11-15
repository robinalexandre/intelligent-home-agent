"""Open-source investigation of what the user gives: a photo, a document, a post, a link.

The model already sees images, searches the web and reads pages; these tools add what an
investigator checks next, from public sources and without any key:

- inspect_file: a kept file's metadata (EXIF with its GPS position and the address there,
  camera, editing software, dates; a PDF's or an Office file's author and dates) and a
  fingerprint to compare two pictures;
- domain_lookup: who registered a site and when (RDAP), its DNS, its first certificate
  (crt.sh) and its first and last captures by the Wayback Machine: how old a site is says
  a lot about a "bank" or a shop seen in a post;
- osm_lookup: a place named, or GPS coordinates, checked on OpenStreetMap;
- reverse_image_search, when the agent has a public address (PUBLIC_URL): a photo the
  user sent, put online for a few minutes so the image search engines can fetch it.

Lookups go to registries and archives, never to the site investigated, and a name read in
this request goes out only as the request's own guards allow. Identifying a private person
from their face, or finding where they live, is not what these tools are for (prompts.py).
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from io import BytesIO
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from PIL import Image, UnidentifiedImageError
from PIL.ExifTags import GPS, IFD, Base
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from intelligent_home_agent.conversations import Attachment
from intelligent_home_agent.shared_links import LINKS, TTL_SECONDS, SharedLinks
from intelligent_home_agent.tools.base import Tool, ToolContext, ToolError

RDAP_URL = "https://rdap.org/domain/"
CDX_URL = "https://web.archive.org/cdx/search/cdx"
CRT_URL = "https://crt.sh/"
NOMINATIM_SEARCH = "https://nominatim.openstreetmap.org/search"
# Nominatim's usage policy asks for an identifying User-Agent.
USER_AGENT = "intelligent-home-agent/0.1 (self-hosted assistant)"
MAX_CRT_BYTES = 3_000_000
MAX_TEXT = 300
_DOMAIN = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_EXIF_KEPT = {
    Base.Make: "camera_make",
    Base.Model: "camera_model",
    Base.Software: "software",
    Base.DateTime: "modified",
    Base.Artist: "artist",
    Base.Copyright: "copyright",
    Base.ImageDescription: "description",
    Base.HostComputer: "host_computer",
}
_EXIF_SUB = {
    Base.DateTimeOriginal: "taken",
    Base.OffsetTimeOriginal: "taken_utc_offset",
    Base.LensModel: "lens",
    Base.BodySerialNumber: "camera_serial",
}
# Second-level endings sold like top-level ones: the registered domain is one label more.
SECOND_LEVEL = frozenset(
    {"co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au", "co.nz", "co.jp",
     "com.br", "com.mx", "com.tr", "co.in", "co.za", "com.cn", "gouv.fr", "asso.fr",
     "com.fr", "co.il", "com.sg", "com.hk"}
)  # fmt: skip
OFFICE_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
}


def _clean(value: Any) -> Any:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        value = " ".join(value.replace("\x00", "").split())[:MAX_TEXT]
        return value or None
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _degrees(values: Any, ref: Any) -> float | None:
    try:
        d, m, s = (float(v) for v in values)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    sign = -1 if str(ref).upper() in ("S", "W") else 1
    return round(sign * (d + m / 60 + s / 3600), 6)


def average_hash(image: Image.Image) -> str:
    """A 64-bit fingerprint that stays close when a picture is resized or recompressed."""
    small = image.convert("L").resize((8, 8))
    pixels = list(small.tobytes())
    mean = sum(pixels) / len(pixels)
    bits = "".join("1" if p >= mean else "0" for p in pixels)
    return f"{int(bits, 2):016x}"


def image_facts(data: bytes) -> dict[str, Any]:
    try:
        image = Image.open(BytesIO(data))
        image.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ToolError("This image can't be read.") from exc
    facts: dict[str, Any] = {
        "format": image.format,
        "size": f"{image.width}x{image.height}",
        "fingerprint": average_hash(image),
    }
    exif = image.getexif()
    found = {name: _clean(exif.get(tag)) for tag, name in _EXIF_KEPT.items()}
    sub = exif.get_ifd(IFD.Exif)
    found |= {name: _clean(sub.get(tag)) for tag, name in _EXIF_SUB.items()}
    gps = exif.get_ifd(IFD.GPSInfo)
    lat = _degrees(gps.get(GPS.GPSLatitude), gps.get(GPS.GPSLatitudeRef))
    lon = _degrees(gps.get(GPS.GPSLongitude), gps.get(GPS.GPSLongitudeRef))
    if lat is not None and lon is not None:
        found["gps"] = {"lat": lat, "lon": lon}
        if (altitude := gps.get(GPS.GPSAltitude)) is not None:
            found["gps"]["altitude_m"] = round(float(altitude), 1)
        if (stamp := gps.get(GPS.GPSDateStamp)) is not None:
            found["gps"]["date_utc"] = _clean(stamp)
    facts["exif"] = {k: v for k, v in found.items() if v not in (None, "")}
    xmp = image.info.get("xmp") or image.info.get("XML:com.adobe.xmp")
    if xmp:
        text = xmp.decode("utf-8", "replace") if isinstance(xmp, bytes) else str(xmp)
        tools = sorted(set(re.findall(r"(?:CreatorTool|softwareAgent)[=>\"]+([^<\"]+)", text)))
        if tools:
            facts["edited_with"] = [_clean(t) for t in tools[:5]]
        if "c2pa" in text.lower() or "contentcredentials" in text.lower():
            facts["content_credentials"] = "C2PA marks present (provenance or AI-made)"
    if not facts["exif"]:
        facts["note"] = (
            "No EXIF: social networks and messaging apps strip it, so a picture with none "
            "has usually been shared or saved again."
        )
    return facts


def pdf_facts(data: bytes) -> dict[str, Any]:
    try:
        reader = PdfReader(BytesIO(data))
        info = reader.metadata or {}
        pages = len(reader.pages)
    except (PyPdfError, ValueError) as exc:
        raise ToolError("This PDF can't be read.") from exc
    keys = {
        "/Author": "author",
        "/Creator": "creator",
        "/Producer": "producer",
        "/Title": "title",
        "/CreationDate": "created",
        "/ModDate": "modified",
    }
    meta = {name: _clean(info.get(key)) for key, name in keys.items()}
    return {"pages": pages, "metadata": {k: v for k, v in meta.items() if v}}


def office_facts(data: bytes, kind: str) -> dict[str, Any]:
    try:
        if kind == "docx":
            import docx

            props = docx.Document(BytesIO(data)).core_properties
        elif kind == "xlsx":
            import openpyxl

            props = openpyxl.load_workbook(BytesIO(data), read_only=True).properties
        else:
            import pptx

            props = pptx.Presentation(BytesIO(data)).core_properties
    except Exception as exc:
        raise ToolError("This document can't be read.") from exc
    # python-docx and python-pptx say author, openpyxl creator.
    fields = {
        "creator": getattr(props, "author", None) or getattr(props, "creator", None),
        "last_modified_by": getattr(props, "last_modified_by", None)
        or getattr(props, "lastModifiedBy", None),
        "created": getattr(props, "created", None),
        "modified": getattr(props, "modified", None),
        "title": getattr(props, "title", None),
        "revision": getattr(props, "revision", None),
    }
    meta = {k: _clean(v) for k, v in fields.items()}
    return {"metadata": {k: v for k, v in meta.items() if v}}


def domain_of(text: str) -> str:
    text = text.strip().lower()
    host = urlsplit(text if "://" in text else f"http://{text}").hostname or ""
    host = host.rstrip(".").removeprefix("www.")
    try:
        host = host.encode("idna").decode()
    except UnicodeError as exc:
        raise ToolError("This is not a domain name.") from exc
    if not _DOMAIN.match(host):
        raise ToolError("Give a domain name or an address (e.g. example.com).")
    return host


def registered_domain(host: str) -> str:
    """The name a registrar sold (bank-login.example for secure.bank-login.example)."""
    labels = host.split(".")
    keep = 3 if ".".join(labels[-2:]) in SECOND_LEVEL else 2
    return ".".join(labels[-keep:])


def _vcard_name(entity: dict[str, Any]) -> str | None:
    for field in (entity.get("vcardArray") or [None, []])[1] or []:
        if isinstance(field, list) and len(field) >= 4 and field[0] == "fn":
            return _clean(field[3])
    return None


def _days_since(stamp: str) -> int | None:
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return (datetime.now(UTC) - when).days


def _wayback_date(stamp: str) -> str:
    return f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}" if len(stamp) >= 8 else stamp


class Investigator:
    """The public sources behind domain_lookup and osm_lookup."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        rdap_url: str = RDAP_URL,
        cdx_url: str = CDX_URL,
        crt_url: str = CRT_URL,
        nominatim_url: str = NOMINATIM_SEARCH,
        resolve_dns: bool = True,
    ) -> None:
        self._http = http
        self._rdap = rdap_url
        self._cdx = cdx_url
        self._crt = crt_url
        self._search = nominatim_url
        self._reverse = nominatim_url.rsplit("/", 1)[0] + "/reverse"
        self._dns = resolve_dns

    async def _json(self, endpoint: str, **params: Any) -> Any:
        resp = await self._http.get(
            endpoint,
            params=params or None,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=25,
            follow_redirects=True,
        )
        resp.raise_for_status()
        return resp.json()

    async def registration(self, domain: str) -> dict[str, Any]:
        try:
            data = await self._json(self._rdap + domain)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return {"found": False, "note": "No registration: the domain may not exist."}
            return {"error": f"registry answered {exc.response.status_code}"}
        except (httpx.HTTPError, ValueError):
            return {"error": "registry unreachable"}
        events = {
            str(e.get("eventAction")): str(e.get("eventDate"))
            for e in data.get("events") or []
            if isinstance(e, dict)
        }
        registrar = next(
            (
                _vcard_name(e)
                for e in data.get("entities") or []
                if isinstance(e, dict) and "registrar" in (e.get("roles") or [])
            ),
            None,
        )
        out: dict[str, Any] = {
            "registered": events.get("registration"),
            "expires": events.get("expiration"),
            "last_changed": events.get("last changed"),
            "registrar": registrar,
            "status": [_clean(s) for s in (data.get("status") or [])][:8],
            "nameservers": [
                _clean(n.get("ldhName"))
                for n in data.get("nameservers") or []
                if isinstance(n, dict)
            ][:6],
        }
        if out["registered"] and (age := _days_since(out["registered"])) is not None:
            out["age_days"] = age
        return {k: v for k, v in out.items() if v}

    async def archive(self, domain: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        try:
            first = await self._json(
                self._cdx, url=domain, output="json", fl="timestamp", limit="1"
            )
            last = await self._json(
                self._cdx, url=domain, output="json", fl="timestamp", limit="-1", fastLatest="true"
            )
        except (httpx.HTTPError, ValueError):
            return {"error": "Wayback Machine unreachable"}
        if isinstance(first, list) and len(first) > 1:
            out["first_capture"] = _wayback_date(str(first[1][0]))
        if isinstance(last, list) and len(last) > 1:
            out["last_capture"] = _wayback_date(str(last[-1][0]))
        if not out:
            out["note"] = "Never captured by the Wayback Machine."
        else:
            out["browse"] = f"https://web.archive.org/web/*/{domain}"
        return out

    async def certificates(self, domain: str) -> dict[str, Any]:
        try:
            async with self._http.stream(
                "GET",
                self._crt,
                params={"q": domain, "output": "json"},
                headers={"User-Agent": USER_AGENT},
                timeout=30,
            ) as resp:
                resp.raise_for_status()
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_CRT_BYTES:
                        return {"note": "Too many certificates to list: a long-lived site."}
            rows = json.loads(body or b"[]")
        except (httpx.HTTPError, ValueError):
            return {"error": "crt.sh unreachable"}
        dates = sorted(str(r.get("not_before") or "") for r in rows if isinstance(r, dict))
        dates = [d for d in dates if d]
        if not dates:
            return {"note": "No certificate ever logged."}
        issuers = sorted({_clean(r.get("issuer_name")) or "" for r in rows if isinstance(r, dict)})
        return {
            "count": len(rows),
            "first": dates[0][:10],
            "latest": dates[-1][:10],
            "issuers": [i for i in issuers if i][:4],
        }

    async def dns_records(self, domain: str) -> dict[str, Any]:
        if not self._dns:
            return {}
        import dns.asyncresolver
        import dns.exception

        resolver = dns.asyncresolver.Resolver()
        resolver.lifetime = 8
        out: dict[str, Any] = {}
        for kind in ("A", "MX", "NS", "TXT"):
            try:
                answer = await resolver.resolve(domain, kind)
            except dns.exception.DNSException:
                continue
            out[kind] = [_clean(r.to_text()) for r in answer][:6]
        return out

    async def place(self, query: str) -> list[dict[str, Any]]:
        try:
            rows = await self._json(self._search, q=query, format="jsonv2", limit="5")
        except (httpx.HTTPError, ValueError) as exc:
            raise ToolError("OpenStreetMap can't be reached right now.") from exc
        return [_place(r) for r in rows if isinstance(r, dict)]

    async def address(self, lat: float, lon: float) -> dict[str, Any] | None:
        try:
            row = await self._json(self._reverse, lat=str(lat), lon=str(lon), format="jsonv2")
        except (httpx.HTTPError, ValueError):
            return None
        return _place(row) if isinstance(row, dict) and row.get("display_name") else None


def _place(row: dict[str, Any]) -> dict[str, Any]:
    lat, lon = row.get("lat"), row.get("lon")
    return {
        "name": _clean(row.get("display_name")),
        "kind": _clean(f"{row.get('category') or row.get('class') or ''} {row.get('type') or ''}"),
        "lat": lat,
        "lon": lon,
        "map": f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=18/{lat}/{lon}",
    }


def search_engines(image_url: str) -> dict[str, str]:
    """Reverse image searches that take a picture's address."""
    encoded = quote(image_url, safe="")
    return {
        "google_lens": f"https://lens.google.com/uploadbyurl?url={encoded}",
        "yandex": f"https://yandex.com/images/search?rpt=imageview&url={encoded}",
        "bing": f"https://www.bing.com/images/search?view=detailv2&iss=sbi&q=imgurl:{encoded}",
        "tineye": f"https://tineye.com/search?url={encoded}",
    }


def stripped_jpeg(data: bytes) -> bytes:
    """The picture alone, re-encoded: no EXIF, GPS or other metadata goes out."""
    try:
        with Image.open(BytesIO(data)) as image:
            image.seek(0)
            frame = image.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ToolError("This image can't be read.") from exc
    frame.thumbnail((2000, 2000))
    out = BytesIO()
    frame.save(out, "JPEG", quality=90)
    return out.getvalue()


def _kept(ctx: ToolContext) -> str:
    names = ", ".join(sorted(ctx.conversation.files))
    return f"Kept files: {names}." if names else "This conversation keeps no file."


def build_osint_tools(
    investigator: Investigator | None,
    public_url: str | None = None,
    links: SharedLinks = LINKS,
) -> list[Tool]:
    """public_url: the agent's own public address; without it, no reverse image search."""
    if investigator is None:
        return []

    async def inspect_file(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        name = str(args.get("file_name") or "")
        file: Attachment | None = ctx.conversation.files.get(name)
        if file is None:
            raise ToolError(f"No kept file named {name}. {_kept(ctx)}")
        # Metadata is written by whoever made the file. A photo the user sent: others'
        # words but nothing private, like a web page (reads_web), so the links a reverse
        # search offers still open. A file from an email or elsewhere: private content.
        if file.origin != "user":
            ctx.tainted = True
        facts: dict[str, Any] = {
            "file": file.filename,
            "from": file.origin,
            "type": file.mimetype,
            "size_kb": round(len(file.data) / 1024),
            "sha256": hashlib.sha256(file.data).hexdigest(),
        }
        if file.mimetype.startswith("image/"):
            facts |= image_facts(file.data)
            gps = facts.get("exif", {}).get("gps")
            # Coordinates go to OpenStreetMap only from a clean request.
            if gps and not ctx.private_read:
                if place := await investigator.address(gps["lat"], gps["lon"]):
                    gps["address"] = place["name"]
                gps["map"] = _place({"lat": gps["lat"], "lon": gps["lon"]})["map"]
        elif file.mimetype == "application/pdf":
            facts |= pdf_facts(file.data)
        elif kind := OFFICE_TYPES.get(file.mimetype):
            facts |= office_facts(file.data, kind)
        else:
            facts["note"] = "Only images, PDFs and Office files have metadata read here."
        return facts

    async def domain_lookup(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        host = domain_of(str(args.get("domain") or ""))
        # Registries and archives only know the domain sold; it is the site's own name,
        # which the leak check leaves aside.
        domain = registered_domain(host)
        ctx.keep_in(f"https://{domain}/", "address")
        result: dict[str, Any] = {"domain": domain}
        if host != domain:
            result["host"] = host
        result["registration"] = await investigator.registration(domain)
        result["archive"] = await investigator.archive(domain)
        result["certificates"] = await investigator.certificates(domain)
        # A DNS query reaches the domain's own servers: not once the request read private
        # content, as a name made up from it would carry it to them.
        if not ctx.private_read:
            result["dns"] = await investigator.dns_records(host)
        age = result["registration"].get("age_days")
        if age is not None and age < 180:
            result["warning"] = f"Registered only {age} days ago: typical of scam sites."
        return result

    async def osm_lookup(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        lat, lon = args.get("lat"), args.get("lon")
        if lat is not None and lon is not None:
            try:
                lat, lon = float(lat), float(lon)
            except (TypeError, ValueError) as exc:
                raise ToolError("lat and lon are numbers.") from exc
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ToolError("These coordinates are off the map.")
            if ctx.private_read:
                raise ToolError("Coordinates read from private content stay here.")
            place = await investigator.address(lat, lon)
            return {"place": place or "No address at these coordinates."}
        if not query:
            raise ToolError("Give a place to search (query) or coordinates (lat, lon).")
        ctx.keep_in(query, "query")
        return {"places": await investigator.place(query)}

    async def reverse_image_search(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        name = str(args.get("file_name") or "")
        file = ctx.conversation.files.get(name)
        if file is None or not file.mimetype.startswith("image/"):
            raise ToolError(f"No kept image named {name}. {_kept(ctx)}")
        # Only what the user sent goes on a public address; a page's picture has its own.
        if file.origin != "user":
            raise ToolError(
                f"{name} came from {file.origin}: only a photo the user sent is put online "
                "for a search. A picture from a page: give the engines its own address."
            )
        token = links.share(stripped_jpeg(file.data), "image/jpeg")
        image_url = f"{public_url.rstrip('/')}/s/{token}/image.jpg"
        engines = search_engines(image_url)
        ctx.offer_urls(list(engines.values()))
        return {
            "image_url": image_url,
            "expires_in_minutes": TTL_SECONDS // 60,
            "search": engines,
            "next": "Open these with a browsing tool when you have one and read the "
            "matches: pages where the picture appears, its earliest date, similar places. "
            "These engines mostly need a real browser: when a page comes back empty, give "
            "the user the links to open themselves while they last.",
        }

    tools = [
        Tool(
            name="inspect_file",
            description=(
                "Investigate a kept file (a photo, a screenshot, a PDF, an Office file): its "
                "metadata (EXIF: when and with what it was taken, the GPS position and the "
                "address there, editing software, AI or provenance marks; a document's "
                "author and dates), its SHA-256 and a fingerprint that stays close for the "
                "same picture resized. Look at the picture itself too."
            ),
            input_schema={
                "type": "object",
                "properties": {"file_name": {"type": "string"}},
                "required": ["file_name"],
            },
            handler=inspect_file,
            reads_web=True,
        ),
        Tool(
            name="domain_lookup",
            description=(
                "Investigate a website or a link (from a post, an SMS, an email): when and "
                "with whom its domain was registered, its first and last captures in the "
                "Wayback Machine, its first certificate and its DNS. A domain a few weeks "
                "old posing as a known brand is a scam sign. Asks registries and archives, "
                "never the site itself."
            ),
            input_schema={
                "type": "object",
                "properties": {"domain": {"type": "string", "description": "example.com or a URL"}},
                "required": ["domain"],
            },
            handler=domain_lookup,
            reads_web=True,
        ),
        Tool(
            name="osm_lookup",
            description=(
                "Check a place on OpenStreetMap: search a name or an address (query) for "
                "its position and a map link, or give coordinates (lat, lon) for the "
                "address there. Use it to confirm where a photo was taken once you have a "
                "guess (a sign, a landmark, a shop name)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "lat": {"type": "number"},
                    "lon": {"type": "number"},
                },
            },
            handler=osm_lookup,
            reads_web=True,
        ),
    ]
    if public_url:
        tools.append(
            Tool(
                name="reverse_image_search",
                description=(
                    "Put a photo the user sent online for 15 minutes (metadata stripped, "
                    "at an unguessable address) and get the reverse image search links "
                    "(Google Lens, Yandex, Bing, TinEye): "
                    "where else the picture appears, since when, whether a profile photo "
                    "was taken from someone else, what a place or an object is."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"file_name": {"type": "string"}},
                    "required": ["file_name"],
                },
                handler=reverse_image_search,
            )
        )
    return tools
