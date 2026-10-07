"""Small synthetic PDF fixtures with a Unicode text layer; no private customer documents."""
import io

from pypdf import PdfWriter
from pypdf.generic import (ArrayObject, DecodedStreamObject, DictionaryObject,
                           NameObject, NumberObject, TextStringObject)


def text_pdf(texts=None, padding=0):
    texts = texts or ["한국어 고객 대시보드\n매출 120\n방문자 42", "두 번째 페이지\n검증 표본 AX PDF"]
    writer = PdfWriter()
    chars = sorted(set("".join(texts)) - {"\n"})
    cmap = ("/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
            "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
            "/CMapName /AXFixture def\n/CMapType 2 def\n"
            "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
            f"{len(chars)} beginbfchar\n" +
            "\n".join(f"<{ord(c):04X}> <{ord(c):04X}>" for c in chars) +
            "\nendbfchar\nendcmap\nCMapName currentdict /CMap defineresource pop\nend\nend")
    unicode_map = DecodedStreamObject()
    unicode_map.set_data(cmap.encode("ascii"))
    descendant = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/CIDFontType0"),
        NameObject("/BaseFont"): NameObject("/HYSMyeongJo-Medium"),
        NameObject("/CIDSystemInfo"): DictionaryObject({
            NameObject("/Registry"): TextStringObject("Adobe"),
            NameObject("/Ordering"): TextStringObject("Identity"),
            NameObject("/Supplement"): NumberObject(0),
        }),
    })
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type0"),
        NameObject("/BaseFont"): NameObject("/HYSMyeongJo-Medium"),
        NameObject("/Encoding"): NameObject("/Identity-H"),
        NameObject("/DescendantFonts"): ArrayObject([writer._add_object(descendant)]),
        NameObject("/ToUnicode"): writer._add_object(unicode_map),
    })
    font_ref = writer._add_object(font)
    for text in texts:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})})
        commands = []
        for index, line in enumerate(text.splitlines()):
            # Header, two columns and footer intentionally use separate positioned text blocks.
            x, y = (40, 730) if index == 0 else (40 + (index % 2) * 260, 670 - index * 50)
            commands.append(f"BT /F1 16 Tf {x} {y} Td <{line.encode('utf-16-be').hex()}> Tj ET")
        stream = DecodedStreamObject()
        stream.set_data("\n".join(commands).encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    if padding:
        writer.add_metadata({"/SyntheticPadding": "x" * padding})
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def image_pdf():
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    image = DecodedStreamObject()
    image.set_data(b"\xff\xff\xff")
    image.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
                  NameObject("/Width"): NumberObject(1), NameObject("/Height"): NumberObject(1),
                  NameObject("/ColorSpace"): NameObject("/DeviceRGB"), NameObject("/BitsPerComponent"): NumberObject(8)})
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/XObject"): DictionaryObject({NameObject("/Im1"): writer._add_object(image)})})
    stream = DecodedStreamObject()
    stream.set_data(b"q 100 0 0 100 40 500 cm /Im1 Do Q")
    page[NameObject("/Contents")] = writer._add_object(stream)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
