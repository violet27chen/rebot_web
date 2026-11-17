"""CPU-only image decoding; called exclusively in the isolated scanner worker."""
from typing import Any, Optional
import base64
import re

_WAYBILL_OCR_ENGINE = None
_WAYBILL_OCR_FAILED = False

def _get_waybill_ocr() -> Any:
    """懒加载轻量 ONNX 面单 OCR 引擎（用于在远距离或强反光条码无法扫出时识别面单单号/取件码）。"""
    global _WAYBILL_OCR_ENGINE, _WAYBILL_OCR_FAILED
    if _WAYBILL_OCR_ENGINE is not None:
        return _WAYBILL_OCR_ENGINE
    if _WAYBILL_OCR_FAILED:
        return None
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore

        _WAYBILL_OCR_ENGINE = RapidOCR(
            det_limit_side_len=512,
            det_db_thresh=0.15,
            det_db_box_thresh=0.2,
            det_db_unclip_ratio=2.0,
            use_cls=False,
            intra_op_num_threads=1,
            inter_op_num_threads=1,
        )
        return _WAYBILL_OCR_ENGINE
    except Exception:
        _WAYBILL_OCR_FAILED = True
        return None

def _decode_waybill_frame(image: Any) -> dict:
    """从单帧 BGR 图像中识别一维条码（Code128/Code39/EAN/ITF 等）、二维码及白色面单单号/取件码。"""
    import cv2
    import numpy as np

    if image is None or not hasattr(image, "shape") or image.size == 0:
        return {"codes": [], "boxes": [], "sharpness": 0.0, "exposure_hint": ""}

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    H, W = gray.shape[:2]
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    found: list[dict] = []
    boxes: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def _add(text: Any, fmt: Any, bbox: Optional[list[int]] = None) -> bool:
        t = str(text or "").strip()
        f = str(fmt or "Barcode").strip().replace("BarcodeFormat.", "")
        if not t or len(t) < 3:
            return False
        t_low = t.lower()
        if t_low.startswith(("http://", "https://", "www.")) or "github.com" in t_low:
            return False
        key = (t, f)
        if key not in seen:
            seen.add(key)
            entry: dict[str, Any] = {"text": t, "type": f}
            if bbox:
                entry["bbox"] = bbox
            found.append(entry)
        return True

    def _zxing_bbox(item: Any, offset_x: int = 0, offset_y: int = 0, scale: float = 1.0) -> Optional[list[int]]:
        try:
            pos = getattr(item, "position", None)
            if pos is None:
                return None
            pts = [pos.top_left, pos.top_right, pos.bottom_right, pos.bottom_left]
            xs = [int(round(p.x / scale)) + offset_x for p in pts]
            ys = [int(round(p.y / scale)) + offset_y for p in pts]
            return [max(0, min(xs)), max(0, min(ys)), min(W, max(xs)), min(H, max(ys))]
        except Exception:
            return None

    # 1. 全图优先使用 zxing-cpp（开启 try_rotate + try_downscale）
    try:
        import zxingcpp  # type: ignore

        clahe = cv2.createCLAHE(clipLimit=2.2, tileGridSize=(8, 8)).apply(gray)
        for cand in (gray, clahe):
            for item in zxingcpp.read_barcodes(cand, try_rotate=True, try_downscale=True):
                bb = _zxing_bbox(item)
                if _add(getattr(item, "text", ""), getattr(item, "format", "Barcode"), bb) and bb:
                    boxes.append({"bbox": bb, "kind": "barcode"})
            if found:
                break
        if not found and min(H, W) <= 720:
            up = cv2.resize(clahe, None, fx=1.7, fy=1.7, interpolation=cv2.INTER_CUBIC)
            for item in zxingcpp.read_barcodes(up, try_rotate=True, try_downscale=True):
                bb = _zxing_bbox(item, scale=1.7)
                if _add(getattr(item, "text", ""), getattr(item, "format", "Barcode"), bb) and bb:
                    boxes.append({"bbox": bb, "kind": "barcode"})
    except Exception:
        pass

    # 2. 自动检测快递上的白色面单区域（解决 1080P 远距离面单占比小、强光过曝反光问题）
    exposure_hint = ""
    rois: list[tuple[float, int, int, int, int]] = []
    if image.ndim == 3:
        try:
            hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
            mask = cv2.inRange(hsv, (0, 0, 170), (180, 70, 255))
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7)))
            cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            for c in cnts:
                area = cv2.contourArea(c)
                if area < 800 or area > H * W * 0.25:
                    continue
                x, y, w, h = cv2.boundingRect(c)
                if x <= 5 or y <= 5 or w < 35 or h < 35 or w > W * 0.35 or max(w, h) / max(1, min(w, h)) > 5.0:
                    continue
                roi_g = gray[y : y + h, x : x + w]
                stretched = np.clip((roi_g.astype(np.float32) - 140.0) * (255.0 / 115.0), 0, 255).astype(np.uint8)
                c_mask = np.zeros((h, w), dtype=np.uint8)
                cv2.drawContours(c_mask, [c - [x, y]], -1, 255, -1)
                inner = cv2.erode(c_mask, np.ones((9, 9), np.uint8))
                if np.count_nonzero(inner) < 200:
                    continue
                gx = np.abs(cv2.Sobel(stretched, cv2.CV_32F, 1, 0, ksize=3))
                gy = np.abs(cv2.Sobel(stretched, cv2.CV_32F, 0, 1, ksize=3))
                ink_grad = float(np.mean((gx + gy)[inner > 0]))
                dark_ratio = float(np.mean(stretched[inner > 0] < 180))
                if 0.02 <= dark_ratio <= 0.60 and ink_grad >= 45.0:
                    rois.append((ink_grad, x, y, w, h))
            rois.sort(reverse=True)
        except Exception:
            pass

    for idx_roi, (ink_grad, x, y, w, h) in enumerate(rois[:2]):
        pad = 65
        x1, y1 = max(0, x - pad), max(0, y - pad)
        x2, y2 = min(W, x + w + pad), min(H, y + h + pad)
        roi_bbox = [x1, y1, x2, y2]
        boxes.append({"bbox": roi_bbox, "kind": "label_roi", "score": round(ink_grad, 1)})
        crop_g = gray[y1:y2, x1:x2]
        p90 = float(np.percentile(crop_g, 90))
        if p90 >= 242 and not exposure_hint:
            exposure_hint = "面单存在顶光反光/过曝，已自动开启去反光增强识别"

        # 2a. 在裁剪放大的白色面单区域上做去反光条码识别
        if not found:
            try:
                import zxingcpp  # type: ignore

                for lo in (0, 130, 155):
                    sub = (
                        crop_g
                        if lo == 0
                        else np.clip((crop_g.astype(np.float32) - lo) * (255.0 / (255.0 - lo)), 0, 255).astype(np.uint8)
                    )
                    up = cv2.resize(sub, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
                    for item in zxingcpp.read_barcodes(up, try_rotate=True, try_downscale=True):
                        _add(getattr(item, "text", ""), getattr(item, "format", "Barcode"), roi_bbox)
                    if found:
                        break
            except Exception:
                pass

        # 2b. 若因距离较远/强光导致一维条码细纹黏连，对置信度最高的 #1 面单区域启用 4 方向单次 OCR 兜底
        if not found and idx_roi == 0:
            ocr = _get_waybill_ocr()
            if ocr is not None:
                try:
                    sub = np.clip((crop_g.astype(np.float32) - 140.0) * (255.0 / 115.0), 0, 255).astype(np.uint8)
                    r0 = sub
                    r90 = cv2.rotate(sub, cv2.ROTATE_90_CLOCKWISE)
                    r180 = cv2.rotate(sub, cv2.ROTATE_180)
                    r270 = cv2.rotate(sub, cv2.ROTATE_90_COUNTERCLOCKWISE)
                    max_w = max(r0.shape[1], r90.shape[1])

                    def _pad_w(im: Any) -> Any:
                        return cv2.copyMakeBorder(
                            im, 6, 6, 6, max_w - im.shape[1] + 6, cv2.BORDER_CONSTANT, value=255
                        )

                    canvas = np.vstack([np.hstack([_pad_w(r0), _pad_w(r180)]), np.hstack([_pad_w(r90), _pad_w(r270)])])
                    res, _ = ocr(cv2.cvtColor(canvas, cv2.COLOR_GRAY2BGR), use_cls=False)
                    for _, txt, conf in res or []:
                        clean = re.sub(r"[^A-Za-z0-9-]", "", str(txt or "")).strip("-")
                        if float(conf) < 0.80 or len(clean) < 4:
                            continue
                        if re.fullmatch(r"[1Il|!]+", clean) or re.fullmatch(r"(\d)\1+", clean):
                            continue
                        if sum(ch.isdigit() for ch in clean) >= 4:
                            _add(clean, "WaybillOCR", roi_bbox)
                except Exception:
                    pass

    # 3. OpenCV QRCodeDetector 兜底
    if not found:
        try:
            qr = cv2.QRCodeDetector()
            ok_qr, decoded_info, _, _ = qr.detectAndDecodeMulti(gray)
            if ok_qr and decoded_info:
                for txt in decoded_info:
                    _add(txt, "QRCode")
            else:
                txt, _, _ = qr.detectAndDecode(gray)
                _add(txt, "QRCode")
        except Exception:
            pass

    return {
        "codes": found,
        "boxes": boxes,
        "sharpness": round(sharpness, 1),
        "exposure_hint": exposure_hint,
    }

def _render_waybill_preview_b64(image: Any, cam_name: str, decode_res: dict, max_width: int = 640) -> str:
    """生成带面单/条码定位框的实时扫码预览图（base64 JPEG dataURL），不在画面上暴露具体单号明文。"""
    import cv2

    if image is None or not hasattr(image, "shape") or image.size == 0:
        return ""
    try:
        vis = image.copy()
        H, W = vis.shape[:2]
        codes = decode_res.get("codes") or []
        boxes = decode_res.get("boxes") or []
        has_code = len(codes) > 0

        # 绘制检测到的面单或条码框
        if boxes:
            for b in boxes:
                bb = b.get("bbox")
                if not bb or len(bb) != 4:
                    continue
                x1, y1, x2, y2 = [int(v) for v in bb]
                color = (34, 197, 94) if has_code else (0, 165, 255)
                cv2.rectangle(vis, (x1, y1), (x2, y2), color, 4)
                label = "WAYBILL OK" if has_code else "LABEL ROI"
                (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)
                ty = max(th + 10, y1)
                cv2.rectangle(vis, (x1, ty - th - 10), (x1 + tw + 14, ty), color, -1)
                cv2.putText(
                    vis, label, (x1 + 7, ty - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA
                )
        else:
            # 未检测到面单时绘制中央扫码取景准星提示
            cx, cy = W // 2, H // 2
            rw, rh = int(W * 0.42), int(H * 0.42)
            x1, y1, x2, y2 = cx - rw // 2, cy - rh // 2, cx + rw // 2, cy + rh // 2
            corner = max(24, min(rw, rh) // 6)
            col = (180, 180, 180)
            for px, py, dx, dy in (
                (x1, y1, 1, 1),
                (x2, y1, -1, 1),
                (x1, y2, 1, -1),
                (x2, y2, -1, -1),
            ):
                cv2.line(vis, (px, py), (px + dx * corner, py), col, 3)
                cv2.line(vis, (px, py), (px, py + dy * corner), col, 3)

        if W > max_width:
            scale = max_width / float(W)
            vis = cv2.resize(vis, (max_width, max(1, int(round(H * scale)))), interpolation=cv2.INTER_AREA)

        ok_enc, buf = cv2.imencode(".jpg", vis, [int(cv2.IMWRITE_JPEG_QUALITY), 78])
        if not ok_enc:
            return ""
        return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("ascii")
    except Exception:
        return ""
