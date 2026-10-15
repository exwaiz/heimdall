"""Bounded local decoding only. OCR and understanding are performed by Codex.

This executable runs under the named offline extraction sandbox, never imports
the bridge database or credentials, and never follows remote document resources.
"""
import base64
import io
import json
import os
import resource
import sys
import zipfile
import xml.etree.ElementTree as ET


def extract(kind, mime, data):
    if kind == "audio":
        return {"unsupported": "audio_turn_input_unsupported"}
    if kind == "video":
        import av
        frames = []
        # Custom IO and protocol whitelist disallow external/network resources.
        with av.open(io.BytesIO(data), options={"protocol_whitelist": "file,pipe"}) as container:
            if not container.streams.video:
                raise ValueError()
            stream = container.streams.video[0]
            if stream.codec_context.width * stream.codec_context.height > 2073600:
                raise ValueError()
            stream.codec_context.thread_count = 1
            duration = min(float(container.duration or 10000000) / 1000000, 60)
            for seconds in (0, duration / 2, max(0, duration - .1)):
                container.seek(int(seconds * 1000000))
                for count, frame in enumerate(container.decode(stream)):
                    if count > 120:
                        break
                    if frame.time is not None and frame.time + .5 < seconds:
                        continue
                    if frame.width * frame.height > 2073600:
                        raise ValueError()
                    image = frame.to_image()
                    image.thumbnail((640, 480))
                    output = io.BytesIO()
                    image.convert("RGB").save(output, format="PNG")
                    frames.append(base64.b64encode(output.getvalue()).decode())
                    break
        if not frames:
            raise ValueError()
        return {"text": "영상의 첫 최대 60초에서 뽑은 최대 3개 프레임. 소리는 분석하지 않았습니다.", "images": frames}
    if mime in {"image/png", "image/jpeg"}:
        return {"text": "", "images": [base64.b64encode(data).decode()]}
    if mime == "application/pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted or len(reader.pages) > 50:
            raise ValueError()
        texts, images = [], []
        for page in reader.pages:
            text = page.extract_text() or ""
            texts.append(text)
            if sum(map(len, texts)) > 100000:
                raise ValueError()
            if not text.strip() and len(images) < 3:
                for embedded in page.images:
                    image = embedded.image
                    if image.width * image.height > 20000000:
                        raise ValueError()
                    image.thumbnail((1600, 1600))
                    output = io.BytesIO()
                    image.convert("RGB").save(output, format="PNG")
                    images.append(base64.b64encode(output.getvalue()).decode())
                    if len(images) == 3:
                        break
        return {"text": "\n".join(texts), "images": images}
    if mime == "application/zip":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > 200 or sum(i.file_size for i in infos) > 16000000 or any(i.file_size > 5000000 or i.file_size > max(1, i.compress_size) * 100 for i in infos):
                raise ValueError()
            if "word/document.xml" not in archive.namelist():
                return {"unsupported": "archive_not_docx"}
            xml = archive.read("word/document.xml")
            if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
                raise ValueError()
            root = ET.fromstring(xml)
            return {"text": "\n".join(node.text or "" for node in root.iter() if node.tag.endswith("}t"))[:100000], "images": []}
    if mime == "text/plain":
        return {"text": data.decode("utf-8")[:100000], "images": []}
    return {"unsupported": "document_format_unsupported"}


def main():
    os.nice(10)
    resource.setrlimit(resource.RLIMIT_AS, (768 * 1024**2, 768 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (35, 35))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    try:
        with open(sys.argv[3], "rb") as handle:
            data = handle.read(20000001)
        if len(data) > 20000000:
            raise ValueError()
        result = extract(sys.argv[1], sys.argv[2], data)
        output = json.dumps(result, ensure_ascii=False)
        if len(output.encode()) > 4000000:
            raise ValueError()
        print(output)
    except Exception:
        print('{"error":"extraction_failed"}')
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
