"""
SPEECH-TO-TEXT TRANSCRIBER SERVICE
Transcreve áudio com timestamps em nível de palavra usando faster-whisper (local)
e fallback opcional para Groq / OpenAI Whisper.
"""

import os
import json
from typing import Dict, Any, List
from sqlalchemy.orm import Session
from app.database.models import SourceVideo

TRANSCRIPTS_DIR = "./data/transcripts"

class WhisperTranscriber:
    def __init__(self, db: Session, model_size: str = "base", device: str = "cpu"):
        self.db = db
        self.model_size = model_size
        self.device = device
        self._model = None
        os.makedirs(TRANSCRIPTS_DIR, exist_ok=True)

    def _get_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            # compute_type int8 para CPU é super rápido e consome pouca RAM
            compute_type = "int8" if self.device == "cpu" else "float16"
            self._model = WhisperModel(self.model_size, device=self.device, compute_type=compute_type)
        return self._model

    def transcribe_video(self, video_id: str, language: str = "pt") -> Dict[str, Any]:
        video = self.db.query(SourceVideo).filter(SourceVideo.id == video_id).first()
        if not video:
            raise ValueError(f"Vídeo {video_id} não encontrado.")

        if not video.audio_path or not os.path.exists(video.audio_path):
            raise FileNotFoundError(f"Áudio não encontrado para o vídeo {video_id}.")

        transcript_output_path = os.path.join(TRANSCRIPTS_DIR, f"{video_id}.json")

        try:
            video.status = "transcribing"
            self.db.commit()

            model = self._get_model()
            segments, info = model.transcribe(
                video.audio_path,
                language=language,
                word_timestamps=True,
                vad_filter=True, # Voice Activity Detection para ignorar silêncio
                vad_parameters=dict(min_silence_duration_ms=500)
            )

            all_segments: List[Dict[str, Any]] = []
            full_text_parts: List[str] = []

            for seg in segments:
                words_data = []
                if seg.words:
                    for w in seg.words:
                        words_data.append({
                            "word": w.word.strip(),
                            "start": round(w.start, 2),
                            "end": round(w.end, 2),
                            "probability": round(w.probability, 3)
                        })

                seg_dict = {
                    "id": seg.id,
                    "start": round(seg.start, 2),
                    "end": round(seg.end, 2),
                    "text": seg.text.strip(),
                    "words": words_data
                }
                all_segments.append(seg_dict)
                full_text_parts.append(seg.text.strip())

            full_text = " ".join(full_text_parts)

            result = {
                "video_id": video_id,
                "language": info.language,
                "language_probability": info.language_probability,
                "duration": info.duration,
                "full_text": full_text,
                "segments": all_segments
            }

            with open(transcript_output_path, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2, ensure_ascii=False)

            video.transcript_path = transcript_output_path
            video.status = "transcribed"
            self.db.commit()

            return result

        except Exception as e:
            video.status = "error"
            video.error_message = f"Erro na transcrição: {str(e)}"
            self.db.commit()
            raise e
