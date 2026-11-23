from django.conf import settings


def site_brand(request):
    return {"site_name": settings.SITE_NAME}
