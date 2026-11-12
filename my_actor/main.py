import asyncio
import io
import logging
from urllib.parse import urlparse, parse_qs

import httpx
from PIL import Image, ImageChops
from apify import Actor
from reportlab.lib.pagesizes import A4, letter
from reportlab.pdfgen.canvas import Canvas
from reportlab.pdfbase.pdfmetrics import stringWidth
from docx import Document
from docx.shared import Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE as RT


log = logging.getLogger(__name__)


def get_value(row, keys):
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.startswith(('https://', 'http://')):
            return value
        if isinstance(value, dict):
            for nested in ('url', 'uri', 'href'):
                v = value.get(nested)
                if isinstance(v, str) and v.startswith(('https://', 'http://')):
                    return v
    return None


def canonical_source(url):
    if not url:
        return None
    p = urlparse(url)
    if '/photo' in p.path:
        photo_id = parse_qs(p.query).get('fbid', [None])[0]
        return f'photo:{photo_id}' if photo_id else url
    if '/posts/' in p.path:
        return 'post:' + p.path.rstrip('/').lower()
    return url.rstrip('/')


async def read_all(dataset_id):
    ds = Actor.apify_client.dataset(dataset_id)
    output, offset = [], 0
    while True:
        result = await ds.list_items(offset=offset, limit=1000, clean=False)
        batch = result.items or []
        output.extend(batch)
        offset += len(batch)
        if len(batch) < 1000:
            return output


async def download_image(client, url):
    response = await client.get(url, follow_redirects=True)
    response.raise_for_status()
    with Image.open(io.BytesIO(response.content)) as im:
        im.load()
        return im.convert('RGB')


def prepare_image(image, trim, max_height):
    if trim:
        # Trim only white borders, not colored Facebook UI or image content.
        diff = ImageChops.difference(image, Image.new('RGB', image.size, 'white'))
        bbox = diff.getbbox()
        if bbox:
            image = image.crop(bbox)
    if max_height and image.height > max_height:
        new_width = max(1, round(image.width * max_height / image.height))
        image = image.resize((new_width, max_height), Image.Resampling.LANCZOS)
    return image


def wrap_url(url, width, font="Helvetica", size=10):
    """Wrap even unbroken Facebook IDs without dropping characters."""
    lines, line = [], ""
    for char in url:
        if line and stringWidth(line + char, font, size) > width:
            lines.append(line)
            line = ""
        line += char
    if line:
        lines.append(line)
    return lines


def encode_image(image, quality):
    stream = io.BytesIO()
    if quality:
        image.save(stream, format='JPEG', quality=quality, optimize=True, subsampling=0)
    else:
        image.save(stream, format='PNG')
    return stream.getvalue()


def layout(image, page_size, margin, source_url):
    page_w, page_h = page_size
    available_w = page_w - 2 * margin
    lines = wrap_url(source_url, available_w - 2) if source_url else []
    # Shared geometry; reserve a small amount for Word's inline image baseline.
    header = 26 + 13 * len(lines) if lines else 0
    scale = min(available_w / image.width,
                (page_h - 2 * margin - header - 4) / image.height)
    if scale <= 0:
        raise ValueError('Margins or URL leave no room for image')
    w, h = image.width * scale, image.height * scale
    top = margin + header if lines else (page_h - h) / 2
    return lines, w, h, top


def draw_page(pdf, encoded, page_size, margin, source_url, kind, geometry):
    from reportlab.lib.utils import ImageReader
    page_w, page_h = page_size
    lines, w, h, top = geometry
    if lines:
        pdf.setFont('Helvetica-Bold', 10)
        pdf.setFillColorRGB(0, 0, 0)
        pdf.drawCentredString(page_w / 2, page_h - margin - 10,
                              'Facebook Profile' if kind == 'profile' else 'Post Link')
        pdf.setFont('Helvetica', 10)
        pdf.setFillColorRGB(0, 0, 0.65)
        for i, line in enumerate(lines):
            baseline = page_h - margin - 23 - i * 13
            line_width = stringWidth(line, 'Helvetica', 10)
            left = (page_w - line_width) / 2
            pdf.drawCentredString(page_w / 2, baseline, line)
            pdf.linkURL(source_url, (left, baseline - 2, left + line_width, baseline + 10), relative=0, thickness=0)
    pdf.drawImage(ImageReader(io.BytesIO(encoded)), (page_w-w)/2, page_h-top-h, width=w, height=h)
    pdf.showPage()


