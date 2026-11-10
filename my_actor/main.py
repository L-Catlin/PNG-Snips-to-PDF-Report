import asyncio
import io
import logging
from datetime import datetime, timezone

import httpx
from apify import Actor
from PIL import Image, ImageChops, ImageStat
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4, letter
from reportlab.lib.utils import ImageReader

log = logging.getLogger(__name__)


def image_is_blank(im):
    sample = im.convert('RGB')
    sample.thumbnail((256, 256))
    stat = ImageStat.Stat(sample)
    return (
        all(mean > 250 for mean in stat.mean)
        and max(stat.stddev) < 5
    )


def trim_white_edges(im, threshold=248, padding=8):
    # Optional: trim near-white outer borders.
    # Default OFF to preserve original screenshots.
    rgb = im.convert('RGB')
    background = Image.new('RGB', rgb.size, (255, 255, 255))

    diff = ImageChops.difference(
        rgb, background
    ).convert('L').point(
        lambda x: 255 if x > 255 - threshold else 0
    )

    bounds = diff.getbbox()

    if not bounds:
        return im

    x0, y0, x1, y1 = bounds

    return im.crop((
        max(0, x0 - padding),
        max(0, y0 - padding),
        min(im.width, x1 + padding),
        min(im.height, y1 + padding)
    ))


def extract_url(item):
    for key in (
        'fileUrl',
        'fileURL',
        'screenshotUrl',
        'imageUrl',
        'downloadUrl'
    ):
        value = item.get(key)

        if isinstance(value, str) and value.startswith(
            ('https://', 'http://')
        ):
            return value

    return None


def add_screenshot_page(
    pdf,
    im,
    page_width,
    page_height,
    margin=30
):
    """
    Add exactly one screenshot to one PDF page.

    - Preserves image aspect ratio.
    - Fits within printable page boundaries.
    - Centers horizontally.
    - Aligns image to top.
    - Leaves unused space blank.
    - Never splits screenshots.
    """

    im = im.convert('RGB')

    available_width = page_width - (2 * margin)
    available_height = page_height - (2 * margin)

    # Scale screenshot to fit entirely on one page.
    scale = min(
        available_width / im.width,
        available_height / im.height
    )

    draw_width = im.width * scale
    draw_height = im.height * scale

    # Center horizontally.
    x = (page_width - draw_width) / 2

    # Align to top margin.
    y = page_height - margin - draw_height

    pdf.drawImage(
        ImageReader(im),
        x,
        y,
        width=draw_width,
        height=draw_height
    )

    # Finish this page.
    pdf.showPage()


async def main():
    print("PDF MERGER: main() started", flush=True)

    async with Actor:
        config = await Actor.get_input() or {}

        dataset_id = (
            config.get('datasetId') or ''
        ).strip()

        if not dataset_id:
            raise ValueError(
                'datasetId is required. Enter the '
                'dataset ID of your screenshot Actor run.'
            )

        page_size_name = (
            config.get('pageSize') or 'LETTER'
        ).upper()

        if page_size_name not in ('LETTER', 'A4'):
            raise ValueError(
                'pageSize must be LETTER or A4'
            )

        page_width, page_height = (
            letter if page_size_name == 'LETTER'
            else A4
        )

        margin = float(
            config.get('marginPoints', 30)
        )

        trim = bool(
            config.get('trimWhiteEdges', False)
        )

        # 0 means no height cropping.
        max_image_height = int(
            config.get('maxImageHeight', 0)
        )

        if (
            margin < 0
            or margin * 2 >= min(
                page_width, page_height
            )
        ):
            raise ValueError(
                'Invalid marginPoints'
            )

        if max_image_height < 0:
            raise ValueError(
                'maxImageHeight must be 0 or greater'
            )

        pages = 0
        placed = 0
        skipped = []

        output = io.BytesIO()

        pdf = canvas.Canvas(
            output,
            pagesize=(page_width, page_height),
            pageCompression=1
        )

        pdf.setTitle(
            'Facebook post screenshots'
        )

        # Connect to screenshot dataset.
        dataset = Actor.apify_client.dataset(
            dataset_id
        )

        records = []
        offset = 0

        while True:
            result = await dataset.list_items(
                offset=offset,
                limit=1000,
                clean=False
            )

            batch = result.items
            records.extend(batch)
            offset += len(batch)

            if not batch or len(batch) < 1000:
                break

        log.info(
            'Found %s dataset records',
            len(records)
        )

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(
                60, connect=20
            ),
            follow_redirects=True
        ) as client:

            for index, item in enumerate(
                records, 1
            ):

                url = (
                    extract_url(item)
                    if isinstance(item, dict)
                    else None
                )

                if not url:
                    skipped.append({
                        'index': index,
                        'reason': (
                            'No supported image URL field'
                        )
                    })
                    continue

                try:
                    response = await client.get(url)
                    response.raise_for_status()

                    if len(response.content) > (
                        35 * 1024 * 1024
                    ):
                        raise ValueError(
                            'Image exceeds 35 MB limit'
                        )

                    with Image.open(
                        io.BytesIO(response.content)
                    ) as opened:
                        opened.load()
                        im = opened.convert('RGB')

                    # Skip blank screenshots.
                    if image_is_blank(im):
                        skipped.append({
                            'index': index,
                            'reason': (
                                'Nearly all-white screenshot'
                            ),
                            'url': url
                        })
                        continue

                    # Optional whitespace trimming.
                    if trim:
                        im = trim_white_edges(im)

                    # Optional maximum image height.
                    # Crops from the bottom.
                    if (
                        max_image_height > 0
                        and im.height > max_image_height
                    ):
                        im = im.crop((
                            0,
                            0,
                            im.width,
                            max_image_height
                        ))

                    # One screenshot = one PDF page.
                    add_screenshot_page(
                        pdf=pdf,
                        im=im,
                        page_width=page_width,
                        page_height=page_height,
                        margin=margin
                    )

                    placed += 1
                    pages += 1

                    log.info(
                        'Placed screenshot %s/%s '
                        'on PDF page %s',
                        index,
                        len(records),
                        pages
                    )

                except Exception as exc:
                    log.warning(
                        'Skipping record %s: %s',
                        index,
                        exc
                    )

                    skipped.append({
                        'index': index,
                        'reason': str(exc),
                        'url': url
                    })

        if placed == 0:
            raise ValueError(
                'No usable screenshots found. '
                f'First errors: {skipped[:5]}'
            )

        # Finalize PDF.
        pdf.save()
        output.seek(0)

        await Actor.set_value(
            'facebook-posts.pdf',
            output.read(),
            content_type='application/pdf'
        )

        summary = {
            'datasetId': dataset_id,
            'records': len(records),
            'screenshotsIncluded': placed,
            'screenshotsSkipped': len(skipped),
            'pdfPages': pages,
            'pageSize': page_size_name,
            'maxImageHeight': max_image_height,
            'skipped': skipped,
            'generatedAt': (
                datetime.now(
                    timezone.utc
                ).isoformat()
            )
        }

        await Actor.set_value(
            'SUMMARY',
            summary
        )

        await Actor.push_data({
            'status': 'success',
            'pdfKey': 'facebook-posts.pdf',
            'summary': summary
        })

        log.info(
            'Saved facebook-posts.pdf: '
            '%s screenshots, %s pages',
            placed,
            pages
        )


if __name__ == '__main__':
    print(
        "PDF MERGER: Python entry point reached",
        flush=True
    )
    asyncio.run(main())