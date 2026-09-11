"""Synthetic known-position images; no private samples or platform requests."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import unittest

from PIL import Image, ImageDraw, ImageFilter

BASE = Path(__file__).resolve().parent
PYTHON = BASE / '.tools/ddddocr-eval/venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def png(image):
    stream = io.BytesIO()
    image.save(stream, format='PNG')
    return stream.getvalue()


def fixture(position=(183, 71), duplicate=False, flat=False):
    width, height = 110, 110
    background = Image.frombytes('RGB', (360, 240), random.Random(31).randbytes(360*240*3))
    background = background.filter(ImageFilter.GaussianBlur(.7))
    x, y = position
    patch = background.crop((x, y, x+width, y+height))
    if flat:
        patch = Image.new('RGB', patch.size, '#808080')
    mask = Image.new('L', (width, height))
    draw = ImageDraw.Draw(mask)
    draw.ellipse((11, 6, 97, 102), fill=255)
    draw.ellipse((34, 32, 73, 73), fill=0)
    target = patch.convert('RGBA')
    target.putalpha(mask)
    # Arbitrary RGB in transparent pixels must not affect the answer.
    pixels = target.load()
    for j in range(height):
        for i in range(width):
            if pixels[i, j][3] == 0:
                pixels[i, j] = (255, 0, 255, 0)
    draw = ImageDraw.Draw(target)
    draw.ellipse((11, 6, 97, 102), outline='white', width=2)
    draw.ellipse((32, 30, 75, 75), outline='white', width=2)
    # Destination retains texture with a darker exposure.
    darker = patch.point(lambda value: int(value*.65+15))
    background.paste(darker, (x, y), mask)
    if duplicate:
        background.paste(darker, (8, y), mask)
    return target, background, [x+width//2, y+height//2]


@unittest.skipUnless(PYTHON.is_file(), 'Local image runtime required')
class SliderTests(unittest.TestCase):
    def solve(self, target, background):
        raw_target, raw_background = png(target), png(background)
        payload = {'method': 'slide_match',
                   'target_image': base64.b64encode(raw_target).decode(),
                   'background_image': base64.b64encode(raw_background).decode()}
        process = subprocess.run([str(PYTHON), str(BASE/'captcha_engine.py')],
                                 input=json.dumps(payload), capture_output=True,
                                 text=True, timeout=15,
                                 env={**os.environ, 'PYTHONIOENCODING': 'utf-8', 'OMP_NUM_THREADS': '1'})
        self.assertEqual(process.returncode, 0, process.stderr)
        value = json.loads(process.stdout)
        if value['status'] == 'predicted':
            self.assertEqual(value['image_sha256'], hashlib.sha256(raw_target+raw_background).hexdigest())
        return value

    def test_padding_hole_white_border_and_darkening_keep_canvas_coordinates(self):
        target, bg, expected = fixture()
        result = self.solve(target, bg)
        self.assertEqual(result['status'], 'predicted', result)
        self.assertEqual(result['result']['target'], expected)
        self.assertEqual(result['target_size'], [110, 110])
        self.assertEqual(result['background_size'], [360, 240])
        self.assertEqual(result['matching_route'], 'alpha_masked_ncc')
        self.assertEqual(result['mask_variants'], 3)
        self.assertFalse(result['score_is_probability'])

    def test_identical_distant_destinations_are_ambiguous(self):
        target, bg, _ = fixture(duplicate=True)
        self.assertEqual(self.solve(target, bg)['reason'], 'ambiguous_match')

    def test_texture_outside_mask_cannot_rescue_a_flat_piece(self):
        target, bg, _ = fixture(flat=True)
        self.assertEqual(self.solve(target, bg)['reason'], 'insufficient_image_information')

    def test_fully_transparent_piece_declines(self):
        target, bg, _ = fixture()
        target.putalpha(0)
        self.assertEqual(self.solve(target, bg)['status'], 'needs_review')

    def test_uniform_semitransparency_declines(self):
        target, bg, _ = fixture()
        target.putalpha(128)
        self.assertEqual(self.solve(target, bg)['status'], 'needs_review')

    def test_too_little_opaque_support_declines(self):
        target, bg, _ = fixture()
        mask = Image.new('L', target.size)
        ImageDraw.Draw(mask).rectangle((50, 50, 57, 57), fill=255)
        target.putalpha(mask)
        self.assertEqual(self.solve(target, bg)['status'], 'needs_review')

    def test_uniform_background_cannot_win_through_numeric_roundoff(self):
        target, _, _ = fixture()
        for value in (0, 128, 255):
            with self.subTest(value=value):
                bg = Image.new('RGB', (360, 240), (value,)*3)
                self.assertEqual(self.solve(target, bg)['reason'], 'weak_match')

    def test_transparent_background_declines(self):
        target, bg, _ = fixture()
        bg = bg.convert('RGBA')
        bg.putpixel((0, 0), (0, 0, 0, 0))
        self.assertEqual(self.solve(target, bg)['status'], 'needs_review')

    def test_independent_unrelated_texture_declines(self):
        target, bg, _ = fixture()
        bg = Image.frombytes('RGB', bg.size, random.Random(91).randbytes(bg.width*bg.height*3))
        self.assertEqual(self.solve(target, bg)['reason'], 'weak_match')

    def test_wrong_image_scale_declines(self):
        target, bg, _ = fixture()
        target = target.resize((75, 75))
        self.assertEqual(self.solve(target, bg)['status'], 'needs_review')

    def test_matching_images_at_smaller_scale(self):
        target, bg, _ = fixture(position=(182, 70))
        result = self.solve(target.resize((55, 55)), bg.resize((180, 120)))
        self.assertEqual(result['status'], 'predicted', result)
        self.assertEqual(result['result']['target'], [118, 62])

    def test_destination_on_image_boundary(self):
        target, bg, expected = fixture(position=(250, 130))
        result = self.solve(target, bg)
        self.assertEqual(result['status'], 'predicted', result)
        self.assertEqual(result['result']['target'], expected)

    def test_oversized_target_declines(self):
        target, bg, _ = fixture()
        self.assertEqual(self.solve(target.resize((400, 400)), bg)['reason'], 'invalid_dimensions')

    def test_opaque_existing_fixture_retains_legacy_route(self):
        folder = BASE/'tests/fixtures/captcha'
        result = self.solve(Image.open(folder/'target.png'), Image.open(folder/'background.png'))
        self.assertEqual(result['status'], 'predicted', result)
        self.assertNotIn('matching_route', result)


if __name__ == '__main__':
    if sys.argv[1:] == ['--fixture']:
        target, background, expected = fixture()
        print(json.dumps({'target': base64.b64encode(png(target)).decode(),
                          'background': base64.b64encode(png(background)).decode(), 'expected': expected}))
    else:
        unittest.main()
