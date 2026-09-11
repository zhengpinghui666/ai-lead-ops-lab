"""Local same-shape pairs; no network or platform/session access.

Segments separated coloured objects on a near-neutral background. A unique
silhouette match must survive three segmentations. Character pairs additionally
require agreement with local OCR; near-identical solid objects
use stronger shape, aspect and separation gates.
Scores are engineering gates, not calibrated success probabilities.
"""
import io
import itertools
import re


def predict(raw):
    # A faint, similarly coloured shadow can join two objects. Try a bounded
    # sequence of stronger colour thresholds only when segmentation is unstable;
    # each accepted result must still agree across three adjacent thresholds.
    for thresholds in ((20,24,28),(28,32,36),(36,40,44)):
        result=_predict(raw,thresholds)
        if result.get('reason') not in ('point_unstable_segmentation','point_fragmented_segmentation'):
            break
    if result.get('reason')=='point_fragmented_segmentation':
        result['reason']='point_segmentation_ambiguous'
    if result.get('reason') == 'point_character_uncertain':
        # A muted object may share the hue of an overlapping vivid object. Keep
        # both saturation populations, and retain all the OCR/shape gates. This
        # image-only fallback never refreshes a challenge or submits an answer.
        for split_thresholds in ((20,24,28),(24,28,32),(28,32,36)):
            candidate = _predict(raw, split_thresholds, split_saturation=True)
            if candidate.get('status') == 'predicted':
                if candidate.get('matching_route') == 'character_agreement':
                    return candidate
                break
            if candidate.get('reason') not in ('point_unstable_segmentation', 'point_fragmented_segmentation'):
                break
    return result


