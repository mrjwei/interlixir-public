"""Context file text extraction for each accepted extension, and unreadable files."""

import io
import unittest
import zipfile

import extract

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def docx(*paragraphs):
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = f'<?xml version="1.0"?><w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


def pdf(text):
    """A one-page PDF with one line of text, built by hand (offsets computed so the xref table is valid)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return out


class TestExtract(unittest.TestCase):
    def test_text_and_markdown(self):
        self.assertEqual(extract.extract("txt", "﻿Hello\r\nworld  \n\n\n\nbye".encode()), "Hello\nworld\n\nbye")
        self.assertEqual(extract.extract("md", b"# Guide\n- ask"), "# Guide\n- ask")

    def test_docx(self):
        self.assertEqual(extract.extract("docx", docx("Discussion guide", "Ask about checkout")),
                         "Discussion guide\nAsk about checkout")

    def test_pdf(self):
        self.assertIn("Ask about checkout", extract.extract("pdf", pdf("Ask about checkout")))

    def test_unreadable(self):
        for ext, data in (("pdf", b"not a pdf"), ("docx", b"not a zip"), ("docx", b""), ("rtf", b"{\\rtf}")):
            with self.subTest(ext=ext, data=data[:8]):
                with self.assertRaises(extract.Unreadable):
                    extract.extract(ext, data)


if __name__ == "__main__":
    unittest.main()
