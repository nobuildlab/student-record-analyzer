"""PDF → 텍스트 추출 (텍스트PDF + 스캔본OCR 폴백)"""

import fitz  # PyMuPDF

MIN_TEXT_LENGTH = 500  # 이보다 짧으면 스캔본으로 판단


def extract_text_from_pdf(pdf_path: str) -> str:
    """PDF에서 텍스트를 추출한다.
    텍스트 PDF면 PyMuPDF로 바로 추출,
    스캔본(텍스트 < 500자)이면 EasyOCR 폴백.
    """
    doc = fitz.open(pdf_path)
    text_parts: list[str] = []

    for page in doc:
        text_parts.append(page.get_text())

    doc.close()
    full_text = "\n".join(text_parts).strip()

    if len(full_text) >= MIN_TEXT_LENGTH:
        return full_text

    # 스캔본 → OCR 폴백
    return _ocr_fallback(pdf_path)


def _ocr_fallback(pdf_path: str) -> str:
    """EasyOCR로 스캔 PDF 텍스트 추출."""
    import easyocr

    reader = easyocr.Reader(["ko", "en"], gpu=False)
    doc = fitz.open(pdf_path)
    all_text: list[str] = []

    for page_num in range(len(doc)):
        page = doc[page_num]
        pix = page.get_pixmap(dpi=300)
        img_bytes = pix.tobytes("png")

        results = reader.readtext(img_bytes, detail=0)
        all_text.append(f"\n--- {page_num + 1}페이지 ---\n")
        all_text.append("\n".join(results))

    doc.close()
    return "\n".join(all_text).strip()
