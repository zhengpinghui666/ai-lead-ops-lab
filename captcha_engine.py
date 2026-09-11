"""Quality-gated ddddocr image worker. JSON over stdin/stdout; no platform traffic.

Promoted from the independently evaluated 1.6.1 adapter. Scores are engineering
thresholds, not probabilities. A prediction is never a platform acceptance.
"""
import base64
import hashlib
import io
import json
import sys
import time

MAX_BYTES = 1024 * 1024
MAX_INPUT = 2 * (4 * ((MAX_BYTES + 2) // 3)) + 1024


def review(reason):
    return {'status': 'needs_review', 'result': None, 'reason': reason}


def solve(payload):
    import cv2
    import ddddocr
    import numpy as np
    from PIL import Image

    if not isinstance(payload, dict) or payload.get('method') not in ('ocr', 'slide_match', 'same_shape_pair'):
        return review('unsupported_type')
    method = payload['method']
    keys = {'method', 'image'} if method in ('ocr', 'same_shape_pair') else {'method', 'target_image', 'background_image'}
    if set(payload) != keys:
        return review('invalid_input')

    def decode(value):
        if not isinstance(value, str) or len(value) > 4 * ((MAX_BYTES + 2) // 3):
            raise ValueError('invalid_image')
        raw = base64.b64decode(value, validate=True)
        if not raw or len(raw) > MAX_BYTES:
            raise ValueError('invalid_image')
        with Image.open(io.BytesIO(raw)) as original:
            width, height = original.size
            if width * height > 4_000_000 or max(width, height) > 4096:
                raise ValueError('invalid_image')
            rgba = np.asarray(original.convert('RGBA'))
        gray = cv2.cvtColor(rgba[:, :, :3], cv2.COLOR_RGB2GRAY)
        return raw, (width, height), gray, bool(np.any(rgba[:, :, 3] != 255))

    try:
        if method == 'same_shape_pair':
            raw, _, _, transparent = decode(payload['image'])
            if transparent:
                return review('point_background_unsupported')
            from captcha_point import predict
            result = predict(raw)
            if result['status'] == 'predicted':
                result['image_sha256'] = hashlib.sha256(raw).hexdigest()
            return result
        if method == 'ocr':
            raw, _, gray, _ = decode(payload['image'])
            if float(np.std(gray)) < 2.0:
                return review('insufficient_image_information')
            result = ddddocr.DdddOcr(ocr=True, det=False, show_ad=False).classification(raw)
            return ({'status': 'predicted', 'result': result, 'output_case_transform': 'none'}
                    if result.strip() else review('empty_recognition'))
        target, size, target_gray, transparent = decode(payload['target_image'])
        background, background_size, background_gray, _ = decode(payload['background_image'])
        if transparent:
            return review('transparent_template_requires_mask_aware_model')
        if any(a > b for a, b in zip(size, background_size)):
            return review('invalid_dimensions')
        if min(float(np.std(target_gray)), float(np.std(background_gray))) < 2.0:
            return review('insufficient_image_information')
        target_features, background_features = cv2.Canny(target_gray, 50, 150), cv2.Canny(background_gray, 50, 150)
        if np.count_nonzero(target_features) < 4:
            return review('insufficient_edges')
        scores = cv2.matchTemplate(background_features, target_features, cv2.TM_CCOEFF_NORMED)
        if not np.all(np.isfinite(scores)):
            return review('non_finite_matching_score')
        _, best, _, location = cv2.minMaxLoc(scores)
        if best < 0.50:
            return review('weak_match')
        x, y = location
        width, height = size
        alternatives = scores.copy()
        alternatives[max(0, y-height//2):y+height//2+1, max(0, x-width//2):x+width//2+1] = -np.inf
        finite = alternatives[np.isfinite(alternatives)]
        margin = float(best - np.max(finite)) if finite.size else None
        if margin is not None and margin < 0.05:
            return review('ambiguous_match')
        # Use the pinned model API, then validate its coordinate contract against
        # the independent quality calculation instead of trusting a raw x value.
        prediction = ddddocr.DdddOcr(ocr=False, det=False, show_ad=False).slide_match(target, background)
        center = [x + width // 2, y + height // 2]
        if prediction.get('target') != center:
            return review('model_coordinate_mismatch')
        return {'status': 'predicted', 'result': {'target': center},
                'coordinate_type': 'center_xy_in_image_pixels', 'background_size': list(background_size),
                'target_size': list(size), 'match_score': float(best), 'peak_margin': margin,
                'score_is_probability': False,
                'image_sha256': hashlib.sha256(target + background).hexdigest()}
    except (ValueError, OSError, KeyError, TypeError):
        return review('invalid_image')


def main():
    # Model dependencies may inspect network interfaces; outbound connections and
    # subprocesses are prohibited in this image-only worker.
    def restrict(event, args):
        if event in ('socket.connect', 'socket.connect_ex', 'subprocess.Popen'):
            raise RuntimeError('Image worker has no outbound transport')
    sys.addaudithook(restrict)
    started = time.monotonic()
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            result = review('input_too_large')
        else:
            result = solve(json.loads(raw))
    except Exception as exc:
        result = review('dependency_missing' if isinstance(exc, ImportError) else 'solver_error')
    result['elapsed_ms'] = round((time.monotonic() - started) * 1000)
    print(json.dumps(result, separators=(',', ':')))


if __name__ == '__main__':
    main()
