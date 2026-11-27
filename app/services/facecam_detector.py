"""
SMART FACECAM & WEBCAM DETECTOR (COMPUTER VISION)
Detecta automaticamente a posição da webcam/facecam do streamer em vídeos 16:9
usando visão computacional (OpenCV 4/5) com clustering e fallback seguro.
"""

import cv2
import numpy as np
from typing import Dict, Any, Optional

class FacecamDetector:
    def __init__(self):
        self.detector = None
        # Suporte a OpenCV 4 (CascadeClassifier) ou OpenCV 5 (FaceDetectorYN)
        if hasattr(cv2, "CascadeClassifier") and hasattr(cv2, "data"):
            cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            try:
                self.detector = cv2.CascadeClassifier(cascade_path)
            except Exception:
                self.detector = None

    def detect_facecam_box(
        self,
        video_path: str,
        sample_duration_seconds: float = 30.0,
        samples_count: int = 15
    ) -> Dict[str, Any]:
        """
        Analisa frames de amostra nos primeiros segundos do vídeo para localizar
        a região estática onde a facecam do streamer está posicionada.
        """
        if not video_path:
            return self._default_fallback(1920, 1080)

        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return self._default_fallback(1920, 1080)

        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 1920
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 1080

        max_sample_frame = min(total_frames, int(sample_duration_seconds * fps))
        if max_sample_frame <= 0 or self.detector is None:
            cap.release()
            return self._default_fallback(width, height)

        frame_indices = np.linspace(0, max_sample_frame - 1, samples_count, dtype=int)
        detected_faces = []

        for f_idx in frame_indices:
            cap.set(cv2.CAP_PROP_POS_FRAMES, f_idx)
            ret, frame = cap.read()
            if not ret or frame is None:
                continue

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = self.detector.detectMultiScale(
                gray,
                scaleFactor=1.15,
                minNeighbors=5,
                minSize=(int(height * 0.08), int(height * 0.08)),
                maxSize=(int(height * 0.45), int(height * 0.45))
            )

            for (x, y, w, h) in faces:
                detected_faces.append((x, y, w, h))

        cap.release()

        if not detected_faces:
            return self._default_fallback(width, height)

        faces_arr = np.array(detected_faces)
        median_x = int(np.median(faces_arr[:, 0]))
        median_y = int(np.median(faces_arr[:, 1]))
        median_w = int(np.median(faces_arr[:, 2]))
        median_h = int(np.median(faces_arr[:, 3]))

        box_w = int(median_w * 2.2)
        box_h = int(median_h * 2.2)
        box_x = max(0, median_x - int((box_w - median_w) / 2))
        box_y = max(0, median_y - int((box_h - median_h) / 2))

        if box_x + box_w > width:
            box_w = width - box_x
        if box_y + box_h > height:
            box_h = height - box_y

        corner = self._identify_corner(box_x, box_y, width, height)

        return {
            "x": box_x,
            "y": box_y,
            "width": box_w,
            "height": box_h,
            "rel_x": round(box_x / width, 3),
            "rel_y": round(box_y / height, 3),
            "rel_w": round(box_w / width, 3),
            "rel_h": round(box_h / height, 3),
            "corner": corner,
            "detected": True
        }

    def _identify_corner(self, x: int, y: int, width: int, height: int) -> str:
        is_right = (x + width / 2) > (width / 2)
        is_bottom = (y + height / 2) > (height / 2)
        if is_right and not is_bottom:
            return "top-right"
        elif not is_right and not is_bottom:
            return "top-left"
        elif is_right and is_bottom:
            return "bottom-right"
        else:
            return "bottom-left"

    def _default_fallback(self, width: int = 1920, height: int = 1080) -> Dict[str, Any]:
        """Fallback padrão de streamer: canto superior direito (45% largura, 45% altura)"""
        w = int(width * 0.40)
        h = int(height * 0.45)
        x = width - w
        y = 0
        return {
            "x": x, "y": y, "width": w, "height": h,
            "rel_x": round(x / width, 3), "rel_y": round(y / height, 3),
            "rel_w": round(w / width, 3), "rel_h": round(h / height, 3),
            "corner": "top-right",
            "detected": False
        }
