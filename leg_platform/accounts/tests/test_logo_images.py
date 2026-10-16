"""The validation of uploaded logo files (D-073): PNG and SVG are accepted exactly as they are or
rejected; the type comes from the content; no file is ever rewritten.

No database is needed. The SVG corpus covers the attacks of docs/SECURITY.md, section 6: script,
event handlers, foreignObject, external references, ``use``, style, entity expansion ("billion
laughs"), XXE, DOCTYPE, CDATA, processing instructions, depth, size and polyglots."""

import hashlib
import struct
import time
import zlib

from django.test import SimpleTestCase
from django.utils.translation import override

from leg_platform.accounts.logo_images import (
    PNG_MAX_BYTES,
    SVG_MAX_BYTES,
    LogoRejected,
    _expected_raw_size,
    validate_logo,
)

SIGNATURE = b"\x89PNG\r\n\x1a\n"


def chunk(kind, body=b""):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def raw_size(width, height, bits_per_pixel):
    return height * (1 + (width * bits_per_pixel + 7) // 8)


def make_png(
    width=64,
    height=64,
    colour_type=2,
    bit_depth=8,
    interlace=0,
    before_data=(),
    after_data=(),
    image=None,
    trailer=b"",
    end=True,
    split=False,
):
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[colour_type]
    header = struct.pack(">IIBBBBB", width, height, bit_depth, colour_type, 0, 0, interlace)
    if image is None:
        if interlace:
            size = 0
            for x0, y0, dx, dy in ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
                                   (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2)):
                w, h = -(-(width - x0) // dx), -(-(height - y0) // dy)
                size += raw_size(w, h, channels * bit_depth) if w > 0 and h > 0 else 0
        else:
            size = raw_size(width, height, channels * bit_depth)
        image = bytes(size)
    compressed = zlib.compress(image)
    parts = [SIGNATURE, chunk(b"IHDR", header), *before_data]
    if split:
        half = len(compressed) // 2
        parts += [chunk(b"IDAT", compressed[:half]), chunk(b"IDAT", compressed[half:])]
    else:
        parts.append(chunk(b"IDAT", compressed))
    parts += list(after_data)
    if end:
        parts.append(chunk(b"IEND"))
    return b"".join(parts) + trailer


def svg(body="", attributes="", header=""):
    return (
        f'{header}<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 40"{attributes}>{body}</svg>'
    ).encode()


MINIMAL = svg('<path d="M0 0h100v40H0z" fill="#005BA2"/>')
# The ancillary chunks that are accepted before the data (L-3): gamma, chromaticities, sRGB, size.
DISPLAY_CHUNKS = [
    chunk(b"gAMA", bytes(4)),
    chunk(b"cHRM", bytes(32)),
    chunk(b"sRGB", b"\x00"),
    chunk(b"pHYs", bytes(9)),
]


def rows_with_filters(rows, stride, filters):
    """The raw data of ``rows`` rows of ``stride`` bytes whose filter bytes cycle through
    ``filters``."""
    return b"".join(bytes([filters[index % len(filters)]]) + bytes(stride - 1) for index in range(rows))


class ValidPngTests(SimpleTestCase):
    def test_a_valid_png_is_accepted_unchanged_with_its_digest(self):
        data = make_png()

        logo = validate_logo(data)

        self.assertEqual(logo.kind, "png")
        self.assertEqual(logo.data, data)
        self.assertEqual(logo.sha256, hashlib.sha256(data).hexdigest())
        self.assertEqual(logo.size, len(data))

    def test_the_colour_types_depths_and_interlacing_are_accepted(self):
        palette = [chunk(b"PLTE", bytes(range(0, 12)))]
        for label, kwargs in (
            ("rgba", {"colour_type": 6, "width": 720, "height": 200}),
            ("grey 16 bit", {"colour_type": 0, "bit_depth": 16}),
            ("grey and alpha", {"colour_type": 4}),
            ("palette", {"colour_type": 3, "bit_depth": 4, "before_data": palette}),
            ("interlaced", {"colour_type": 2, "interlace": 1, "width": 100, "height": 70}),
            ("data in two chunks", {"split": True}),
            ("widest", {"width": 4000, "height": 64, "colour_type": 0}),
            ("the most pixels", {"width": 4000, "height": 1000, "colour_type": 0}),
            ("the display chunks", {"before_data": DISPLAY_CHUNKS}),
            ("transparency of a grey image", {"colour_type": 0, "before_data": [chunk(b"tRNS", bytes(2))]}),
            ("transparency of an rgb image", {"before_data": [chunk(b"tRNS", bytes(6))]}),
            (
                "transparency and background of a palette image",
                {
                    "colour_type": 3,
                    "bit_depth": 4,
                    "before_data": [chunk(b"PLTE", bytes(12)), chunk(b"tRNS", bytes(3)), chunk(b"bKGD", b"\x01")],
                },
            ),
            ("the background of a grey and alpha image", {"colour_type": 4, "before_data": [chunk(b"bKGD", bytes(2))]}),
            ("the filter types 0 to 4", {"image": rows_with_filters(64, 193, (0, 1, 2, 3, 4))}),
        ):
            with self.subTest(label):
                self.assertEqual(validate_logo(make_png(**kwargs)).kind, "png")

    def test_the_worst_accepted_file_is_checked_in_well_under_a_few_seconds(self):
        # 4 million pixels of 16-bit RGBA inflate to 32 MB (D-073, L-4); measured at about 0.2 s
        # on the development machine, the bound here only catches a regression to quadratic time.
        data = make_png(width=4000, height=1000, colour_type=6, bit_depth=16)
        started = time.monotonic()

        self.assertEqual(validate_logo(data).kind, "png")

        self.assertLess(time.monotonic() - started, 5)


class InvalidPngTests(SimpleTestCase):
    def rejected(self, data):
        with self.assertRaises(LogoRejected) as caught:
            validate_logo(data)
        return caught.exception

    def test_a_wrong_signature_is_not_a_png(self):
        for data in (b"\xff\xd8\xff\xe0JFIF", b"GIF89a....", b"%PDF-1.7", b"", b"x" * 100, b"\x89PNG\r\n"):
            with self.subTest(data=data[:8]):
                self.assertIn("not a PNG or SVG", self.rejected(data).reason)

    def test_a_png_that_is_cut_off_or_changed_is_refused(self):
        valid = make_png()
        for label, data in (
            ("cut in the end chunk", valid[:-5]),
            ("cut after the header", valid[:40]),
            ("only the signature", SIGNATURE),
            ("a changed byte in the image data", valid[:60] + bytes([valid[60] ^ 0xFF]) + valid[61:]),
            ("a changed header", valid[:20] + b"\x01" + valid[21:]),
        ):
            with self.subTest(label):
                self.assertEqual(self.rejected(data).reason.split(":")[0], "png")

    def test_a_wrong_checksum_is_refused(self):
        valid = bytearray(make_png())
        valid[-1] ^= 0xFF  # the CRC of IEND

        self.assertIn("checksum", self.rejected(bytes(valid)).reason)

    def test_bytes_after_the_end_chunk_make_a_polyglot_and_are_refused(self):
        for trailer in (b"<script>alert(1)</script>", b"GIF89a", b"\x00", b"PK\x03\x04", b"<svg/>"):
            with self.subTest(trailer=trailer):
                self.assertIn("after the end chunk", self.rejected(make_png(trailer=trailer)).reason)

    def test_the_size_limits(self):
        for width, height in ((63, 64), (64, 63), (4001, 64), (64, 4001), (1, 1)):
            with self.subTest(size=(width, height)):
                error = self.rejected(make_png(width=width, height=height, image=b"\x00"))
                self.assertEqual(error.reason, "png: size out of range")
                with override("de-ch"):
                    self.assertEqual(
                        str(error.message),
                        "Das Bild muss zwischen 64 und 4000 Pixel breit und hoch sein.",
                    )

    def test_more_than_four_million_pixels_are_refused_before_the_data_is_read(self):
        for width, height in ((4000, 1001), (4000, 4000), (2001, 2000)):
            with self.subTest(size=(width, height)):
                error = self.rejected(make_png(width=width, height=height, image=b"\x00"))
                self.assertEqual(error.reason, "png: too many pixels")
        with override("de-ch"):
            self.assertIn("zu viele Pixel", str(error.message))

    def test_metadata_chunks_are_refused_before_and_after_the_data(self):
        for kind, body in (
            (b"tEXt", b"Comment\x00hello"),
            (b"zTXt", b"k\x00\x00x"),
            (b"iTXt", b"k\x00\x00\x00\x00\x00v"),
            (b"eXIf", b"MM\x00*"),
            (b"iCCP", b"p\x00\x00x"),
            (b"tIME", bytes(7)),
            (b"sBIT", bytes(3)),
            (b"prVt", b"private"),
        ):
            for label, kwargs in (("before", "before_data"), ("after", "after_data")):
                with self.subTest(kind=kind, place=label):
                    error = self.rejected(make_png(**{kwargs: [chunk(kind, body)]}))
                    self.assertEqual(error.reason, "png: chunk not allowed")
        with override("de-ch"):
            self.assertIn("Metadaten", str(error.message))

    def test_a_png_with_markup_in_a_text_chunk_is_refused(self):
        # The file would be served as image/png with nosniff, but it is no reason to keep data
        # that is not part of the image (security review of D-073, L-3).
        data = make_png(before_data=[chunk(b"tEXt", b"x\x00<svg onload=alert(1)>")])

        self.assertEqual(self.rejected(data).reason, "png: chunk not allowed")

    def test_the_accepted_ancillary_chunks_must_be_well_formed_and_before_the_data(self):
        palette = chunk(b"PLTE", bytes(12))
        for label, kwargs in (
            ("gAMA with a wrong length", {"before_data": [chunk(b"gAMA", bytes(3))]}),
            ("pHYs with a wrong length", {"before_data": [chunk(b"pHYs", bytes(10))]}),
            ("sRGB twice", {"before_data": [chunk(b"sRGB", b"\x00"), chunk(b"sRGB", b"\x00")]}),
            ("pHYs after the data", {"after_data": [chunk(b"pHYs", bytes(9))]}),
            ("tRNS of an image with alpha", {"colour_type": 6, "before_data": [chunk(b"tRNS", bytes(8))]}),
            ("tRNS of an rgb image with a wrong length", {"before_data": [chunk(b"tRNS", bytes(2))]}),
            ("tRNS of a palette image with too many entries",
             {"colour_type": 3, "bit_depth": 4, "before_data": [palette, chunk(b"tRNS", bytes(5))]}),
            ("tRNS of a palette image before the palette",
             {"colour_type": 3, "bit_depth": 4, "before_data": [chunk(b"tRNS", bytes(1)), palette]}),
            ("bKGD with a wrong length", {"before_data": [chunk(b"bKGD", bytes(2))]}),
        ):
            with self.subTest(label), self.assertRaises(LogoRejected) as caught:
                validate_logo(make_png(**kwargs))
            self.assertTrue(caught.exception.reason.startswith("png: ancillary chunk"), caught.exception.reason)

    def test_a_filter_type_above_four_is_refused(self):
        for label, kwargs in (
            ("first row", {"image": rows_with_filters(64, 193, (5,))}),
            ("a later row", {"image": rows_with_filters(64, 193, (0, 0, 0, 0, 0, 0, 0, 255))}),
            ("interlaced", {"interlace": 1, "image": b"\x05" + bytes(_expected_raw_size(64, 64, 24, True) - 1)}),
        ):
            with self.subTest(label):
                self.assertEqual(self.rejected(make_png(**kwargs)).reason, "png: filter type")

    def test_a_file_above_one_megabyte_is_too_large(self):
        data = SIGNATURE + b"\x00" * PNG_MAX_BYTES

        error = self.rejected(data)

        self.assertEqual(error.reason, "png: too large")
        self.assertEqual(error.params, {"max": "1 MB"})

    def test_the_chunks_must_be_complete_and_in_order(self):
        for label, data in (
            ("no end chunk", make_png(end=False)),
            ("no data", SIGNATURE + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 64, 8, 2, 0, 0, 0)) + chunk(b"IEND")),
            ("a second header", make_png(before_data=[chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 64, 8, 2, 0, 0, 0))])),
            ("an unknown critical chunk", make_png(before_data=[chunk(b"ABCD", b"x")])),
            ("data chunks apart", make_split_apart()),
            ("an end chunk with data", make_png(end=False) + chunk(b"IEND", b"x")),
            ("a palette missing for a palette image", make_png(colour_type=3, bit_depth=8, image=bytes(64 * 65))),
            ("a palette for a grey image", make_png(colour_type=0, before_data=[chunk(b"PLTE", bytes(3))])),
            ("an invalid chunk name", make_png(before_data=[chunk(b"ab1d", b"x")])),
            ("an invalid colour type and depth", make_png(colour_type=2, bit_depth=4, image=bytes(64 * 100))),
        ):
            with self.subTest(label), self.assertRaises(LogoRejected):
                validate_logo(data)

    def test_the_image_data_must_have_exactly_the_size_of_the_header(self):
        for label, image in (("too short", bytes(64 * 193 - 1)), ("too long", bytes(64 * 193 + 1)), ("empty", b"")):
            with self.subTest(label), self.assertRaises(LogoRejected):
                validate_logo(make_png(image=image))

    def test_data_that_is_no_zlib_stream_is_refused(self):
        data = SIGNATURE + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 64, 8, 2, 0, 0, 0)) + chunk(
            b"IDAT", b"this is not a zlib stream"
        ) + chunk(b"IEND")

        self.assertIn("inflated", self.rejected(data).reason)

    def test_a_decompression_bomb_is_refused_without_inflating_it(self):
        # About 5 MB of zeros compress to a few kB; the header says 64 x 64.
        started = time.monotonic()

        error = self.rejected(make_png(image=bytes(5_000_000)))

        self.assertIn("larger than the header says", error.reason)
        self.assertLess(time.monotonic() - started, 5)

    def test_the_reason_never_holds_a_part_of_the_file(self):
        marker = b"SECRET-SENTINEL"
        error = self.rejected(make_png(before_data=[chunk(b"ABCD", marker)]))

        self.assertNotIn("SENTINEL", error.reason)
        self.assertNotIn("SENTINEL", str(error.params))


