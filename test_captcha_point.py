import base64
import io
import json
import os
from pathlib import Path
import subprocess
import unittest

BASE = Path(__file__).resolve().parent
PYTHON = BASE / '.tools/ddddocr-eval/venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def sample(duplicate='3', ambiguous=False, neutral=False, scale=1):
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new('RGB', (480, 300), (244, 244, 244))
    draw = ImageDraw.Draw(image)
    rows = [('3', (28, 18), 62, '#d09b34'), ('G', (202, 13), 48, '#72acdf'),
            ('V', (361, 26), 64, '#ac7b7b'), ('M', (122, 140), 66, '#7eb3cc'),
            (duplicate, (308, 156), 87, '#bd9490'), ('G' if ambiguous else '6', (26, 212), 50, '#c38d2a')]
    regions = []
    for letter, pos, size, colour in rows:
        font = ImageFont.load_default(size=size)
        draw.text(pos, letter, font=font, fill='#777777' if neutral else colour)
        box = draw.textbbox(pos, letter, font=font)
        if letter == '3':
            regions.append([v*scale for v in box])
    if scale != 1:
        image = image.resize((int(image.width*scale), int(image.height*scale)))
    stream = io.BytesIO()
    image.save(stream, format='PNG')
    return stream.getvalue(), regions


def geometry_sample(second='cone', ambiguous=False):
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new('RGB', (480, 300), '#f4f4f4')
    draw = ImageDraw.Draw(image)
    for letter, pos, colour in [('Q',(50,20),'#cc9188'),('4',(230,12),'#72acdf'),('K',(365,45),'#72acdf')]:
        draw.text(pos, letter, font=ImageFont.load_default(size=55), fill=colour)
    def cone(x,y,w,h,colour):
        draw.polygon([(x+w/2,y),(x,y+h*.85),(x+w,y+h*.85)],fill=colour)
        draw.ellipse((x,y+h*.70,x+w,y+h),fill=colour)
    cone(60,160,70,85,'#cc9188')
    if second == 'cone':
        cone(300,150,100,121,'#8eb984')
    elif second == 'wide':
        cone(270,185,170,70,'#8eb984')
    else:
        draw.ellipse((300,160,390,245),fill='#8eb984')
    if ambiguous:
        draw.ellipse((165,120,195,150),fill='#72acdf')
        draw.ellipse((220,235,263,278),fill='#cc9188')
    stream=io.BytesIO();image.save(stream,format='PNG')
    return stream.getvalue()


def lookalike_sample(pair=('S','S')):
    from PIL import Image,ImageDraw,ImageFont
    image=Image.new('RGB',(480,300),'#f4f4f4');draw=ImageDraw.Draw(image)
    rows=[(pair[0],(35,20),37,'#bd88c3'),(pair[1],(300,125),68,'#72acdf'),
          ('g',(215,25),62,'#8eb984'),('8',(105,145),61,'#bd88c3'),
          ('Z',(85,65),43,'#72acdf'),('M',(375,35),49,'#c38d2a')]
    regions=[]
    for i,(letter,pos,size,colour) in enumerate(rows):
        font=ImageFont.load_default(size=size)
        draw.text(pos,letter,font=font,fill=colour)
        if i<2:regions.append(draw.textbbox(pos,letter,font=font))
    stream=io.BytesIO();image.save(stream,format='PNG')
    return stream.getvalue(),regions


def touching_sample(second='7', extra_muted_pair=False, same_colour=False):
    from PIL import Image,ImageDraw,ImageFont
    image=Image.new('RGB',(480,300),'#f4f4f4');draw=ImageDraw.Draw(image)
    font=ImageFont.load_default(size=78)
    vivid,muted='#fdb091','#b9a398'
    rows=[('R',(55,100),vivid if same_colour else muted),('7',(80,50),vivid),
          (second,(310,45),'#9e6fac'),
          ('R' if extra_muted_pair else 'K',(290,200),muted if extra_muted_pair else '#8bb683'),
          ('G',(190,130),'#efc760'),('2',(38,200),'#78acdf')]
    regions=[]
    for index,(letter,position,colour) in enumerate(rows):
        draw.text(position,letter,font=font,fill=colour)
        if index in (1,2):regions.append(draw.textbbox(position,letter,font=font))
    stream=io.BytesIO();image.save(stream,format='PNG')
    return stream.getvalue(),regions


