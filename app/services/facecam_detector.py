"""
SMART FACECAM & WEBCAM DETECTOR (OPENCV 5 YUNET + OPENCV 4 HAAR)
Detecta automaticamente a posição real da webcam/facecam do streamer em vídeos 16:9:
1. OpenCV 5 / 4.7+: cv2.FaceDetectorYN (YuNet CNN ONNX — 228 KB incluso no repo, alta precisão com headset/luz RGB)
2. OpenCV 4 fallback: cv2.CascadeClassifier (Haar Cascade)
3. Amostragem dentro do trecho do clipe (evita telas de introdução sem webcam) + agrupamento espacial da webcam
"""

import os
import logging
import urllib.request
from typing import Dict, Any, Optional, List, Tuple

try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None
    np = None

logger = logging.getLogger("FacecamDetector")

YUNET_URL = "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
YUNET_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "models", "face_detection_yunet_2023mar.onnx"),
    "./app/assets/models/face_detection_yunet_2023mar.onnx",
    "./data/models/face_detection_yunet_2023mar.onnx",
]


def _resolve_yunet_model() -> Optional[str]:
    for path in YUNET_CANDIDATES:
        if os.path.exists(path) and os.path.getsize(path) > 50000:
            return os.path.abspath(path)
    # Fallback: baixa automaticamente para ./data/models/ se o arquivo local não existir
    target = "./data/models/face_detection_yunet_2023mar.onnx"
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        urllib.request.urlretrieve(YUNET_URL, target)
        if os.path.exists(target) and os.path.getsize(target) > 50000:
            return os.path.abspath(target)
    except Exception as exc:
        logger.warning("Não foi possível baixar modelo YuNet: %s", exc)
    return None


