import numpy as np
import whisper
from .asr_interface import ASRInterface


class VoiceRecognition(ASRInterface):
    def __init__(
        self,
        name: str = "base",
        download_root: str = None,
        device="cpu",
    ) -> None:
        self.model = whisper.load_model(
            name=name,
            device=device,
            download_root=download_root,
        )

    def transcribe_np(self, audio: np.ndarray) -> str:
        result = self.model.transcribe(audio)
        full_text = result['text']
        return full_text