@unittest.skipUnless(PYTHON.is_file(), 'Local ddddocr runtime required')
class PointTests(unittest.TestCase):
    def solve(self, raw, **extra):
        result = subprocess.run([str(PYTHON), str(BASE/'captcha_engine.py')],
            input=json.dumps({'method':'same_shape_pair','image':base64.b64encode(raw).decode(),**extra}),
            capture_output=True, text=True, timeout=15,
            env={**os.environ,'PYTHONIOENCODING':'utf-8','OMP_NUM_THREADS':'1'})
        self.assertEqual(result.returncode, 0)
        return json.loads(result.stdout)

    def assert_pair(self, scale):
        raw, regions = sample(scale=scale)
        result = self.solve(raw)
        self.assertEqual(result['status'], 'predicted', result)
        self.assertEqual(len(result['result']['points']), 2)
        self.assertFalse(result['score_is_probability'])
        for box in regions:
            self.assertEqual(sum(box[0] <= x < box[2] and box[1] <= y < box[3]
                                 for x,y in result['result']['points']), 1)
        self.assertNotIn('text', result)

    def test_scaled_coloured_pair(self):
        self.assert_pair(1)

    def test_resized_pixels_have_corresponding_coordinates(self):
        self.assert_pair(.75)

    def test_no_duplicate_declines(self):
        self.assertEqual(self.solve(sample(duplicate='2')[0])['status'], 'needs_review')

    def test_two_possible_pairs_decline(self):
        self.assertEqual(self.solve(sample(ambiguous=True)[0])['status'], 'needs_review')

    def test_neutral_objects_decline_instead_of_guessing(self):
        self.assertEqual(self.solve(sample(neutral=True)[0])['status'], 'needs_review')

    def test_extra_fields_rejected(self):
        self.assertEqual(self.solve(sample()[0],url='https://example.invalid')['reason'], 'invalid_input')

    def test_invalid_bytes_rejected(self):
        self.assertEqual(self.solve(b'not an image')['reason'], 'invalid_image')

    def test_solid_geometry_without_character_ocr(self):
        result=self.solve(geometry_sample())
        self.assertEqual(result['status'], 'predicted', result)
        self.assertEqual(result['matching_route'], 'solid_geometry')
        for x1,y1,x2,y2 in ((60,160,131,246),(300,150,401,272)):
            self.assertEqual(sum(x1<=x<x2 and y1<=y<y2 for x,y in result['result']['points']),1)

    def test_different_solid_shapes_decline(self):
        self.assertEqual(self.solve(geometry_sample(second='ellipse'))['status'],'needs_review')

    def test_geometric_distortion_declines(self):
        self.assertEqual(self.solve(geometry_sample(second='wide'))['status'],'needs_review')

    def test_ambiguous_solid_pairs_decline(self):
        self.assertEqual(self.solve(geometry_sample(ambiguous=True))['status'],'needs_review')

    def test_real_letter_pair_wins_over_similar_g_and_eight(self):
        raw,regions=lookalike_sample()
        result=self.solve(raw)
        self.assertEqual(result['status'],'predicted',result)
        for x1,y1,x2,y2 in regions:
            self.assertEqual(sum(x1<=x<x2 and y1<=y<y2 for x,y in result['result']['points']),1)

    def test_s_and_five_with_g_and_eight_are_not_duplicate_pairs(self):
        self.assertEqual(self.solve(lookalike_sample(('S','5'))[0])['status'],'needs_review')

    def test_v_and_y_with_g_and_eight_are_not_duplicate_pairs(self):
        self.assertEqual(self.solve(lookalike_sample(('V','Y'))[0])['status'],'needs_review')

    def test_faint_coloured_bridge_can_be_separated_consistently(self):
        import colorsys
        from PIL import Image,ImageDraw
        raw,regions=lookalike_sample(('7','7'))
        image=Image.open(io.BytesIO(raw)).convert('RGB');draw=ImageDraw.Draw(image)
        shadow=tuple(round(c*255) for c in colorsys.hsv_to_rgb(.806,26/255,.90))
        draw.line((43,52,68,68),fill=shadow,width=3)
        draw.rectangle((66,66,92,94),fill='#bd88c3')
        stream=io.BytesIO();image.save(stream,format='PNG')
        result=self.solve(stream.getvalue())
        self.assertEqual(result['status'],'predicted',result)
        self.assertGreater(result['segmentation_thresholds'][0],20)
        for x1,y1,x2,y2 in regions:
            self.assertEqual(sum(x1<=x<x2 and y1<=y<y2 for x,y in result['result']['points']),1)

    def test_faint_thin_shadow_can_be_removed(self):
        import colorsys
        from PIL import Image,ImageDraw
        raw,regions=lookalike_sample()
        image=Image.open(io.BytesIO(raw)).convert('RGB')
        shadow=tuple(round(c*255) for c in colorsys.hsv_to_rgb(.806,26/255,.90))
        ImageDraw.Draw(image).rectangle((430,160,434,190),fill=shadow)
        stream=io.BytesIO();image.save(stream,format='PNG')
        result=self.solve(stream.getvalue())
        self.assertEqual(result['status'],'predicted',result)
        self.assertGreater(result['segmentation_thresholds'][0],20)
        for x1,y1,x2,y2 in regions:
            self.assertEqual(sum(x1<=x<x2 and y1<=y<y2 for x,y in result['result']['points']),1)

    def test_solid_thin_object_is_not_discarded_as_shadow(self):
        from PIL import Image,ImageDraw
        raw,_=lookalike_sample()
        image=Image.open(io.BytesIO(raw)).convert('RGB')
        ImageDraw.Draw(image).rectangle((430,160,434,190),fill='#bd88c3')
        stream=io.BytesIO();image.save(stream,format='PNG')
        result=self.solve(stream.getvalue())
        self.assertEqual(result['status'],'needs_review',result)

    def test_muted_touching_object_is_separated_without_lower_ocr_gates(self):
        raw,regions=touching_sample()
        result=self.solve(raw)
        self.assertEqual(result['status'],'predicted',result)
        self.assertEqual(result['palette_split'],'hue_saturation')
        self.assertEqual(result['object_count'],6)
        for x1,y1,x2,y2 in regions:
            self.assertEqual(sum(x1<=x<x2 and y1<=y<y2 for x,y in result['result']['points']),1)

    def test_touching_unique_letters_do_not_create_a_pair(self):
        self.assertEqual(self.solve(touching_sample(second='Z')[0])['status'],'needs_review')

    def test_second_muted_pair_is_preserved_and_declines_ambiguity(self):
        self.assertEqual(self.solve(touching_sample(extra_muted_pair=True)[0])['status'],'needs_review')

    def test_same_colour_overlap_is_not_split_into_guessed_letters(self):
        self.assertEqual(self.solve(touching_sample(same_colour=True)[0])['status'],'needs_review')


if __name__ == '__main__':
    unittest.main()
