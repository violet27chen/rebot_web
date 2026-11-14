"""快递单号扫描：只处理图像，不接触任何电机、相机或传输。"""
import re
import cv2
import numpy as np
from .geometry import SortingError

MIN_SHARPNESS = 60.0  # Laplacian 方差；低于此值视为失焦/运动模糊


def sharpness(image):
    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def _variants(gray):
    yield gray
    yield cv2.createCLAHE(2.0, (8, 8)).apply(gray)
    yield cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    for code in (cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_180, cv2.ROTATE_90_COUNTERCLOCKWISE):
        yield cv2.rotate(gray, code)


def decode(image):
    """返回去重后的 [{'text','type'}]；空列表表示没有读到。"""
    if not isinstance(image, np.ndarray) or image.size == 0:
        raise SortingError('单号扫描需要有效图像')
    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    barcode = cv2.barcode_BarcodeDetector() if hasattr(cv2, 'barcode_BarcodeDetector') else None
    qr = cv2.QRCodeDetector()
    found = {}
    for view in _variants(gray):
        if barcode is not None:
            try:
                ok, texts, kinds, _ = barcode.detectAndDecodeWithType(view)
            except cv2.error:
                ok, texts, kinds = False, (), ()
            if ok:
                for t, k in zip(texts, kinds):
                    if t:
                        found.setdefault(t, k or 'barcode')
        try:
            text, _, _ = qr.detectAndDecode(view)
        except cv2.error:
            text = ''
        if text:
            found.setdefault(text, 'QR_CODE')
        if found:
            break
    return [{'text': t, 'type': k} for t, k in found.items()]


def scan(image, min_sharpness=MIN_SHARPNESS):
    """单次扫描结果：status 为 ok / blurry / not_found，供上层决定是否调整姿态重试。"""
    sharp = sharpness(image)
    codes = decode(image)
    if codes:
        return {'status': 'ok', 'codes': codes, 'sharpness': sharp}
    return {'status': 'blurry' if sharp < min_sharpness else 'not_found', 'codes': [], 'sharpness': sharp}


def destination(codes, rules):
    """按规则（先匹配先生效）把单号映射到目的地；多个不同单号或无匹配都拒绝。

    rules: [{'pattern': 正则, 'destination': 名称}]
    """
    if not codes:
        raise SortingError('没有读到单号，无法确定目的地')
    texts = {c['text'] for c in codes}
    if len(texts) != 1:
        raise SortingError('同一画面读到多个不同单号，请调整视野后重试')
    text = next(iter(texts))
    for rule in rules or []:
        try:
            if re.fullmatch(rule['pattern'], text):
                return {'code': text, 'destination': str(rule['destination'])}
        except (KeyError, re.error):
            raise SortingError('单号规则格式无效')
    raise SortingError('单号 %s 没有匹配的目的地规则' % text)
