from django.urls import path

from billing import views

urlpatterns = [
    path("", views.overview, name="billing-overview"),
    path("checkout/<int:tariff_id>/", views.checkout, name="billing-checkout"),
    path("payments/<uuid:operation_id>/", views.payment_result, name="billing-payment"),
    path("method/", views.method_action, name="billing-method"),
    path("period/<int:period_id>/", views.period_action, name="billing-period"),
]
