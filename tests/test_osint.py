from io import BytesIO

import httpx
import pytest
import respx
from PIL import Image
from PIL.ExifTags import GPS, IFD, Base
from pypdf import PdfWriter

from intelligent_home_agent.conversations import Attachment
from intelligent_home_agent.tools import Investigator, ToolError, build_osint_tools
from intelligent_home_agent.tools.osint import average_hash, domain_of, registered_domain

RDAP = "https://rdap.test/domain/"
CDX = "https://archive.test/cdx"
CRT = "https://crt.test/"
OSM = "https://osm.test/search"


def photo(gps: bool = True, color: str = "skyblue") -> bytes:
    image = Image.new("RGB", (64, 48), color)
    exif = Image.Exif()
    exif[Base.Make] = "Acme"
    exif[Base.Model] = "Phone 15"
    exif[Base.Software] = "Photo Editor 26.0"
    exif[IFD.Exif] = {Base.DateTimeOriginal: "2026:10:01 14:02:03"}
    if gps:
        exif[IFD.GPSInfo] = {
            GPS.GPSLatitudeRef: "N",
            GPS.GPSLatitude: (48.0, 51.0, 30.24),
            GPS.GPSLongitudeRef: "E",
            GPS.GPSLongitude: (2.0, 17.0, 40.2),
        }
    out = BytesIO()
    image.save(out, "JPEG", exif=exif)
    return out.getvalue()


def tools(http, dns=False):
    investigator = Investigator(
        http, rdap_url=RDAP, cdx_url=CDX, crt_url=CRT, nominatim_url=OSM, resolve_dns=dns
    )
    return {t.name: t for t in build_osint_tools(investigator)}


@respx.mock
async def test_a_photo_tells_when_where_and_with_what_it_was_taken(ctx):
    reverse = respx.get("https://osm.test/reverse").respond(
        json={"display_name": "Eiffel Tower, Paris", "lat": "48.8584", "lon": "2.2945"}
    )
    ctx.conversation.files["IMG.jpg"] = Attachment("IMG.jpg", "image/jpeg", photo())
    async with httpx.AsyncClient() as http:
        facts = await tools(http)["inspect_file"].handler({"file_name": "IMG.jpg"}, ctx)

    exif = facts["exif"]
    assert exif["camera_model"] == "Phone 15" and exif["taken"] == "2026:10:01 14:02:03"
    assert exif["software"] == "Photo Editor 26.0"
    assert exif["gps"]["lat"] == pytest.approx(48.8584, abs=1e-4)
    assert exif["gps"]["lon"] == pytest.approx(2.2945, abs=1e-4)
    assert exif["gps"]["address"] == "Eiffel Tower, Paris"
    assert "openstreetmap.org" in exif["gps"]["map"]
    assert facts["size"] == "64x48" and len(facts["sha256"]) == 64
    assert reverse.calls[0].request.headers["user-agent"].startswith("intelligent-home-agent")
    assert not ctx.tainted  # the user's own photo


async def test_a_shared_photo_has_no_exif_and_says_why(ctx):
    plain = BytesIO()
    Image.new("RGB", (10, 10)).save(plain, "PNG")
    ctx.conversation.files["screenshot.png"] = Attachment(
        "screenshot.png", "image/png", plain.getvalue(), "web"
    )
    async with httpx.AsyncClient() as http:
        facts = await tools(http)["inspect_file"].handler({"file_name": "screenshot.png"}, ctx)
    assert facts["exif"] == {} and "strip" in facts["note"]
    assert ctx.tainted  # a file from the web: its metadata is others' words


async def test_coordinates_of_a_private_read_stay_here(ctx):
    ctx.tainted = True
    ctx.conversation.files["attached.jpg"] = Attachment(
        "attached.jpg", "image/jpeg", photo(), "mail"
    )
    async with httpx.AsyncClient() as http:
        osint = tools(http)
        facts = await osint["inspect_file"].handler({"file_name": "attached.jpg"}, ctx)
        with pytest.raises(ToolError, match="stay here"):
            await osint["osm_lookup"].handler({"lat": 48.85, "lon": 2.29}, ctx)
    assert "address" not in facts["exif"]["gps"]  # no call went out (respx not active)


def test_the_fingerprint_survives_a_resize_but_not_another_picture():
    a = Image.new("RGB", (200, 100), "white")
    a.paste((0, 0, 0), (0, 0, 100, 100))
    b = a.resize((50, 25))
    c = Image.new("RGB", (200, 100), "white")
    c.paste((0, 0, 0), (100, 0, 200, 100))
    assert average_hash(a) == average_hash(b) != average_hash(c)


