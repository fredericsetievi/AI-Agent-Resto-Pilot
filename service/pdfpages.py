"""Render PDF invoice pages to JPEG images for the vision pipeline.

StepFun vision accepts images only, so an invoice PDF is rendered locally
with PyMuPDF — no model, no network, and the same image budget as photo
intake. Caps keep a pathological PDF from flooding the vision calls: at
most five pages, each rendered JPEG within the intake image size limit.
"""

from __future__ import annotations

import pymupdf


MAX_PAGES = 5
MAX_PDF_BYTES = 10 * 1024 * 1024
MAX_PAGE_BYTES = 6 * 1024 * 1024
RENDER_DPI = 150


class PdfRenderError(Exception):
    """Safe message for the owner; never includes PDF internals."""


def render_pdf_pages(pdf_bytes):
    """Render each PDF page to JPEG bytes, capped and validated.

    Raises PdfRenderError with an owner-facing Indonesian message for
    unreadable, encrypted, oversized, or too-many-page documents.
    """
    if not pdf_bytes:
        raise PdfRenderError("Berkas PDF kosong.")
    if len(pdf_bytes) > MAX_PDF_BYTES:
        raise PdfRenderError(f"Berkas PDF melebihi {MAX_PDF_BYTES // (1024 * 1024)} MB.")
    try:
        document = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except Exception:  # noqa: BLE001 - any parser failure is the same polite error
        raise PdfRenderError("Berkas tidak bisa dibaca sebagai PDF.")
    try:
        if document.needs_pass:
            raise PdfRenderError("PDF terkunci dengan password; kirim versi tanpa password.")
        if document.page_count == 0:
            raise PdfRenderError("PDF tidak memiliki halaman.")
        if document.page_count > MAX_PAGES:
            raise PdfRenderError(f"PDF lebih dari {MAX_PAGES} halaman; pisahkan atau kirim halaman utamanya saja.")
        pages = []
        for page in document:
            pixmap = page.get_pixmap(dpi=RENDER_DPI)
            data = pixmap.tobytes("jpeg")
            if len(data) > MAX_PAGE_BYTES:
                raise PdfRenderError(f"Halaman terlalu besar untuk dibaca setelah dirender.")
            pages.append(data)
    finally:
        document.close()
    if not pages:
        raise PdfRenderError("PDF tidak memiliki halaman.")
    return pages