def make_split_apart():
    """Two data chunks with another chunk between them: not one run of data."""
    compressed = zlib.compress(bytes(64 * 193))
    half = len(compressed) // 2
    return (
        SIGNATURE
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 64, 64, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", compressed[:half])
        + chunk(b"pHYs", bytes(9))
        + chunk(b"IDAT", compressed[half:])
        + chunk(b"IEND")
    )


class ValidSvgTests(SimpleTestCase):
    def test_a_minimal_svg_is_accepted_unchanged(self):
        logo = validate_logo(MINIMAL)

        self.assertEqual(logo.kind, "svg")
        self.assertEqual(logo.data, MINIMAL)
        self.assertEqual(logo.sha256, hashlib.sha256(MINIMAL).hexdigest())

    def test_a_typical_logo_with_gradient_clip_path_title_style_and_declaration_is_accepted(self):
        body = (
            "<title>Logo</title><desc>A logo</desc><defs>"
            '<linearGradient id="g1" x1="0" y1="0" x2="1" y2="0" gradientUnits="objectBoundingBox">'
            '<stop offset="0" stop-color="#005BA2"/><stop offset="1" style="stop-color:#6AA9E0;stop-opacity:0.8"/>'
            '</linearGradient><clipPath id="c1"><rect width="50" height="20"/></clipPath></defs>'
            '<g transform="translate(1 2) scale(1.5)" clip-path="url(#c1)">'
            '<path d="M0 0L10 10C20 20 30 30 40 40Z" fill="url(#g1)" style="stroke:#000;stroke-width:2"/>'
            '<circle cx="5" cy="5" r="3" fill="rgb(10, 20, 30)"/><ellipse cx="1" cy="1" rx="2" ry="3"/>'
            '<line x1="0" y1="0" x2="5" y2="5" stroke="currentColor"/>'
            '<polyline points="0,0 10,10"/><polygon points="0,0 10,10 0,10"/></g>'
        )
        data = svg(body, ' width="100%" height="40" preserveAspectRatio="xMidYMid meet"',
                   header='<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n<!-- Generator: an editor -->\n')

        self.assertEqual(validate_logo(data).kind, "svg")

    def test_a_byte_order_mark_and_unused_namespace_declarations_are_accepted(self):
        data = b"\xef\xbb\xbf" + svg("<g/>", ' xmlns:xlink="http://www.w3.org/1999/xlink"')

        self.assertEqual(validate_logo(data).kind, "svg")

    def test_the_type_comes_from_the_content_and_not_from_a_name(self):
        # There is no name in the check at all: a PNG is a PNG and an SVG is an SVG.
        self.assertEqual(validate_logo(make_png()).kind, "png")
        self.assertEqual(validate_logo(MINIMAL).kind, "svg")


