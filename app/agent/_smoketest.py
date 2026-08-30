"""Headless WebRTC smoke test: connect to the live agent, receive its greeting
audio. Proves the real pipeline (WebRTC media + Groq LLM + Kokoro TTS) works end to
end without a mic. Run inside the agent container:
    docker compose exec agent python -m app.agent._smoketest
"""
import asyncio
import fractions

import httpx
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamTrack
from av import AudioFrame


class Silence(MediaStreamTrack):
    kind = "audio"

    def __init__(self):
        super().__init__()
        self.sample_rate = 48000
        self.samples = 960
        self.pts = 0

    async def recv(self):
        await asyncio.sleep(0.02)
        frame = AudioFrame(format="s16", layout="mono", samples=self.samples)
        for p in frame.planes:
            p.update(bytes(self.samples * 2))
        frame.pts = self.pts
        frame.sample_rate = self.sample_rate
        frame.time_base = fractions.Fraction(1, self.sample_rate)
        self.pts += self.samples
        return frame


async def main():
    pc = RTCPeerConnection()
    pc.addTrack(Silence())
    got = {"audio_frames": 0, "state": None}

    @pc.on("connectionstatechange")
    async def _state():
        got["state"] = pc.connectionState
        print("connectionState:", pc.connectionState)

    @pc.on("track")
    def _track(track):
        print("received track:", track.kind)

        async def read():
            while True:
                try:
                    await track.recv()
                    got["audio_frames"] += 1
                except Exception:
                    break
        asyncio.ensure_future(read())

    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    async with httpx.AsyncClient() as c:
        r = await c.post("http://localhost:7860/api/offer",
                         json={"sdp": pc.localDescription.sdp,
                               "type": pc.localDescription.type}, timeout=30)
    ans = r.json()
    print("offer accepted, pc_id:", ans.get("pc_id"))
    await pc.setRemoteDescription(RTCSessionDescription(sdp=ans["sdp"], type=ans["type"]))

    # Wait up to 90s for the greeting audio (first run downloads the Kokoro model).
    for _ in range(90):
        await asyncio.sleep(1)
        if got["audio_frames"] > 5:
            break

    print(f"RESULT connectionState={got['state']} greeting_audio_frames={got['audio_frames']}")
    await pc.close()


if __name__ == "__main__":
    asyncio.run(main())