def new_word(page_size, margin):
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = map(Pt, page_size)
    sec.top_margin = sec.bottom_margin = sec.left_margin = sec.right_margin = Pt(margin)
    normal = doc.styles['Normal']
    normal.font.name = 'Arial'
    normal.font.size = Pt(10)
    return doc


def word_paragraph(doc, first, new_page=False):
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    fmt = p.paragraph_format
    fmt.space_before = fmt.space_after = Pt(0)
    fmt.line_spacing = Pt(13)
    fmt.keep_with_next = False
    fmt.keep_together = True
    fmt.widow_control = False
    fmt.page_break_before = first and new_page
    return p


def add_link(p, text, url):
    link = OxmlElement('w:hyperlink')
    link.set(qn('r:id'), p.part.relate_to(url, RT.HYPERLINK, is_external=True))
    run = OxmlElement('w:r')
    props = OxmlElement('w:rPr')
    for tag, val in [('color', '0000A6'), ('sz', '20')]:
        node = OxmlElement('w:' + tag); node.set(qn('w:val'), val); props.append(node)
    fonts = OxmlElement('w:rFonts'); fonts.set(qn('w:ascii'), 'Arial'); fonts.set(qn('w:hAnsi'), 'Arial'); props.append(fonts)
    run.append(props)
    t = OxmlElement('w:t'); t.text = text; run.append(t)
    link.append(run); p._p.append(link)


def draw_word_page(doc, encoded, page_size, margin, source_url, kind, geometry, new_page):
    lines, w, h, top = geometry
    if lines:
        p = word_paragraph(doc, True, new_page)
        r = p.add_run('Facebook Profile' if kind == 'profile' else 'Post Link')
        r.bold = True
        for line in lines:
            p = word_paragraph(doc, False)
            add_link(p, line, source_url)
    p = word_paragraph(doc, not lines, new_page)
    p.paragraph_format.space_before = Pt(13 if lines else max(0, top-margin))
    # An exact-height line box can position an inline drawing above its
    # paragraph in Word. Let the drawing establish the line height instead.
    fmt = p.paragraph_format
    fmt.line_spacing_rule = WD_LINE_SPACING.SINGLE
    run = p.add_run()
    # Minimize the extra descent/baseline space below the inline drawing.
    run.font.size = Pt(1)
    run.add_picture(io.BytesIO(encoded), width=Pt(w), height=Pt(h))