async def test_a_pdf_and_a_word_file_tell_their_author(ctx, tmp_path):
    import docx

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_metadata({"/Author": "Sam Doe", "/Creator": "Word"})
    pdf = BytesIO()
    writer.write(pdf)

    document = docx.Document()
    document.core_properties.author = "Jo Bloggs"
    document.core_properties.last_modified_by = "Kim"
    out = BytesIO()
    document.save(out)
    ctx.conversation.files.update(
        {
            "quote.pdf": Attachment("quote.pdf", "application/pdf", pdf.getvalue()),
            "cv.docx": Attachment(
                "cv.docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                out.getvalue(),
            ),
        }
    )
    async with httpx.AsyncClient() as http:
        inspect = tools(http)["inspect_file"]
        pdf_facts = await inspect.handler({"file_name": "quote.pdf"}, ctx)
        doc_facts = await inspect.handler({"file_name": "cv.docx"}, ctx)
        with pytest.raises(
            ToolError, match="No kept file named absent.jpg. Kept files: cv.docx, quote.pdf."
        ):
            await inspect.handler({"file_name": "absent.jpg"}, ctx)
    assert pdf_facts["metadata"]["author"] == "Sam Doe" and pdf_facts["pages"] == 1
    assert doc_facts["metadata"]["creator"] == "Jo Bloggs"
    assert doc_facts["metadata"]["last_modified_by"] == "Kim"


def test_domains_are_read_from_links_and_reduced_to_what_was_sold():
    assert domain_of("https://www.Secure.Bank-login.com/verify?id=1") == "secure.bank-login.com"
    assert domain_of("Parcel-Delivery.example") == "parcel-delivery.example"
    assert registered_domain("secure.bank-login.com") == "bank-login.com"
    assert registered_domain("shop.example.co.uk") == "example.co.uk"
    for bad in ["", "localhost", "http://10.0.0.1/", "not a domain"]:
        with pytest.raises(ToolError):
            domain_of(bad)


@respx.mock
async def test_a_young_domain_is_flagged_from_registries_and_archives(ctx):
    rdap = respx.get(f"{RDAP}bank-login.com").respond(
        json={
            "events": [
                {"eventAction": "registration", "eventDate": "2026-10-20T10:00:00Z"},
                {"eventAction": "expiration", "eventDate": "2027-10-20T10:00:00Z"},
            ],
            "entities": [
                {
                    "roles": ["registrar"],
                    "vcardArray": [
                        "vcard",
                        [["version", {}, "text", "4.0"], ["fn", {}, "text", "Cheap Registrar LLC"]],
                    ],
                }
            ],
            "status": ["client transfer prohibited"],
            "nameservers": [{"ldhName": "ns1.parking.example"}],
        }
    )
    respx.get(CDX).mock(
        side_effect=[
            httpx.Response(200, json=[["timestamp"], ["20261021093000"]]),
            httpx.Response(200, json=[["timestamp"], ["20261105120000"]]),
        ]
    )
    respx.get(CRT).respond(
        json=[
            {"not_before": "2026-10-21T00:00:00", "issuer_name": "C=US, O=Let's Encrypt"},
            {"not_before": "2026-11-01T00:00:00", "issuer_name": "C=US, O=Let's Encrypt"},
        ]
    )
    async with httpx.AsyncClient() as http:
        result = await tools(http)["domain_lookup"].handler(
            {"domain": "https://secure.bank-login.com/verify"}, ctx
        )

    assert result["domain"] == "bank-login.com" and result["host"] == "secure.bank-login.com"
    reg = result["registration"]
    assert reg["registrar"] == "Cheap Registrar LLC" and reg["age_days"] < 180
    assert "scam" in result["warning"]
    assert result["archive"]["first_capture"] == "2026-10-21"
    assert result["archive"]["last_capture"] == "2026-11-05"
    assert result["certificates"] == {
        "count": 2,
        "first": "2026-10-21",
        "latest": "2026-11-01",
        "issuers": ["C=US, O=Let's Encrypt"],
    }
    assert rdap.call_count == 1 and result["dns"] == {}


@respx.mock
async def test_an_unknown_domain_and_dead_sources_dont_break_the_lookup(ctx):
    respx.get(f"{RDAP}nowhere-xyz.com").respond(404)
    respx.get(CDX).respond(500)
    respx.get(CRT).mock(side_effect=httpx.ConnectError("down"))
    async with httpx.AsyncClient() as http:
        result = await tools(http)["domain_lookup"].handler({"domain": "nowhere-xyz.com"}, ctx)
    assert result["registration"]["found"] is False
    assert "unreachable" in result["archive"]["error"]
    assert "unreachable" in result["certificates"]["error"]


async def test_after_a_private_read_no_dns_query_goes_out(ctx, monkeypatch):
    ctx.tainted = True
    calls = []

    async def no_dns(self, host):
        calls.append(host)
        return {}

    monkeypatch.setattr(Investigator, "dns_records", no_dns)

    async def nothing(self, domain):
        return {}

    for name in ("registration", "archive", "certificates"):
        monkeypatch.setattr(Investigator, name, nothing)
    async with httpx.AsyncClient() as http:
        result = await tools(http, dns=True)["domain_lookup"].handler(
            {"domain": "secure.bank-login.com"}, ctx
        )
    assert calls == [] and "dns" not in result


