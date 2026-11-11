import asyncio
import io
import logging
from urllib.parse import urlparse, parse_qs

import httpx
from PIL import Image, ImageChops
from apify import Actor
from reportlab.lib.pagesizes import A4, letter
from reportlab.pdfgen.canvas import Canvas

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


def draw_page(pdf, image, page_size, margin):
    page_w, page_h = page_size
    image_w, image_h = image.size
    scale = min((page_w - 2 * margin) / image_w, (page_h - 2 * margin) / image_h)
    if scale <= 0:
        raise ValueError('PDF margins leave no room for image')
    w, h = image_w * scale, image_h * scale
    x, y = (page_w - w) / 2, (page_h - h) / 2
    from reportlab.lib.utils import ImageReader
    pdf.drawImage(ImageReader(image), x, y, width=w, height=h)
    pdf.showPage()


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
        if margin < 0 or max_height < 0:
            raise ValueError('Invalid margins or maxImageHeight')
        rows = await read_all(dataset_id)
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
        pages = 0
        missing = []
        async with httpx.AsyncClient(timeout=40, follow_redirects=True) as client:
            for entry in manifest:
                if not isinstance(entry, dict) or not entry.get('url'):
                    continue
                kind = entry.get('kind')
                if kind == 'photo':
                    # The photo is downloaded directly from the Photos Scraper's
                    # image field; there is no screenshot for this manifest entry.
                    image_url = entry.get('imageUrl')
                    if not image_url:
                        missing.append({'kind': kind, 'url': entry['url'], 'error': 'Direct image URL missing'})
                        continue
                elif kind == 'post':
                    key = canonical_source(entry['url'])
                    image_url = screenshots.get(key)
                    if not image_url:
                        missing.append({'kind': kind, 'url': entry['url'], 'error': 'Post screenshot missing'})
                        continue
                else:
                    missing.append({'kind': kind, 'url': entry['url'], 'error': 'Unknown manifest kind'})
                    continue
                try:
                    image = await download_image(client, image_url)
                    image = prepare_image(image, trim, max_height)
                    draw_page(pdf, image, letter if size == 'LETTER' else A4, margin)
                    pages += 1
                except Exception as exc:
                    log.warning('Could not include %s: %s', entry['url'], exc)
                    missing.append({'kind': entry.get('kind'), 'url': entry['url'], 'error': str(exc)})
        if pages == 0:
            raise RuntimeError('No usable screenshots; no PDF generated')
        pdf.save()
        await Actor.set_value('facebook-posts.pdf', output.getvalue(), content_type='application/pdf')
        summary = {'pages': pages, 'requestedPages': len(manifest), 'skipped': missing, 'pdfKey': 'facebook-posts.pdf'}
        await Actor.set_value('SUMMARY', summary)
        await Actor.push_data(summary)
        log.info('Generated %s pages; skipped %s', pages, len(missing))


if __name__ == '__main__':
    asyncio.run(main())
