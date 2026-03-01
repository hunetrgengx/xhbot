"""OpenCV DNN 本地性别检测 - 无需 API，无 QPS 限制

两阶段流水线：人脸 → 人体 → other
- 有人脸：人脸区域性别分类
- 无人脸：HOG 人体检测 → 上半身裁剪 → 性别分类（置信度 < 0.6 则返回 other）
"""
import logging
import urllib.request
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# 模型目录
MODELS_DIR = Path(__file__).parent / "models"
MODELS_DIR.mkdir(parents=True, exist_ok=True)

# 性别模型（需下载），人脸检测用 OpenCV 内置 Haar
GENDER_PROTO = MODELS_DIR / "gender_deploy.prototxt"
GENDER_MODEL = MODELS_DIR / "gender_net.caffemodel"
GENDER_MODEL_URL = "https://github.com/GilLevi/AgeGenderDeepLearning/raw/master/models/gender_net.caffemodel"

# 预处理参数
GENDER_MEAN = (78.4263377603, 87.7689143744, 114.895847746)

# 全身人像：上半身性别预测置信度阈值，低于则返回 other
BODY_GENDER_CONFIDENCE_THRESHOLD = 0.6


def _download_file(url: str, dest: Path) -> None:
    """下载文件到指定路径"""
    if dest.exists():
        return
    logger.info(f"正在下载: {dest.name} (~45MB) ...")
    try:
        urllib.request.urlretrieve(url, dest)
        logger.info(f"下载完成: {dest.name}")
    except Exception as e:
        logger.error(f"下载失败 {dest.name}: {e}")
        raise


def _ensure_models() -> None:
    """确保性别模型存在，缺失则下载"""
    if not GENDER_MODEL.exists():
        _download_file(GENDER_MODEL_URL, GENDER_MODEL)


# 全局模型实例（懒加载）
_face_cascade = None
_gender_net = None
_hog = None


def _get_face_cascade():
    """获取 OpenCV 内置人脸检测器"""
    global _face_cascade
    if _face_cascade is None:
        path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        _face_cascade = cv2.CascadeClassifier(path)
    return _face_cascade


def _get_gender_net():
    """获取性别识别网络"""
    global _gender_net
    if _gender_net is None:
        _ensure_models()
        _gender_net = cv2.dnn.readNetFromCaffe(str(GENDER_PROTO), str(GENDER_MODEL))
    return _gender_net


def _get_hog():
    """获取 HOG 行人检测器"""
    global _hog
    if _hog is None:
        _hog = cv2.HOGDescriptor()
        _hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
    return _hog


def _detect_faces(image: np.ndarray) -> list[tuple[int, int, int, int]]:
    """检测人脸，返回 bbox 列表 [(x1,y1,x2,y2), ...]"""
    cascade = _get_face_cascade()
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    faces = cascade.detectMultiScale(gray, 1.1, 5, minSize=(30, 30))
    return [(int(x), int(y), int(x + w), int(y + h)) for (x, y, w, h) in faces]


def _detect_persons(image: np.ndarray) -> list[tuple[int, int, int, int]]:
    """HOG 人体检测，返回 bbox 列表 [(x1,y1,x2,y2), ...]，取面积最大的"""
    hog = _get_hog()
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    rects, _ = hog.detectMultiScale(
        gray, winStride=(8, 8), padding=(16, 16), scale=1.05
    )
    if rects is None or len(rects) == 0:
        return []
    # 按面积排序，取最大的
    rects = sorted(rects, key=lambda r: r[2] * r[3], reverse=True)
    return [(int(x), int(y), int(x + w), int(y + h)) for (x, y, w, h) in rects]


def _predict_gender(face_roi: np.ndarray) -> tuple[str, float]:
    """
    对裁剪区域做性别预测，返回 (gender, confidence)
    gender: "male" | "female", confidence: 0~1
    """
    if face_roi.size == 0:
        return "other", 0.0
    gender_net = _get_gender_net()
    blob = cv2.dnn.blobFromImage(
        face_roi, 1.0, (227, 227), GENDER_MEAN, swapRB=False
    )
    gender_net.setInput(blob)
    preds = gender_net.forward()
    probs = preds[0]
    if len(probs) < 2:
        return "other", 0.0
    # softmax
    exp = np.exp(probs - probs.max())
    probs = exp / exp.sum()
    idx = int(probs.argmax())
    conf = float(probs[idx])
    return ("male" if idx == 0 else "female", conf)


def detect_gender(image_path: Path) -> str:
    """
    两阶段性别检测：人脸 → 人体 → other
    返回: "male"=男性, "female"=女性, "other"=无人脸/无人体/置信度低, "failure"=检测失败
    """
    try:
        image = cv2.imread(str(image_path))
        if image is None:
            return "failure"

        # 阶段 1：人脸检测
        face_bboxes = _detect_faces(image)
        if face_bboxes:
            x1, y1, x2, y2 = face_bboxes[0]
            face = image[y1:y2, x1:x2]
            if face.size > 0:
                gender, _ = _predict_gender(face)
                return gender

        # 阶段 2：人体检测（无人脸时）
        person_bboxes = _detect_persons(image)
        if not person_bboxes:
            return "other"

        x1, y1, x2, y2 = person_bboxes[0]
        h = y2 - y1
        # 裁剪上半身（头部区域，约 40%）
        upper_h = max(int(h * 0.4), 50)
        upper_body = image[y1 : y1 + upper_h, x1:x2]
        if upper_body.size == 0 or upper_body.shape[0] < 30 or upper_body.shape[1] < 30:
            return "other"

        gender, conf = _predict_gender(upper_body)
        if conf < BODY_GENDER_CONFIDENCE_THRESHOLD:
            return "other"
        return gender

    except Exception as e:
        logger.exception(f"OpenCV 性别检测失败: {e}")
        return "failure"