class FacecamDetector:
    def __init__(self):
        self.yunet_model_path: Optional[str] = None
        self.haar_detector = None
        self.engine_name = "fallback"

        if cv2 is None:
            return

        # 1. Prioridade: OpenCV 5 / 4.7+ FaceDetectorYN (YuNet CNN — funciona em OpenCV 5.1.0!)
        if hasattr(cv2, "FaceDetectorYN"):
            model_path = _resolve_yunet_model()
            if model_path:
                self.yunet_model_path = model_path
                self.engine_name = "yunet_cnn"

        # 2. Fallback: OpenCV 4 CascadeClassifier
        if hasattr(cv2, "CascadeClassifier") and hasattr(cv2, "data"):
            cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            try:
                det = cv2.CascadeClassifier(cascade_path)
                if not det.empty():
                    self.haar_detector = det
                    if self.engine_name == "fallback":
                        self.engine_name = "haar_cascade"
            except Exception:
                self.haar_detector = None

    def _detect_faces_in_frame(self, frame, yunet_det=None) -> List[Tuple[int, int, int, int, float]]:
        """Retorna lista de (x, y, w, h, confidence) no espaço de coordenadas do frame."""
        h_img, w_img = frame.shape[:2]
        found: List[Tuple[int, int, int, int, float]] = []

        if yunet_det is not None:
            # Reduz frame grande para largura máx 640px (muito rápido e preciso no YuNet)
            scale = min(1.0, 640.0 / max(1, w_img))
            rw = max(64, int(round(w_img * scale)))
            rh = max(64, int(round(h_img * scale)))
            resized = cv2.resize(frame, (rw, rh)) if scale < 0.999 else frame
            yunet_det.setInputSize((rw, rh))
            _, faces = yunet_det.detect(resized)
            if faces is not None and len(faces) > 0:
                inv = 1.0 / scale
                for f in faces:
                    fx = int(max(0, round(float(f[0]) * inv)))
                    fy = int(max(0, round(float(f[1]) * inv)))
                    fw = int(max(1, round(float(f[2]) * inv)))
                    fh = int(max(1, round(float(f[3]) * inv)))
                    score = float(f[14]) if len(f) > 14 else 0.85
                    if fw >= int(h_img * 0.04) and fh >= int(h_img * 0.04):
                        found.append((fx, fy, fw, fh, score))
            if found:
                return found

        if self.haar_detector is not None:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = self.haar_detector.detectMultiScale(
                gray,
                scaleFactor=1.15,
                minNeighbors=4,
                minSize=(int(h_img * 0.05), int(h_img * 0.05)),
                maxSize=(int(h_img * 0.50), int(h_img * 0.50)),
            )
            for (fx, fy, fw, fh) in faces:
                found.append((int(fx), int(fy), int(fw), int(fh), 0.75))

        return found

    def detect_facecam_box(
        self,
        video_path: str,
        sample_duration_seconds: float = 30.0,
        samples_count: int = 16,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Analisa frames do trecho do clipe (ou ao longo do vídeo) para localizar
        com precisão a região onde a webcam/facecam do streamer está posicionada.
        """
        if not video_path or cv2 is None or np is None or not os.path.exists(video_path):
            return self._default_fallback(1920, 1080)

        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return self._default_fallback(1920, 1080)

        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 1920
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 1080

        if total_frames <= 0 or (self.yunet_model_path is None and self.haar_detector is None):
            cap.release()
            return self._default_fallback(width, height)

        # Define janela de amostragem: prioriza o trecho do clipe (evita tela de intro sem webcam!)
        video_dur = total_frames / max(1.0, fps)
        if start_time is not None:
            s_sec = max(0.0, min(video_dur - 0.5, float(start_time)))
            e_sec = float(end_time) if (end_time is not None and end_time > s_sec) else min(video_dur, s_sec + sample_duration_seconds)
            e_sec = min(video_dur, max(s_sec + 1.0, e_sec))
        else:
            # Pula os primeiros 5% caso seja vídeo longo (intro), mas garante cobertura em vídeos curtos
            s_sec = min(15.0, video_dur * 0.05) if video_dur > 40.0 else 0.0
            e_sec = min(video_dur, s_sec + max(sample_duration_seconds, video_dur * 0.35))

        start_frame = max(0, int(s_sec * fps))
        end_frame = min(total_frames - 1, max(start_frame + 1, int(e_sec * fps)))
        frame_indices = np.linspace(start_frame, end_frame, min(samples_count, max(1, end_frame - start_frame + 1)), dtype=int)

        yunet_det = None
        if self.yunet_model_path and hasattr(cv2, "FaceDetectorYN"):
            try:
                yunet_det = cv2.FaceDetectorYN.create(
                    self.yunet_model_path,
                    "",
                    (640, 360),
                    0.55,  # score_threshold
                    0.30,  # nms_threshold
                    5000,
                )
            except Exception as exc:
                logger.warning("Falha ao criar FaceDetectorYN: %s", exc)
                yunet_det = None

        detected_faces: List[Tuple[int, int, int, int, float]] = []
        for f_idx in frame_indices:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(f_idx))
            ret, frame = cap.read()
            if not ret or frame is None:
                continue
            detected_faces.extend(self._detect_faces_in_frame(frame, yunet_det=yunet_det))

        cap.release()

        if not detected_faces:
            return self._default_fallback(width, height)

        # Agrupamento por quadrante: em lives gamers, a webcam fica parada no mesmo canto;
        # rostos aleatórios da gameplay mudam de lugar, então o cluster com mais detecções vence!
        buckets: Dict[str, List[Tuple[int, int, int, int, float]]] = {}
        for face in detected_faces:
            fx, fy, fw, fh, _ = face
            cx = (fx + fw / 2.0) / width
            cy = (fy + fh / 2.0) / height
            # Grade 3x2 (esq/centro/dir × topo/base)
            gx = 0 if cx < 0.36 else (2 if cx > 0.64 else 1)
            gy = 0 if cy < 0.50 else 1
            key = f"{gx}_{gy}"
            buckets.setdefault(key, []).append(face)

        # Prefere o quadrante mais consistente (mais detecções; empate -> maior confiança)
        best_cluster = max(
            buckets.values(),
            key=lambda grp: (len(grp), sum(x[4] for x in grp)),
        )

        faces_arr = np.array([(x, y, w, h) for x, y, w, h, _ in best_cluster])
        median_x = int(np.median(faces_arr[:, 0]))
        median_y = int(np.median(faces_arr[:, 1]))
        median_w = int(np.median(faces_arr[:, 2]))
        median_h = int(np.median(faces_arr[:, 3]))

        # Expande ao redor do rosto com enquadramento mais fechado (foco no rosto/ombros sem vazar HUD da gameplay)
        box_w = max(int(width * 0.16), min(int(width * 0.36), int(median_w * 2.3)))
        box_h = max(int(height * 0.18), min(int(height * 0.40), int(median_h * 2.0)))
        box_x = max(0, median_x - int((box_w - median_w) / 2))
        box_y = max(0, median_y - int((box_h - median_h) * 0.36))

        if box_x + box_w > width:
            box_x = max(0, width - box_w)
        if box_y + box_h > height:
            box_y = max(0, height - box_h)

        corner = self._identify_corner(box_x, box_y, box_w, box_h, width, height)

        return {
            "x": box_x,
            "y": box_y,
            "width": box_w,
            "height": box_h,
            "rel_x": round(box_x / width, 4),
            "rel_y": round(box_y / height, 4),
            "rel_w": round(box_w / width, 4),
            "rel_h": round(box_h / height, 4),
            "corner": corner,
            "engine": self.engine_name,
            "samples_matched": len(best_cluster),
            "detected": True,
        }

    def _identify_corner(self, x: int, y: int, box_w: int, box_h: int, width: int, height: int) -> str:
        center_x = x + box_w / 2.0
        center_y = y + box_h / 2.0
        is_right = center_x > (width / 2.0)
        is_bottom = center_y > (height / 2.0)
        if is_right and not is_bottom:
            return "top-right"
        elif not is_right and not is_bottom:
            return "top-left"
        elif is_right and is_bottom:
            return "bottom-right"
        else:
            return "bottom-left"

    def _default_fallback(self, width: int = 1920, height: int = 1080) -> Dict[str, Any]:
        """Fallback padrão de streamer: canto superior direito."""
        w = int(width * 0.35)
        h = int(height * 0.40)
        x = width - w
        y = 0
        return {
            "x": x, "y": y, "width": w, "height": h,
            "rel_x": round(x / width, 4), "rel_y": round(y / height, 4),
            "rel_w": round(w / width, 4), "rel_h": round(h / height, 4),
            "corner": "top-right",
            "engine": self.engine_name,
            "samples_matched": 0,
            "detected": False,
        }