async def main():
    async with Actor:
        config = await Actor.get_input() or {}
        dataset_id = config.get('datasetId')
        manifest = config.get('manifest')
        if not dataset_id:
            raise ValueError('datasetId is required')
        if not isinstance(manifest, list) or not manifest:
            raise ValueError('manifest must be a nonempty ordered array')
        size = str(config.get('pageSize') or 'LETTER').upper()
        if size not in ('LETTER', 'A4'):
            raise ValueError('pageSize must be LETTER or A4')
        margin = float(config.get('marginPoints', 30))
        max_height = int(config.get('maxImageHeight', 1400))
        trim = bool(config.get('trimWhiteEdges', False))
        jpeg_quality = int(config.get('jpegQuality', 85))
        if not 0 <= jpeg_quality <= 100:
            raise ValueError('jpegQuality must be between 0 and 100 (0 disables JPEG compression)')
        if margin < 0 or max_height < 0:
            raise ValueError('Invalid margins or maxImageHeight')
        rows = await read_all(dataset_id)
        profile_dataset_id = config.get("profileDatasetId")
        if profile_dataset_id and profile_dataset_id != dataset_id:
            rows.extend(await read_all(profile_dataset_id))
        # Map by source URL, never by screenshot dataset row position.
        screenshots = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            if row.get('status') not in (None, 'success', 'SUCCEEDED'):
                continue
            source = get_value(row, ('inputUrl', 'input_url', 'sourceUrl', 'originalUrl', 'url', 'websiteUrl'))
            image = get_value(row, ('screenshotUrl', 'screenshot_url', 'imageUrl', 'image_url', 'screenshot', 'image', 'fileUrl', 'outputUrl'))
            if source and image:
                screenshots.setdefault(canonical_source(source), image)
        if not screenshots:
            raise RuntimeError('No post screenshot URL mappings found in dataset.')
        output = io.BytesIO()
        pdf = Canvas(output, pagesize=letter if size == 'LETTER' else A4, pageCompression=1)
        page_size = letter if size == 'LETTER' else A4
        doc = new_word(page_size, margin)
        pages = 0
        missing = []
        included = []
        async with httpx.AsyncClient(timeout=40, follow_redirects=True) as client:
            for entry in manifest:
                if not isinstance(entry, dict) or not entry.get('url'):
                    continue
                kind = entry.get('kind')
                if kind == 'photo':
                    if entry.get('downloadError'):
                        missing.append({**entry, 'error': entry['downloadError']})
                        continue
                    # The photo is downloaded directly from the Photos Scraper's
                    # image field; there is no screenshot for this manifest entry.
                    image_url = entry.get('imageUrl')
                    if not image_url and not entry.get('storageKey'):
                        missing.append({**entry, 'error': 'Direct image URL missing'})
                        continue
                elif kind in ('profile', 'post'):
                    key = canonical_source(entry['url'])
                    image_url = screenshots.get(key)
                    if not image_url:
                        if kind == 'profile':
                            raise RuntimeError('Required profile screenshot missing')
                        missing.append({'kind': kind, 'url': entry['url'], 'error': 'Post screenshot missing'})
                        continue
                else:
                    missing.append({'kind': kind, 'url': entry['url'], 'error': 'Unknown manifest kind'})
                    continue
                try:
                    if kind == 'photo' and entry.get('storageKey'):
                        record = await Actor.apify_client.key_value_store(entry['storageId']).get_record(entry['storageKey'])
                        if not record:
                            raise ValueError('Cached photo record missing')
                        with Image.open(io.BytesIO(record.value if hasattr(record, 'value') else record['value'])) as im:
                            im.load()
                            image = im.convert('RGB')
                    else:
                        image = await download_image(client, image_url)
                    image = prepare_image(image, trim, max_height)
                    source_url = entry['url'] if kind in ('profile', 'post') else None
                    encoded = encode_image(image, jpeg_quality)
                    geometry = layout(image, page_size, margin, source_url)
                except Exception as exc:
                    if kind == 'profile':
                        raise RuntimeError('Required profile screenshot could not be included') from exc
                    log.warning('Could not include %s: %s', entry['url'], exc)
                    missing.append({**entry, 'error': str(exc)})
                    continue
                # Rendering failures are fatal to avoid mismatched documents.
                draw_page(pdf, encoded, page_size, margin, source_url, kind, geometry)
                draw_word_page(doc, encoded, page_size, margin, source_url, kind, geometry, pages > 0)
                pages += 1
                included.append({"kind": kind, "url": entry["url"], "photoId": entry.get("photoId"), "page": pages})
        if pages == 0:
            raise RuntimeError('No usable screenshots; no PDF generated')
        pdf.save()
        await Actor.set_value('facebook-posts.pdf', output.getvalue(), content_type='application/pdf')
        word_output = io.BytesIO()
        doc.save(word_output)
        await Actor.set_value('facebook-posts.docx', word_output.getvalue(), content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document')
        summary = {'docxKey': 'facebook-posts.docx', 'docxBytes': len(word_output.getvalue()), 'pages': pages, 'requestedPages': len(manifest), 'skipped': missing, 'included': included, 'pdfKey': 'facebook-posts.pdf', 'jpegQuality': jpeg_quality, 'pdfBytes': len(output.getvalue())}
        await Actor.set_value('SUMMARY', summary)
        await Actor.push_data(summary)
        log.info('Generated %s pages; skipped %s', pages, len(missing))


if __name__ == '__main__':
    asyncio.run(main())
