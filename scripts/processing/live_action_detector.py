#!/usr/bin/env python3
"""Lightweight local action detector for live CS2 streams."""

import logging
from pathlib import Path
from typing import Dict, Tuple

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    cv2 = None
    CV2_AVAILABLE = False

try:
    import pytesseract
    TESSERACT_AVAILABLE = True
except ImportError:
    pytesseract = None
    TESSERACT_AVAILABLE = False


class LiveActionDetector:
    """Detect likely highlight moments locally before escalating to vision."""

    KILLFEED_REGION = (0.66, 0.04, 0.98, 0.22)
    SCORE_REGION = (0.20, 0.00, 0.80, 0.14)
    CENTER_REGION = (0.24, 0.18, 0.76, 0.52)
    OVERLAY_KEYWORDS = {
        'clutch', 'ace', 'defused', 'defuse', 'wins', 'win',
        'match point', 'overtime', 'timeout', 'technical',
    }

    def __init__(self):
        self.diff_threshold = float(0.11)
        self.killfeed_threshold = float(0.15)
        self.overlay_threshold = float(0.09)

    def _load_rgb(self, image_path: str) -> np.ndarray:
        image = Image.open(image_path).convert('RGB')
        return np.asarray(image)

    def _crop(self, image: np.ndarray, region: Tuple[float, float, float, float]) -> np.ndarray:
        height, width = image.shape[:2]
        x1 = int(region[0] * width)
        y1 = int(region[1] * height)
        x2 = int(region[2] * width)
        y2 = int(region[3] * height)
        return image[y1:y2, x1:x2]

    def _normalize_gray(self, image: np.ndarray) -> np.ndarray:
        if CV2_AVAILABLE:
            gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
            return cv2.resize(gray, (320, 180))
        return np.asarray(Image.fromarray(image).convert('L').resize((320, 180)))

    def _mean_abs_diff(self, previous: np.ndarray, current: np.ndarray) -> float:
        prev_gray = self._normalize_gray(previous).astype(np.float32) / 255.0
        curr_gray = self._normalize_gray(current).astype(np.float32) / 255.0
        return float(np.mean(np.abs(curr_gray - prev_gray)))

    def _extract_keywords(self, image: np.ndarray) -> Tuple[str, bool]:
        if not TESSERACT_AVAILABLE:
            return '', False
        try:
            text = pytesseract.image_to_string(Image.fromarray(image), config='--psm 6').lower()
            return text, any(keyword in text for keyword in self.OVERLAY_KEYWORDS)
        except Exception as exc:
            logger.debug(f"OCR trigger check failed: {exc}")
            return '', False

    def analyze_transition(self, previous_path: str, current_path: str) -> Dict:
        previous = self._load_rgb(previous_path)
        current = self._load_rgb(current_path)

        killfeed_prev = self._crop(previous, self.KILLFEED_REGION)
        killfeed_curr = self._crop(current, self.KILLFEED_REGION)
        score_prev = self._crop(previous, self.SCORE_REGION)
        score_curr = self._crop(current, self.SCORE_REGION)
        center_prev = self._crop(previous, self.CENTER_REGION)
        center_curr = self._crop(current, self.CENTER_REGION)

        killfeed_diff = self._mean_abs_diff(killfeed_prev, killfeed_curr)
        score_diff = self._mean_abs_diff(score_prev, score_curr)
        overlay_diff = self._mean_abs_diff(center_prev, center_curr)

        overlay_text, has_overlay_keyword = self._extract_keywords(center_curr)
        killfeed_text, has_killfeed_keyword = self._extract_keywords(killfeed_curr)
        trigger_score = (killfeed_diff * 0.5) + (score_diff * 0.2) + (overlay_diff * 0.3)
        triggered = (
            trigger_score >= self.diff_threshold
            or killfeed_diff >= self.killfeed_threshold
            or overlay_diff >= self.overlay_threshold
            or has_overlay_keyword
            or has_killfeed_keyword
        )

        return {
            'triggered': triggered,
            'trigger_score': round(trigger_score, 4),
            'killfeed_diff': round(killfeed_diff, 4),
            'score_diff': round(score_diff, 4),
            'overlay_diff': round(overlay_diff, 4),
            'overlay_text': overlay_text[:240],
            'killfeed_text': killfeed_text[:240],
        }
