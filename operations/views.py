from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST, require_http_methods

from accounts.views import viewer_required
from operations.forms import MarketingForm, SupportForm
from operations.models import Notification, NotificationSettings, SupportRequest
from operations.services import create_request, set_marketing


@viewer_required
@require_http_methods(["GET", "POST"])
def support(request):
    form = SupportForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        ticket = create_request(request.user.pk, **form.cleaned_data)
        return redirect("support-detail", ticket_id=ticket.pk)
    tickets = SupportRequest.objects.filter(owner=request.user).order_by("-updated_at", "-pk")
    page = Paginator(tickets, 20).get_page(request.GET.get("page"))
    return render(request, "operations/support.html", {"form": form, "page": page})


@viewer_required
@require_http_methods(["GET"])
def support_detail(request, ticket_id):
    ticket = get_object_or_404(SupportRequest, owner=request.user, pk=ticket_id)
    return render(request, "operations/support_detail.html", {"ticket": ticket,
        "entries": ticket.messages.order_by("created_at", "pk")})


@viewer_required
@require_http_methods(["GET", "POST"])
def inbox(request):
    preference = NotificationSettings.objects.filter(user=request.user).first()
    form = MarketingForm(request.POST if request.method == "POST" else None,
                         initial={"marketing_opt_in": bool(preference and preference.marketing_opt_in)})
    if request.method == "POST" and form.is_valid():
        set_marketing(request.user.pk, form.cleaned_data["marketing_opt_in"])
        return redirect("notification-inbox")
    rows = Notification.objects.filter(user=request.user).order_by("-sent_at", "-pk")
    return render(request, "operations/inbox.html", {"form": form, "page": Paginator(rows, 20).get_page(request.GET.get("page"))})


@viewer_required
@require_POST
def mark_read(request, notification_id):
    notification = get_object_or_404(Notification, user=request.user, pk=notification_id)
    Notification.objects.filter(pk=notification.pk, is_read=False).update(is_read=True)
    return redirect("notification-inbox")
