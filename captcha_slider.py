"""Local alpha-masked slider matching; no network, models or platform verdicts.

Correlate only opaque interior texture, with per-channel mean subtraction to
allow a darkened destination. Three mask erosions must agree. Coordinates use
the full original PNG canvas, including transparent padding and internal holes.
Thresholds are conservative engineering gates, not calibrated probabilities.
"""

VERSION = 'alpha-slider-v1'


def predict(target, background):
    import cv2
    import numpy as np

    def review(reason):
        return {'status': 'needs_review', 'result': None, 'reason': reason,
                'matching_route': 'alpha_masked_ncc', 'solver_version': VERSION}

    if (target.ndim != 3 or background.ndim != 3 or target.shape[2] != 4
            or background.shape[2] != 4 or target.dtype != np.uint8
            or background.dtype != np.uint8):
        return review('invalid_image')
    height, width = target.shape[:2]
    bh, bw = background.shape[:2]
    if (min(height, width) < 32 or max(height, width) > 512
            or bh * bw > 1_048_576 or height >= bh or width >= bw):
        return review('invalid_dimensions')
    if np.any(background[:, :, 3] != 255):
        return review('unsupported_type')
    opaque = (target[:, :, 3] >= 250).astype(np.uint8)
    if not np.any(target[:, :, 3] == 0):
        return review('insufficient_image_information')

    template = target[:, :, :3].astype(np.float32)
    source = background[:, :, :3].astype(np.float32)
    step = max(1, round(min(height, width) / 55))
    variants = []
    for radius in (step, step * 2, step * 3):
        mask = cv2.erode(opaque, np.ones((2 * radius + 1,) * 2, np.uint8),
                         borderType=cv2.BORDER_CONSTANT, borderValue=0).astype(np.float32)
        support = float(mask.sum())
        if support < max(128, height * width * .05):
            return review('insufficient_image_information')
        mean = (template * mask[:, :, None]).sum(axis=(0, 1), dtype=np.float64) / support
        centered = ((template - mean) * mask[:, :, None]).astype(np.float32)
        energy = float(np.sum(centered * centered, dtype=np.float64))
        if energy < support * 12:
            return review('insufficient_image_information')
        numerator = cv2.matchTemplate(source, centered, cv2.TM_CCORR)
        variance = np.zeros_like(numerator)
        for channel in range(3):
            pixels = np.ascontiguousarray(source[:, :, channel])
            sums = cv2.matchTemplate(pixels, mask, cv2.TM_CCORR)
            squares = cv2.matchTemplate(pixels * pixels, mask, cv2.TM_CCORR)
            variance += np.maximum(0, squares - sums * sums / support)
        denominator = np.sqrt(variance * energy)
        # A flat background must never win through floating point roundoff.
        scores = np.divide(numerator, denominator, out=np.full_like(numerator, -1),
                           where=(variance >= support * 12) & (denominator > 0))
        if not np.all(np.isfinite(scores)):
            return review('non_finite_matching_score')
        scores = np.clip(scores, -1, 1)
        _, best, _, location = cv2.minMaxLoc(scores)
        if best < .80:
            return review('weak_match')
        x, y = location
        alternatives = scores.copy()
        alternatives[max(0, y-height//2):y+height//2+1,
                     max(0, x-width//2):x+width//2+1] = -np.inf
        finite = alternatives[np.isfinite(alternatives)]
        if not finite.size:
            return review('ambiguous_match')
        margin = float(best - np.max(finite))
        if margin < .06:
            return review('ambiguous_match')
        variants.append({'xy': [x, y], 'score': float(best), 'margin': margin,
                         'support': int(support)})

    positions = np.asarray([v['xy'] for v in variants])
    if np.any(np.ptp(positions, axis=0) > 1):
        return review('ambiguous_match')
    x, y = np.median(positions, axis=0).astype(int).tolist()
    return {'status': 'predicted', 'result': {'target': [x + width//2, y + height//2]},
            'coordinate_type': 'center_xy_in_image_pixels',
            'target_size': [width, height], 'background_size': [bw, bh],
            'matching_route': 'alpha_masked_ncc', 'solver_version': VERSION,
            'match_score': min(v['score'] for v in variants),
            'peak_margin': min(v['margin'] for v in variants),
            'mask_support': min(v['support'] for v in variants),
            'mask_variants': len(variants), 'score_is_probability': False}
