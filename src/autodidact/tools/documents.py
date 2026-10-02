# 文件职责：读取有大小/页数限制的 UTF-8 TXT/MD 或可选 PDF，导入不直接提升信念。
from pathlib import Path

from autodidact.schemas import SourceDocument


# 功能：校验本地文件并提取文本/页码等 metadata，返回初始等级 1 的 SourceDocument；扫描 OCR 不在此实现。
def read_local_document(path: str):
    source = Path(path).resolve(strict=True)
    if not source.is_file() or source.stat().st_size > 20_000_000:
        raise ValueError("本地文档必须是小于20MB的文件")
    suffix = source.suffix.lower()
    pages = []
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError("PDF导入需要安装项目的 .[documents] 可选依赖") from exc
        reader = PdfReader(source)
        if reader.is_encrypted:
            raise ValueError("请先由你正常解密PDF后再导入")
        if len(reader.pages) > 100:
            raise ValueError("单次导入最多100页，请先拆分较长PDF")
        for number, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ""
            pages.append({"page": number, "text": text})
        content = "\n\n".join(f"[第{p['page']}页]\n{p['text']}" for p in pages)
    elif suffix in {".txt", ".md", ".markdown"}:
        content = source.read_text(encoding="utf-8-sig")
    else:
        raise ValueError("支持 UTF-8 TXT、Markdown 和可提取文本的 PDF")
    if not content.strip():
        raise ValueError("文档没有可提取文本；扫描PDF需要先进行OCR")
    # Import is provenance, not a declaration that the local document is authoritative.
    return SourceDocument(
        url=source.as_uri(),
        normalized_url=source.as_uri(),
        lineage_key=source.as_uri(),
        title=source.name,
        text=content[:200000],
        source_type="local_document",
        quality_class="unreviewed_local_document",
        evidence_level=1,
        credibility_score=0.3,
        quality_reason="用户导入，尚未独立评审或核源",
        metadata={
            "local_path": str(source),
            "page_count": len(pages),
            "text_truncated": len(content) > 200000,
        },
    )
