"""Real Codex knowledge smoke in a disposable DB. No Telegram credentials."""
import asyncio
import io
import json
import os
from pathlib import Path
import tempfile
from dataclasses import replace

from PIL import Image, ImageDraw, ImageFont
import av

from heimdall_bridge.__main__ import update
from heimdall_bridge.bridge import Bridge
from heimdall_bridge.codex import CodexAdapter
from heimdall_bridge.config import Config
from heimdall_bridge.db import Store
from heimdall_bridge.extraction import prepare
from heimdall_bridge.knowledge_backup import backup_bundle, restore_bundle
from heimdall_bridge.transport import FakeTelegramTransport

ROOT = Path(__file__).resolve().parents[1]


def receipt():
    image = Image.new("RGB", (850, 180), "white")
    draw = ImageDraw.Draw(image)
    draw.text((30, 40), "HEIMDALL RECEIPT 73000", fill="black", font=ImageFont.load_default(size=48))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def video():
    buffer = io.BytesIO()
    with av.open(buffer, "w", format="mp4") as container:
        stream = container.add_stream("mpeg4", rate=10)
        stream.width, stream.height, stream.pix_fmt = 96, 64, "yuv420p"
        for _ in range(10):
            frame = av.VideoFrame.from_image(Image.new("RGB", (96,64), "red"))
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return buffer.getvalue()


class Fixture(FakeTelegramTransport):
    def __init__(self):
        super().__init__()
        self.data = receipt()

    async def get_file(self, file_id):
        return {"file_id":file_id,"file_path":"documents/fixture.dat","file_size":len(self.data)}

    async def download_file(self, path, destination, maximum, *, reserved_identity=None):
        fd = os.open(destination,os.O_WRONLY|os.O_NOFOLLOW)
        with os.fdopen(fd,"wb") as output:
            info=os.fstat(output.fileno())
            if (info.st_dev,info.st_ino)!=reserved_identity or len(self.data)>maximum:
                raise ValueError("fixture_identity_or_size")
            output.write(self.data)
        return len(self.data)


async def smoke():
    results={}
    with tempfile.TemporaryDirectory(prefix="phase4-smoke-",dir=ROOT/"artifacts") as directory:
        root=Path(directory)
        config=replace(Config.load(ROOT/"config.toml",live=False),data_dir=root/"data",user_id=7,chat_id=7,runtime=120)
        store=Store(config.data_dir/"bridge.sqlite3")
        adapter=CodexAdapter(config)
        transport=Fixture()
        bridge=Bridge(config,store,adapter,transport,conflict_check=lambda *_:False)
        async def check_job(number,key):
            await bridge.work_once()
            job=store.job(number)
            if not job or job["state"]!="completed":
                raise ValueError(f"{key}:{job['error_code'] if job else 'missing_job'}")
            results[key]=True
            return job
        try:
            await adapter.open()
            await bridge.handle(update(1,"/new Phase4 knowledge verification"))
            await bridge.handle(update(2,"/save 서울 출장 식비 예산은 73000원으로 확정되었습니다."))
            results["korean_two_character_search"]=bool(bridge.knowledge.search("출장",7,7))
            await bridge.handle(update(3,"/ask 서울 출장 식비 예산은 얼마인가요?"))
            await check_job(1,"real_grounded_ask")
            message={"message_id":4,"from":{"id":7},"chat":{"id":7,"type":"private"},
                "document":{"file_id":"receipt_fixture","mime_type":"image/png"},"caption":"검증 영수증"}
            await bridge.handle({"update_id":4,"message":message})
            await bridge.handle(update(5,"/extract GB-000002"))
            await bridge.process_knowledge_once()
            job=await check_job(2,"real_codex_ocr")
            results["ocr_amount"]= "73000" in job["response_text"]
            await bridge.handle(update(6,"/ask GB-000002 영수증에 쓰인 숫자는 무엇인가요?"))
            await check_job(3,"real_ocr_retrieval_ask")
            transport.data=video()
            video_message={"message_id":7,"from":{"id":7},"chat":{"id":7,"type":"private"},
                "video":{"file_id":"video_fixture"},"caption":"빨간 영상 검증"}
            await bridge.handle({"update_id":7,"message":video_message})
            await bridge.handle(update(8,"/extract GB-000003"))
            await bridge.process_knowledge_once()
            await check_job(4,"real_video_frame_analysis")
            results["video_frames"]=len(json.loads(store.job(4)["image_assets"]))==3
            backups=root/"backups"
            backups.mkdir(mode=0o700)
            bundle=await asyncio.to_thread(backup_bundle,config,backups)
            restored=await asyncio.to_thread(restore_bundle,config,bundle,root/"restored")
            results["full_restore"]= (restored/"bridge.sqlite3").is_file() and len(list((restored/"assets").iterdir()))==6
        finally:
            await adapter.close()
            store.close()
    results["status"]="passed" if all(results.values()) else "failed"
    return results


if __name__=="__main__":
    try:
        result=asyncio.run(smoke())
    except Exception as exc:
        # Only this controlled harness's fixed labels may be printed.
        detail=str(exc) if isinstance(exc,ValueError) and str(exc).startswith(("real_",)) else type(exc).__name__
        result={"status":"failed","error":detail}
    print(json.dumps(result,indent=2))
    raise SystemExit(0 if result["status"]=="passed" else 1)