@respx.mock
async def test_a_place_is_checked_on_openstreetmap(ctx):
    search = respx.get(OSM).respond(
        json=[
            {
                "display_name": "Tower Bridge, London",
                "lat": "51.5055",
                "lon": "-0.0754",
                "category": "bridge",
                "type": "yes",
            }
        ]
    )
    async with httpx.AsyncClient() as http:
        osm = tools(http)["osm_lookup"]
        result = await osm.handler({"query": "Tower Bridge London"}, ctx)
        with pytest.raises(ToolError, match="off the map"):
            await osm.handler({"lat": 120, "lon": 2}, ctx)
        with pytest.raises(ToolError, match="Give a place"):
            await osm.handler({}, ctx)
    assert result["places"][0]["name"] == "Tower Bridge, London"
    assert search.calls[0].request.url.params["q"] == "Tower Bridge London"


def test_shared_links_expire_and_stay_few():
    from intelligent_home_agent.shared_links import MAX_LINKS, SharedLinks

    now = [0.0]
    links = SharedLinks(clock=lambda: now[0])
    first = links.share(b"a", "image/jpeg", ttl=60)
    assert links.get(first).data == b"a" and links.get("guess") is None
    now[0] = 61
    assert links.get(first) is None
    tokens = [links.share(b"x", "image/jpeg") for _ in range(MAX_LINKS + 3)]
    assert links.get(tokens[0]) is None and links.get(tokens[-1]) is not None


async def test_a_sent_photo_goes_online_stripped_for_a_reverse_search(ctx):
    from urllib.parse import unquote

    from intelligent_home_agent.shared_links import SharedLinks

    links = SharedLinks()
    ctx.conversation.files["profile.jpg"] = Attachment("profile.jpg", "image/jpeg", photo())
    async with httpx.AsyncClient() as http:
        investigator = Investigator(http, resolve_dns=False)
        search = {
            t.name: t for t in build_osint_tools(investigator, "https://agent.example/", links)
        }["reverse_image_search"]
        result = await search.handler({"file_name": "profile.jpg"}, ctx)

    url = result["image_url"]
    assert url.startswith("https://agent.example/s/") and url.endswith("/image.jpg")
    token = url.split("/")[-2]
    shared = links.get(token)
    online = Image.open(BytesIO(shared.data))
    assert online.format == "JPEG" and not online.getexif()  # no GPS, no camera
    assert unquote(result["search"]["google_lens"]).endswith(url)
    assert set(result["search"]) == {"google_lens", "yandex", "bing", "tineye"}


async def test_only_a_photo_the_user_sent_goes_online(ctx):
    from intelligent_home_agent.shared_links import SharedLinks

    ctx.conversation.files["attached.jpg"] = Attachment(
        "attached.jpg", "image/jpeg", photo(), "mail"
    )
    async with httpx.AsyncClient() as http:
        osint = {
            t.name: t
            for t in build_osint_tools(Investigator(http), "https://agent.example", SharedLinks())
        }
        with pytest.raises(ToolError, match="came from mail"):
            await osint["reverse_image_search"].handler({"file_name": "attached.jpg"}, ctx)
        assert "reverse_image_search" not in {
            t.name for t in build_osint_tools(Investigator(http))
        }  # no public address configured


async def test_what_the_tools_read_is_others_words_and_nothing_private(ctx):
    from intelligent_home_agent.tools import ToolRegistry

    async with httpx.AsyncClient() as http:
        investigator = Investigator(http, resolve_dns=False)
        found = build_osint_tools(investigator, "https://agent.example")
        assert build_osint_tools(None, "https://agent.example") == []
    by_name = {t.name: t for t in found}
    for name in ("inspect_file", "domain_lookup", "osm_lookup"):
        assert by_name[name].reads_web and not by_name[name].taints
    registry = ToolRegistry(found)
    ctx.conversation.files["photo.jpg"] = Attachment("photo.jpg", "image/jpeg", photo(gps=False))
    await registry.run("inspect_file", {"file_name": "photo.jpg"}, ctx)
    with pytest.raises(ToolError, match="No kept image named absent.jpg. Kept files: photo.jpg."):
        await registry.run("reverse_image_search", {"file_name": "absent.jpg"}, ctx)


async def test_a_place_query_repeating_what_the_request_holds_does_not_go_out(ctx):
    ctx.leaks.add("The alarm code is 48151623.")
    async with httpx.AsyncClient() as http:
        osint = tools(http)
        with pytest.raises(ToolError, match="48151623"):
            await osint["osm_lookup"].handler({"query": "48151623 Main Street"}, ctx)
