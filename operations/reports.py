import csv
from datetime import datetime, time, timedelta
from io import BytesIO

from django.db.models import Count, Q, Sum
from django.http import HttpResponse
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell

from billing.models import Payment
from viewing.models import WatchEvent


def report(kind, start, end):
    zone = timezone.get_current_timezone()
    lower = datetime.combine(start, time.min, tzinfo=zone)
    upper = datetime.combine(end + timedelta(days=1), time.min, tzinfo=zone)
    if kind == "billing":
        paid = Q(paid_at__gte=lower, paid_at__lt=upper)
        refunded = Q(refunded_at__gte=lower, refunded_at__lt=upper)
        totals = Payment.objects.filter(paid | refunded).aggregate(
            paid_count=Count("pk", filter=paid), paid_total=Sum("amount", filter=paid, default=0),
            refund_count=Count("pk", filter=refunded), refund_total=Sum("amount", filter=refunded, default=0),
        )
        return ("Платежей", "Начислено, ₽", "Возвратов", "Возвращено, ₽", "Итог, ₽"), [(
            totals["paid_count"], totals["paid_total"], totals["refund_count"], totals["refund_total"],
            totals["paid_total"] - totals["refund_total"],
        )]
    started = Q(started_at__gte=lower, started_at__lt=upper)
    completed = Q(completed_at__gte=lower, completed_at__lt=upper)
    rows = WatchEvent.objects.filter(started | completed).values("movie_id", "movie__title").annotate(
        starts=Count("pk", filter=started), completions=Count("pk", filter=completed),
    ).order_by("movie_id").values_list("movie_id", "movie__title", "starts", "completions")
    rows = list(rows)
    return ("ID фильма", "Фильм", "Запуски", "Завершения"), rows + [
        ("", "Итого", sum(row[2] for row in rows), sum(row[3] for row in rows)),
    ]


def export_report(kind, start, end, file_format, headers, rows):
    data = [("Период", start.isoformat(), end.isoformat()), headers, *rows]
    if file_format == "csv":
        response = HttpResponse("\ufeff", content_type="text/csv; charset=utf-8")
        writer = csv.writer(response)
        for row in data:
            writer.writerow([("'" + item if isinstance(item, str) and item.lstrip("\ufeff \t\r\n").startswith(("=", "+", "-", "@")) else item)
                             for item in row])
    else:
        workbook = Workbook(write_only=True)
        sheet = workbook.create_sheet("Отчёт")
        for row in data:
            cells = []
            for item in row:
                cell = WriteOnlyCell(sheet, value=item)
                if isinstance(item, str):
                    cell.data_type = "s"
                cells.append(cell)
            sheet.append(cells)
        buffer = BytesIO()
        workbook.save(buffer)
        response = HttpResponse(buffer.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = f'attachment; filename="{kind}-{start}-{end}.{file_format}"'
    return response
