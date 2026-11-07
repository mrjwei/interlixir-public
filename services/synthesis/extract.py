"""Plain text from a context file. The extensions are the API's CONTEXT_TYPES.

PDF uses pypdf (pure Python). DOCX is a zip of XML, so the standard library reads it: one line per paragraph
from word/document.xml, which skips headers, footers, comments and images. A file that can't be read raises
Unreadable, and the job carries on without it.
"""

import io
import re
import zipfile
import xml.etree.ElementTree as ET

EXTENSIONS = ("pdf", "docx", "md", "txt")
# Uncompressed size of word/document.xml we're willing to parse (the upload itself is at most 20 MiB).
MAX_DOCX_XML_BYTES = 50 * 2**20
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class Unreadable(Exception):
    pass


def extract(ext, data):
    try:
        if ext in ("md", "txt"):
            text = data.decode("utf-8-sig", errors="replace")
        elif ext == "pdf":
            text = _pdf(data)
        elif ext == "docx":
            text = _docx(data)
        else:
            raise Unreadable(f"unsupported extension .{ext}")
    except Unreadable:
        raise
    except Exception as error:  # a corrupt or encrypted file: any library error means "can't read it"
        raise Unreadable(f"{type(error).__name__}: {error}") from None
    return _tidy(text)


def _pdf(data):
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        raise Unreadable("the PDF is encrypted")
    return "\n\n".join(page.extract_text() or "" for page in reader.pages)


def _docx(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        info = archive.getinfo("word/document.xml")
        if info.file_size > MAX_DOCX_XML_BYTES:
            raise Unreadable("the document is too large to read")
        root = ET.fromstring(archive.read(info))
    paragraphs = []
    for paragraph in root.iter(f"{W}p"):
        parts = []
        for node in paragraph.iter():
            if node.tag == f"{W}t":
                parts.append(node.text or "")
            elif node.tag == f"{W}tab":
                parts.append("\t")
            elif node.tag in (f"{W}br", f"{W}cr"):
                parts.append("\n")
        paragraphs.append("".join(parts))
    return "\n".join(paragraphs)


def _tidy(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