def _predict(raw, thresholds, split_saturation=False):
    import cv2
    import ddddocr
    import numpy as np
    from PIL import Image

    def decline(reason):
        return {'status': 'needs_review', 'result': None, 'reason': reason}

    rgb = np.asarray(Image.open(io.BytesIO(raw)).convert('RGB'))
    height, width = rgb.shape[:2]
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    def normalize(mask, angle=0):
        patch = np.pad(mask, max(mask.shape))
        if angle:
            transform = cv2.getRotationMatrix2D((patch.shape[1]/2, patch.shape[0]/2), angle, 1)
            patch = cv2.warpAffine(patch, transform, (patch.shape[1], patch.shape[0]), flags=cv2.INTER_NEAREST)
        x, y, w, h = cv2.boundingRect(patch)
        return cv2.resize(patch[y:y+h, x:x+w], (64, 64), interpolation=cv2.INTER_NEAREST) > 0

    def components(foreground):
        # Differently coloured objects can touch through tinted shadows. Separate
        # the observed palette before connected components, instead of merging them.
        hist = np.bincount(hsv[:, :, 0][foreground > 0], minlength=180).astype(float)
        smooth = sum(np.roll(hist, i) for i in range(-2, 3)) / 5
        peaks = []
        for p in np.argsort(smooth)[::-1]:
            if smooth[p] < smooth.max()*.04:
                break
            if all(min(abs(int(p)-q), 180-abs(int(p)-q)) >= 10 for q in peaks):
                peaks.append(int(p))
            if len(peaks) >= 6:
                break
        nearest = np.full(foreground.shape, 180, np.int16)
        groups = np.zeros(foreground.shape, np.uint8)
        hue = hsv[:, :, 0].astype(np.int16)
        for group, peak in enumerate(peaks):
            distance = np.abs(hue-peak)
            distance = np.minimum(distance, 180-distance)
            closer = distance < nearest
            groups[closer] = group
            nearest[closer] = distance[closer]
        for group in range(len(peaks)):
            selected = (foreground > 0) & (groups == group)
            _, roots, root_stats, _ = cv2.connectedComponentsWithStats(np.uint8(selected)*255)
            for root, root_stat in enumerate(root_stats[1:],1):
                if root_stat[4] < max(40, width*height*.0007):
                    continue
                selected = roots == root
                partitions = [selected]
                if split_saturation:
                    saturation = hsv[:, :, 1]
                    values = saturation[selected]
                    if len(values) >= 160:
                        boundary, _ = cv2.threshold(values, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
                        lower, upper = values[values <= boundary], values[values > boundary]
                        # Require two substantial, well separated populations;
                        # don't peel a thin shadow or a highlight off every glyph.
                        if min(len(lower), len(upper)) >= max(80, len(values)*.25):
                            low_edge, high_edge = np.quantile(lower, .90), np.quantile(upper, .10)
                            if upper.mean()-lower.mean() >= 50 and high_edge-low_edge >= 20:
                                cut = (low_edge+high_edge)/2
                                pieces = [selected & (saturation <= cut), selected & (saturation > cut)]
                                boxes = []
                                for piece in pieces:
                                    _, _, stats, _ = cv2.connectedComponentsWithStats(np.uint8(piece)*255)
                                    main = max(stats[1:], key=lambda row: row[4])
                                    if main[4] < np.count_nonzero(piece)*.90:
                                        break
                                    boxes.append(main[:4])
                                if len(boxes) == 2:
                                    x,y,w,h = boxes[0]; u,v,s,t = boxes[1]
                                    overlap = max(0,min(x+w,u+s)-max(x,u))*max(0,min(y+h,v+t)-max(y,v))
                                    # Broadly overlapping colour layers are more
                                    # likely shading on one object than two objects.
                                    if overlap <= min(w*h,s*t)*.40:
                                        partitions = pieces
                for partition in partitions:
                    _, labels, stats, _ = cv2.connectedComponentsWithStats(np.uint8(partition)*255)
                    for label, stat in enumerate(stats[1:], 1):
                        x, y, w, h, area = stat
                        if area >= max(40, width*height*.0007):
                            yield stat, np.uint8(labels[y:y+h, x:x+w] == label)*255

    choices = []
    for threshold in thresholds:
        foreground = np.uint8((hsv[:, :, 1] > threshold) & (hsv[:, :, 2] > 70)) * 255
        coverage = np.count_nonzero(foreground) / (width * height)
        if not .02 <= coverage <= .40:
            return decline('point_background_unsupported')
        objects = []
        for (x, y, w, h, area), mask in components(foreground):
            if area > width * height * .25 or x == 0 or y == 0 or x+w == width or y+h == height:
                return decline('point_segmentation_ambiguous')
            if min(w,h)<8:
                return decline('point_fragmented_segmentation')
            variants = [normalize(mask, angle) for angle in range(-20, 21, 5)]
            objects.append({'box': (int(x), int(y), int(w), int(h)), 'mask': mask, 'variants': variants})
        if not 4 <= len(objects) <= 12:
            return decline('point_object_count_unsupported')
        objects.sort(key=lambda obj: (obj['box'][0]+obj['box'][2]/2, obj['box'][1]))
        ranked = []
        for a, b in itertools.combinations(range(len(objects)), 2):
            va, vb = objects[a]['variants'], objects[b]['variants']
            ma, mb = va[4], vb[4]
            score = max(float(np.count_nonzero(v & mb) / np.count_nonzero(v | mb)) for v in va)
            score = max(score, max(float(np.count_nonzero(v & ma) / np.count_nonzero(v | ma)) for v in vb))
            ranked.append((score, a, b))
        ranked.sort(reverse=True)
        score, a, b = ranked[0]
        margin = score - ranked[1][0]
        choices.append((objects, (a, b), score, margin, ranked))
    objects, pair, _, _, _ = choices[1]
    for other, _, _, _, _ in choices:
        if len(other) != len(objects):
            return decline('point_unstable_segmentation')

    def solid(obj):
        contours, _ = cv2.findContours(obj['mask'], cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if len(contours) != 1:
            return False
        hull = np.zeros_like(obj['mask'])
        cv2.drawContours(hull, [cv2.convexHull(contours[0])], -1, 255, -1)
        # Reject hollow letters and highly concave objects. A near-exact match of
        # two solid silhouettes is useful for geometric objects that OCR cannot read.
        return np.count_nonzero(obj['mask']) / max(1, np.count_nonzero(hull)) >= .95

    geometric = all(candidate == pair and score >= .90 and margin >= .18 and all(solid(group[i]) for i in pair)
                    for group, candidate, score, margin, _ in choices)
    ratios = [objects[i]['box'][2]/objects[i]['box'][3] for i in pair]
    geometric = geometric and max(ratios)/min(ratios) <= 1.2
    def character(obj, ocr):
        x, y, w, h = obj['box']
        colour = Image.fromarray(rgb[max(0,y-2):min(height,y+h+2), max(0,x-2):min(width,x+w+2)])
        flat = Image.fromarray(255-obj['mask']).convert('RGB')
        padded = Image.new('RGB', (w+12, h+12), 'white')
        padded.paste(flat, (6, 6))
        votes = []
        for image, threshold in ((colour, .80), (padded, .95)):
            encoded = io.BytesIO()
            image.save(encoded, format='PNG')
            result = ocr.classification(encoded.getvalue(), probability=True)
            text = result.get('text', '') if isinstance(result, dict) else ''
            if not re.fullmatch(r'[A-Za-z0-9]', text) or result.get('confidence', 0) < threshold:
                return None
            votes.append(text.casefold())
        return votes[0] if votes[0] == votes[1] else None

    if geometric:
        pair_score = min(c[2] for c in choices)
        pair_margin = min(c[3] for c in choices)
    else:
        # Coloured shadows can make S resemble 5 more closely than a second S.
        # Rank only OCR-compatible pairs, but still require silhouette agreement.
        # OCR alone also confused V with Y in development; it never suffices alone.
        # Both models are included in the existing installation. Beta distinguishes
        # g/8 in actual 3D glyphs, while the older model handles some small glyphs
        # better. Confident disagreement about a pair prevents automatic selection.
        identities=[]
        for beta in (True, False):
            ocr=ddddocr.DdddOcr(ocr=True, det=False, beta=beta, show_ad=False)
            identities.append([character(obj,ocr) for obj in objects])
        fused=[]
        for i in range(len(objects)):
            labels={row[i] for row in identities if row[i]}
            fused.append(next(iter(labels)) if len(labels)==1 else None)
        compatible = [(a,b) for a,b in itertools.combinations(range(len(objects)),2)
                      if fused[a] and fused[a]==fused[b]]
        if not compatible:
            return decline('point_character_uncertain')
        ranked = []
        for a,b in compatible:
            scores = [next(score for score,x,y in c[4] if (x,y)==(a,b)) for c in choices]
            ranked.append((min(scores),a,b))
        ranked.sort(reverse=True)
        pair_score,a,b = ranked[0]
        pair_margin = pair_score - (ranked[1][0] if len(ranked)>1 else 0)
        if pair_score < .65:
            return decline('point_weak_shape_match')
        if pair_margin < .12:
            return decline('point_ambiguous_pair')
        pair=(a,b)
        # Missing/uncertain OCR must not erase a second near-identical visual pair.
        # Such a challenge has more than one plausible answer and needs review.
        for x,y in itertools.combinations(range(len(objects)),2):
            if (x,y) != pair and all(next(score for score,u,v in c[4] if (u,v)==(x,y)) >= .85 for c in choices):
                return decline('point_ambiguous_pair')

    for other, _, _, _, _ in choices:
        for i in pair:
            if max(abs(a-b) for a,b in zip(objects[i]['box'],other[i]['box'])) > max(width,height)*.02:
                return decline('point_unstable_segmentation')
    points=[]
    for i in pair:
        obj=objects[i]
        x,y,w,h=obj['box']
        distance = cv2.distanceTransform(np.pad(obj['mask'], 1), cv2.DIST_L2, 3)[1:-1,1:-1]
        yy, xx = np.unravel_index(np.argmax(distance), distance.shape)
        points.append([int(x+xx), int(y+yy)])
    return {'status': 'predicted', 'result': {'points': points},
            'coordinate_type': 'xy_in_image_pixels', 'image_size': [width, height],
            'object_count': len(objects), 'matching_route': 'solid_geometry' if geometric else 'character_agreement',
            'pair_score': pair_score, 'pair_margin': pair_margin, 'score_is_probability': False,
            'segmentation_thresholds': list(thresholds),
            'palette_split': 'hue_saturation' if split_saturation else 'hue'}
