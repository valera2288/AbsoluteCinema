from math import cos, sin
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from PIL import Image, ImageDraw, ImageFont


class Command(BaseCommand):
    help = "Готовит оригинальные постер и обложку для ручного прогона каталога."

    def add_arguments(self, parser):
        parser.add_argument("directory", type=Path)

    def handle(self, *args, **options):
        directory = options["directory"]
        directory.mkdir(parents=True, exist_ok=True)
        for name, size in (("poster.jpg", (600, 900)), ("banner.jpg", (1280, 640))):
            path = directory / name
            if path.exists():
                raise CommandError(f"Файл уже существует: {path}")
        for name, (width, height) in (("poster.jpg", (600, 900)), ("banner.jpg", (1280, 640))):
            image = Image.new("RGB", (width, height))
            draw = ImageDraw.Draw(image)
            for line in range(height):
                ratio = line / height
                draw.line((0, line, width, line), fill=(int(22 + 26 * ratio), int(18 + 16 * ratio), int(46 + 25 * ratio)))
            center_x = int(width * (0.52 if name == "poster.jpg" else 0.73))
            center_y = int(height * .40)
            for radius in range(32, int(height * .32), 22):
                draw.ellipse((center_x - radius, center_y - radius, center_x + radius, center_y + radius), outline="#79588e", width=2)
            points = [(x, int(center_y + sin(x / 20) * cos(x / 51) * height * .11)) for x in range(width)]
            draw.line(points, fill="#f5b09d", width=4)
            if name == "poster.jpg":
                draw.text((width / 2, height * .76), "NIGHT SIGNAL", fill="#fff1e7",
                          font=ImageFont.load_default(size=45), anchor="mm")
                draw.text((width / 2, height * .82), "ABSOLUTE CINEMA", fill="#cbb6d8",
                          font=ImageFont.load_default(size=21), anchor="mm")
            image.save(directory / name, quality=90)
        self.stdout.write(self.style.SUCCESS("Постер и обложка готовы."))