class InvalidSvgTests(SimpleTestCase):
    def rejected(self, data):
        with self.assertRaises(LogoRejected) as caught:
            validate_logo(data if isinstance(data, bytes) else data.encode())
        return caught.exception

    def test_scripts_handlers_and_embedded_content_are_refused(self):
        for label, body, attributes in (
            ("script", "<script>alert(1)</script>", ""),
            ("script in upper case", "<SCRIPT>alert(1)</SCRIPT>", ""),
            ("an event handler on the root", "", ' onload="alert(1)"'),
            ("an event handler on an element", '<g onclick="alert(1)"/>', ""),
            ("foreignObject", "<foreignObject><div>x</div></foreignObject>", ""),
            ("image", '<image href="x.png"/>', ""),
            ("an external image through use", '<use href="https://evil.example/x.svg#a"/>', ""),
            ("a link", '<a href="javascript:alert(1)"><g/></a>', ""),
            ("a style element", "<style>@import url(https://evil.example/x.css);</style>", ""),
            ("an animation", '<set attributeName="onload" to="alert(1)"/>', ""),
            ("an animate element", '<animate attributeName="href" values="javascript:alert(1)"/>', ""),
            ("a filter", '<filter id="f"/>', ""),
            ("text", "<text>x</text>", ""),
            ("a nested svg", "<svg><g/></svg>", ""),
            ("html in the svg namespace", "<div>x</div>", ""),
            ("a marker", "<marker/>", ""),
            ("a pattern", "<pattern/>", ""),
            ("a symbol", "<symbol/>", ""),
            ("a mask", "<mask/>", ""),
            ("metadata", "<metadata>x</metadata>", ""),
            ("an href attribute", '<path d="M0 0" href="https://evil.example/"/>', ""),
            ("a class attribute", '<g class="a"/>', ""),
            ("a data attribute", '<g data-x="1"/>', ""),
            ("xml:space", '<g xml:space="preserve"/>', ""),
        ):
            with self.subTest(label):
                self.assertEqual(self.rejected(svg(body, attributes)).reason.split(":")[0], "svg")

    def test_namespaced_content_of_editors_is_refused(self):
        for label, data in (
            (
                "an inkscape element",
                '<svg xmlns="http://www.w3.org/2000/svg" xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd" '
                'viewBox="0 0 1 1"><sodipodi:namedview id="n"/></svg>',
            ),
            (
                "an inkscape attribute",
                '<svg xmlns="http://www.w3.org/2000/svg" xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
                'viewBox="0 0 1 1"><g inkscape:label="x"/></svg>',
            ),
            (
                "an xlink attribute",
                '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
                'viewBox="0 0 1 1"><path xlink:href="#a"/></svg>',
            ),
            ("no namespace", '<svg viewBox="0 0 1 1"><g/></svg>'),
            ("another namespace", '<svg xmlns="http://www.w3.org/1999/xhtml" viewBox="0 0 1 1"/>'),
            (
                "a nested namespace",
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><g xmlns="http://www.w3.org/1999/xhtml"/></svg>',
            ),
        ):
            with self.subTest(label):
                self.assertEqual(self.rejected(data).reason.split(":")[0], "svg")

    def test_references_and_values_are_checked(self):
        for label, body in (
            ("an external url", '<path fill="url(https://evil.example/x.svg#a)"/>'),
            ("a url without a defined id", '<path fill="url(#missing)"/>'),
            ("a url with quotes", '<path fill="url(\'#a\')"/><g id="a"/>'),
            ("a url with spaces", '<path fill="url (#a)"/><g id="a"/>'),
            ("a url in a style value", '<path style="fill:url(//evil.example/a)"/>'),
            ("a javascript value", '<path fill="javascript:alert(1)"/>'),
            ("an expression", '<path style="fill:expression(alert(1))"/>'),
            ("an unknown style property", '<path style="behavior:url(#a)"/>'),
            ("a style without a property", '<path style="red"/>'),
            ("an import in a style", '<path style="fill:red;@import url(x)"/>'),
            ("a backslash", '<path fill="\\72 ed"/>'),
            ("a markup character in a value", '<path fill="&lt;x&gt;"/>'),
            ("a quote in a value", "<path fill=\"a'b\"/>"),
            ("a colon in a value", '<path fill="a:b"/>'),
            ("an id that is no identifier", '<g id="1 bad"/>'),
        ):
            with self.subTest(label):
                self.assertEqual(self.rejected(svg(body)).reason.split(":")[0], "svg")

    def test_a_document_type_declaration_is_refused_before_any_parser_sees_it(self):
        for label, prefix in (
            ("a plain doctype", "<!DOCTYPE svg>"),
            ("a lower case doctype", "<!doctype svg>"),
            ("an entity", '<!DOCTYPE svg [<!ENTITY a "b">]>'),
            ("xxe", '<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'),
            ("xxe with a parameter entity", '<!DOCTYPE svg [<!ENTITY % p SYSTEM "http://evil.example/x.dtd"> %p;]>'),
            ("an external dtd", '<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://evil.example/svg.dtd">'),
            ("an entity declaration alone", '<!ENTITY a "b">'),
        ):
            with self.subTest(label):
                data = prefix.encode() + svg('<g id="&a;"/>' if "ENTITY" in prefix.upper() else "<g/>")
                self.assertEqual(self.rejected(data).reason, "svg: doctype or entity")

    def test_entity_expansion_is_never_run(self):
        laughs = ['<!DOCTYPE svg [<!ENTITY a0 "x">'] + [
            f'<!ENTITY a{n} "&a{n - 1};&a{n - 1};&a{n - 1};&a{n - 1};&a{n - 1};&a{n - 1};&a{n - 1};&a{n - 1};&a{n - 1};&a{n - 1};">'
            for n in range(1, 10)
        ] + ["]>"]
        data = "".join(laughs).encode() + svg('<title>&a9;</title>')
        started = time.monotonic()

        error = self.rejected(data)

        self.assertEqual(error.reason, "svg: doctype or entity")
        self.assertLess(time.monotonic() - started, 1)

    def test_other_markup_that_a_parser_could_misuse_is_refused(self):
        for label, data, reason in (
            ("a cdata section", svg("<title><![CDATA[x]]></title>"), "svg: CDATA section"),
            ("a style sheet instruction", b'<?xml-stylesheet href="x.css"?>' + MINIMAL, "svg: processing instruction"),
            ("an instruction in the body", svg("<?php echo 1 ?>"), "svg: processing instruction"),
            ("a second declaration", b'<?xml version="1.0"?><?xml version="1.0"?>' + MINIMAL, "svg: processing instruction"),
            ("another encoding", b'<?xml version="1.0" encoding="ISO-8859-1"?>' + MINIMAL, "svg: processing instruction"),
            ("a declaration after white space", b'\n<?xml version="1.0"?>' + MINIMAL, "svg: processing instruction"),
        ):
            with self.subTest(label):
                self.assertEqual(self.rejected(data).reason, reason)

    def test_the_encoding_must_be_utf_8_without_nul(self):
        for label, data in (
            ("latin-1 bytes", svg("<title>\xe9</title>").decode().encode("latin-1")),
            ("utf-16", MINIMAL.decode().encode("utf-16")),
            ("a nul character", svg("<title>a\x00b</title>")),
        ):
            with self.subTest(label):
                self.assertEqual(self.rejected(data).reason.split(":")[0], "svg" if label != "utf-16" else "file")

    def test_the_limits_of_size_depth_and_elements(self):
        too_big = svg("<g/>" * (SVG_MAX_BYTES // 4))
        deep = svg("<g>" * 25 + "</g>" * 25)
        deepest = svg("<g>" * 15000 + "</g>" * 15000)
        many = svg("<g/>" * 2001)
        for label, data, reason in (
            ("a file above 200 KB", too_big, "svg: too large"),
            ("nested 26 levels", deep, "svg: too deep"),
            ("nested 15000 levels", deepest, "svg: too deep"),
            ("2002 elements", many, "svg: too many elements"),
        ):
            with self.subTest(label):
                self.assertEqual(self.rejected(data).reason, reason)
        self.assertEqual(validate_logo(svg("<g/>" * 1990)).kind, "svg")
        self.assertEqual(validate_logo(svg("<g>" * 18 + "</g>" * 18)).kind, "svg")

    def test_a_view_box_is_needed_and_must_have_a_size(self):
        for label, data in (
            ("none", b'<svg xmlns="http://www.w3.org/2000/svg"><g/></svg>'),
            ("empty", b'<svg xmlns="http://www.w3.org/2000/svg" viewBox=""><g/></svg>'),
            ("three numbers", b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10"><g/></svg>'),
            ("no width", b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 0 10"><g/></svg>'),
            ("words", b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="a b c d"><g/></svg>'),
        ):
            with self.subTest(label):
                error = self.rejected(data)
                self.assertEqual(error.reason, "svg: no viewBox")
                with override("de-ch"):
                    self.assertEqual(str(error.message), "Das SVG braucht eine viewBox, damit es skaliert werden kann.")

    def test_not_well_formed_files_are_refused(self):
        for label, data in (
            ("an unclosed element", b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><g></svg>'),
            ("two roots", MINIMAL + MINIMAL),
            ("text only", b"<not xml"),
            ("a html page", b"<html><body><script>alert(1)</script></body></html>"),
            ("text in an element", svg("<g>text</g>")),
            ("text between elements", svg("<g/>text<g/>")),
            ("a long title", svg("<title>" + "x" * 501 + "</title>")),
            ("an element in a title", svg("<title><g/></title>")),
        ):
            with self.subTest(label):
                self.assertEqual(self.rejected(data).reason.split(":")[0], "svg")

    def test_other_files_are_not_png_or_svg(self):
        for data in (b"plain text", b"\x00\x01\x02", b"PK\x03\x04zip", b"   x", b""):
            with self.subTest(data=data):
                error = self.rejected(data)
                self.assertEqual(error.reason, "file: not a PNG or SVG file")
                with override("de-ch"):
                    self.assertEqual(str(error.message), "Es sind nur PNG- und SVG-Dateien erlaubt.")

    def test_the_text_for_the_person_names_only_a_sanitised_element_or_attribute(self):
        error = self.rejected(svg("<script>alert(1)</script>"))
        self.assertEqual(error.params, {"what": "script"})
        with override("de-ch"):
            text = str(error.message) % error.params
        self.assertIn("(script)", text)
        self.assertIn("«einfaches SVG»", text)
        # A name with other characters than letters is shown as "unknown".
        self.assertEqual(self.rejected(svg("<h1/>")).params, {"what": "unknown"})
        self.assertEqual(self.rejected(svg("<g data-x1='1'/>")).params, {"what": "unknown"})

    def test_the_reason_and_the_text_never_hold_content_of_the_file(self):
        error = self.rejected(svg("<script>SECRET-SENTINEL</script>"))

        self.assertNotIn("SENTINEL", error.reason)
        self.assertNotIn("SENTINEL", str(error.params))
